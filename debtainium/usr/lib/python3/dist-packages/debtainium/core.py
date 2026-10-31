"""
Debtainium Core Module
Handles configuration, GitHub Releases API, architecture detection,
asset matching, SHA-256 verification, dpkg version comparisons, and APT installation.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

APP_VERSION = "1.1.0"
CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
) / "debtainium"
CONFIG_FILE = CONFIG_DIR / "apps.json"
CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
) / "debtainium"
TOKEN_FILE = CONFIG_DIR / "token"

# dpkg-query memoisation: (monotonic timestamp, version-or-None)
_VERSION_CACHE_TTL = 3.0
_version_cache: Dict[str, Tuple[float, Optional[str]]] = {}

DEFAULT_APPS = [
    {
        "name": "debtainium",
        "repo": "Usman0220/debtainium",
        "deb_pkg": "debtainium",
        "asset_pattern": r"debtainium.*all\.deb",
        "installed_version": "1.1.0",
        "latest_version": "1.1.0",
        "last_checked": "",
    }
]


class DebtainiumError(Exception):
    """Base exception for Debtainium operations."""
    pass


class GitHubRateLimitError(DebtainiumError):
    """Raised when GitHub API rate limit is exceeded."""
    pass


class ChecksumMismatchError(DebtainiumError):
    """Raised when downloaded asset SHA-256 does not match expected digest."""
    pass


class InstallationError(DebtainiumError):
    """Raised when package installation via pkexec/apt fails."""
    pass


@dataclass
class AppEntry:
    name: str
    repo: str
    deb_pkg: str
    asset_pattern: str = ""
    installed_version: str = ""
    latest_version: str = ""
    last_checked: str = ""
    auto_update: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppEntry":
        return cls(
            name=data.get("name") or data.get("repo", "unknown").split("/")[-1],
            repo=data.get("repo", ""),
            deb_pkg=data.get("deb_pkg") or data.get("name", ""),
            asset_pattern=data.get("asset_pattern", ""),
            installed_version=data.get("installed_version", ""),
            latest_version=data.get("latest_version", ""),
            last_checked=data.get("last_checked", ""),
            auto_update=bool(data.get("auto_update", False)),
        )


def get_config_dir() -> Path:
    """Return the configuration directory and ensure it exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def get_cache_dir() -> Path:
    """Return the download cache directory and ensure it exists."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def _corrupt_config_backup() -> Optional[Path]:
    """
    Copy an unparseable apps.json aside so a later save cannot destroy it.
    Returns the backup path, or None when there is nothing to preserve.
    """
    if not CONFIG_FILE.exists():
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = CONFIG_FILE.with_name(f"apps.json.corrupt-{stamp}")
    try:
        shutil.copy2(CONFIG_FILE, dest)
        return dest
    except OSError as e:
        sys.stderr.write(f"Warning: could not back up {CONFIG_FILE}: {e}\n")
        return None


def load_apps() -> List[AppEntry]:
    """Load tracked apps from ~/.config/debtainium/apps.json."""
    get_config_dir()
    if not CONFIG_FILE.exists():
        initial_data = {"apps": DEFAULT_APPS}
        save_apps_raw(initial_data)
        return [AppEntry.from_dict(item) for item in DEFAULT_APPS]

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            apps_list = data.get("apps", [])
            return [AppEntry.from_dict(item) for item in apps_list]
    except Exception as e:
        backup = _corrupt_config_backup()
        sys.stderr.write(f"Warning: Failed to parse {CONFIG_FILE}: {e}\n")
        if backup:
            sys.stderr.write(f"Warning: Your previous configuration was preserved at {backup}\n")
        return []


def save_apps_raw(data: Dict[str, Any]) -> None:
    """Save raw dict to apps.json atomically, keeping the previous file as .bak."""
    get_config_dir()
    if CONFIG_FILE.exists():
        try:
            shutil.copy2(CONFIG_FILE, CONFIG_FILE.with_name("apps.json.bak"))
        except OSError:
            pass  # a missing backup is never worth failing a save over

    tmp_file = CONFIG_DIR / f"apps.json.tmp.{os.getpid()}"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_file, CONFIG_FILE)


def save_apps(apps: List[AppEntry]) -> None:
    """Save list of AppEntry objects to apps.json."""
    save_apps_raw({"apps": [app.to_dict() for app in apps]})


def export_apps(apps: List[AppEntry]) -> Dict[str, Any]:
    """Build a portable snapshot of the tracked apps for `debtainium export`."""
    return {
        "format": 1,
        "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "app_version": APP_VERSION,
        "apps": [app.to_dict() for app in apps],
    }


def merge_imported_apps(
    existing: List[AppEntry],
    imported: List[AppEntry],
    replace: bool = False,
) -> Tuple[List[AppEntry], int, int]:
    """
    Merge imported entries into the current list, keyed on repository.

    Returns (apps, added_count, updated_count). In replace mode the imported
    list wins entirely. Entries are matched case-insensitively on `repo` so an
    export from another machine round-trips without duplicates.
    """
    if replace:
        return list(imported), len(imported), 0

    by_repo = {a.repo.lower(): a for a in existing}
    added = updated = 0
    for entry in imported:
        key = entry.repo.lower()
        if key in by_repo:
            current = by_repo[key]
            changed = False
            for field in ("name", "deb_pkg", "asset_pattern", "auto_update"):
                if getattr(entry, field):
                    if getattr(current, field) != getattr(entry, field):
                        setattr(current, field, getattr(entry, field))
                        changed = True
            if changed:
                updated += 1
        else:
            existing.append(entry)
            by_repo[key] = entry
            added += 1
    return existing, added, updated


def normalize_repo(repo_input: str) -> str:
    """
    Normalize GitHub URL or 'owner/repo' input into 'owner/repo'.
    Handles full URLs like https://github.com/owner/repo or https://github.com/owner/repo/releases.
    """
    clean = repo_input.strip()
    clean = re.sub(r"^https?://github\.com/", "", clean)
    clean = re.sub(r"\.git$", "", clean)
    clean = re.sub(r"/releases(/latest)?/?$", "", clean)
    clean = clean.strip("/")
    parts = clean.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid GitHub repository format: '{repo_input}'. Expected 'owner/repo' or GitHub URL.")
    return f"{parts[0]}/{parts[1]}"


def get_system_arch() -> str:
    """
    Detect the machine's Debian architecture using `dpkg --print-architecture`
    with platform.machine() fallback.
    """
    try:
        res = subprocess.run(
            ["dpkg", "--print-architecture"],
            capture_output=True,
            text=True,
            check=True,
        )
        arch = res.stdout.strip()
        if arch:
            return arch
    except Exception:
        pass

    mach = platform.machine().lower()
    mapping = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armhf",
        "armhf": "armhf",
        "i686": "i386",
        "i386": "i386",
    }
    return mapping.get(mach, mach)


def get_installed_version(package_name: str, use_cache: bool = True) -> Optional[str]:
    """
    Query the installed package version using dpkg-query.
    Returns the version string or None if not installed.

    Results are memoised for a few seconds: a single GUI redraw asks for every
    tracked app (twice), and spawning one dpkg-query per row per keystroke is
    the main cause of UI lag with a long list.
    """
    if not package_name:
        return None
    now = time.monotonic()
    if use_cache:
        cached = _version_cache.get(package_name)
        if cached is not None and now - cached[0] < _VERSION_CACHE_TTL:
            return cached[1]
    try:
        res = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "--", package_name],
            capture_output=True,
            text=True,
            check=False,
        )
        version = res.stdout.strip() if res.returncode == 0 else ""
        result = version or None
    except Exception:
        result = None
    if use_cache:
        _version_cache[package_name] = (now, result)
    return result


def invalidate_version_cache(package_name: Optional[str] = None) -> None:
    """Drop memoised dpkg versions after an install/remove changed them."""
    if package_name is None:
        _version_cache.clear()
    else:
        _version_cache.pop(package_name, None)


def clean_version(version_str: str) -> str:
    """
    Clean version strings for dpkg --compare-versions.
    Strips leading 'v' or 'V' to avoid dpkg syntax warnings.
    """
    s = str(version_str).strip()
    if s and (s.startswith("v") or s.startswith("V")) and len(s) > 1 and s[1].isdigit():
        return s[1:]
    return s


def compare_versions(v1: str, op: str, v2: str) -> bool:
    """
    Compare Debian versions using dpkg --compare-versions <v1> <op> <v2>.
    Supported operators: lt, le, eq, ne, ge, gt.
    """
    clean_v1 = clean_version(v1)
    clean_v2 = clean_version(v2)
    try:
        res = subprocess.run(
            ["dpkg", "--compare-versions", clean_v1, op, clean_v2],
            capture_output=True,
            check=False,
        )
        return res.returncode == 0
    except Exception as e:
        sys.stderr.write(f"dpkg --compare-versions failed: {e}\n")
        return False


def is_update_available(installed_ver: Optional[str], latest_ver: Optional[str]) -> bool:
    """
    Determine if an update is available:
    - If not installed, it's considered ready to install (returns True).
    - If both exist, compares installed < latest.
    """
    if not latest_ver:
        return False
    if not installed_ver:
        return True
    return compare_versions(installed_ver, "lt", latest_ver)


def get_github_token() -> Optional[str]:
    """
    Resolve a GitHub token from GITHUB_TOKEN, GH_TOKEN, or
    ~/.config/debtainium/token (in that order).

    The file form is what makes private repositories and CI use practical;
    it is warned about when group/other readable.
    """
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    try:
        if not TOKEN_FILE.is_file():
            return None
        mode = TOKEN_FILE.stat().st_mode & 0o777
        if mode & 0o077:
            sys.stderr.write(
                f"Warning: {TOKEN_FILE} is readable by other users "
                f"(mode {mode:03o}); run: chmod 600 {TOKEN_FILE}\n"
            )
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def get_github_request(url: str) -> urllib.request.Request:
    """
    Build a urllib.request.Request with standard headers and token support.
    """
    headers = {
        "User-Agent": f"Debtainium/{APP_VERSION}",
        "Accept": "application/vnd.github.v3+json",
    }
    token = get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def fetch_latest_release(repo: str) -> Dict[str, Any]:
    """
    Fetch the latest release object from GitHub API.
    Raises GitHubRateLimitError on 403 rate limits, DebtainiumError on other errors.
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = get_github_request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            remaining = e.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                reset_time = e.headers.get("x-ratelimit-reset", "unknown")
                raise GitHubRateLimitError(
                    f"GitHub API rate limit exceeded (reset at {reset_time}). "
                    "Set the GITHUB_TOKEN environment variable to increase limits."
                )
            raise GitHubRateLimitError(f"GitHub API HTTP 403: {e.reason}")
        if e.code == 404:
            raise DebtainiumError(f"No releases found for repository '{repo}' (HTTP 404).")
        raise DebtainiumError(f"GitHub API HTTP error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise DebtainiumError(f"Network error connecting to GitHub: {e.reason}")
    except Exception as e:
        raise DebtainiumError(f"Failed to fetch release for '{repo}': {e}")


def match_asset_name_for_arch(asset_name: str, arch: str, pattern: str = "") -> bool:
    """
    Test whether an asset filename matches the specified machine architecture or regex pattern.
    """
    name_lower = asset_name.lower()
    if not name_lower.endswith(".deb"):
        return False

    # Ignore debug/symbol packages unless explicitly requested in pattern
    if not pattern and re.search(r"(-dbgsym|_dbgsym|-dbg|_dbg|-debuginfo)\.deb$", name_lower):
        return False

    if pattern:
        try:
            return bool(re.search(pattern, asset_name, re.IGNORECASE))
        except re.error as e:
            sys.stderr.write(f"Invalid regex pattern '{pattern}': {e}\n")
            return False

    # Universal architecture
    if "_all.deb" in name_lower or "-all.deb" in name_lower:
        return True

    if arch == "amd64":
        if any(x in name_lower for x in ["amd64", "x86_64", "x64"]):
            if not any(neg in name_lower for neg in ["arm64", "aarch64", "armhf", "armv7", "i386", "i686"]):
                return True
    elif arch == "arm64":
        if any(x in name_lower for x in ["arm64", "aarch64", "armv8"]):
            if not any(neg in name_lower for neg in ["amd64", "x86_64", "x64", "armhf", "armv7", "i386"]):
                return True
    elif arch == "armhf":
        if any(x in name_lower for x in ["armhf", "armv7", "armv6"]):
            if not any(neg in name_lower for neg in ["arm64", "aarch64", "amd64", "x86_64"]):
                return True
    elif arch == "i386":
        if any(x in name_lower for x in ["i386", "i686", "x86"]):
            if not any(neg in name_lower for neg in ["amd64", "x86_64", "x64", "arm64", "armhf"]):
                return True

    return False


def pick_release_asset(release: Dict[str, Any], arch: Optional[str] = None, pattern: str = "") -> Optional[Dict[str, Any]]:
    """
    Pick the best matching .deb asset from the release matching arch or custom regex.
    """
    if arch is None:
        arch = get_system_arch()

    assets = release.get("assets", [])
    if not assets:
        return None

    matched: List[Dict[str, Any]] = []
    for asset in assets:
        name = asset.get("name", "")
        if match_asset_name_for_arch(name, arch, pattern):
            matched.append(asset)

    if not matched:
        return None

    if len(matched) == 1:
        return matched[0]

    # Prioritize:
    # 1. Exact architecture string in filename
    # 2. Non-polyfilled / standard builds
    def asset_score(item: Dict[str, Any]) -> int:
        score = 0
        n = item.get("name", "").lower()
        if f"_{arch}.deb" in n or f"-{arch}.deb" in n:
            score += 10
        elif arch in n:
            score += 5
        if "_all.deb" in n or "-all.deb" in n:
            score += 2
        if "polyfilled" in n:
            score -= 1
        return score

    matched.sort(key=asset_score, reverse=True)
    return matched[0]


def extract_sha256_digest(release: Dict[str, Any], asset_name: str) -> Optional[str]:
    """
    Extract the expected SHA-256 digest if GitHub provides one:
    1. Check asset's own digest or attributes
    2. Check companion checksum files (e.g. SHA256SUMS, checksums.txt, <asset>.sha256)
    3. Check release body markdown for matching 64-hex SHA-256 strings
    """
    assets = release.get("assets", [])

    # 1. Direct asset property
    for a in assets:
        if a.get("name") == asset_name:
            if a.get("digest") and a["digest"].startswith("sha256:"):
                return a["digest"].split("sha256:", 1)[1].strip().lower()

    # 2. Companion checksum files
    checksum_assets = []
    for a in assets:
        aname = a.get("name", "").lower()
        if aname == f"{asset_name.lower()}.sha256" or aname == f"{asset_name.lower()}.sha256sum":
            checksum_assets.insert(0, a)  # direct match top priority
        elif any(k in aname for k in ["sha256", "checksums.txt", "sha256sums", "hashes.txt"]):
            checksum_assets.append(a)

    for ca in checksum_assets:
        try:
            req = get_github_request(ca["browser_download_url"])
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                # Look for line matching asset_name
                for line in text.splitlines():
                    if asset_name in line:
                        m = re.search(r"([a-fA-F0-9]{64})", line)
                        if m:
                            return m.group(1).lower()
                # If file only contains a single 64-hex string and was named <asset>.sha256
                if ca.get("name", "").startswith(asset_name):
                    m = re.search(r"([a-fA-F0-9]{64})", text)
                    if m:
                        return m.group(1).lower()
        except Exception:
            pass

    # 3. Release body search
    body = release.get("body", "")
    if body and asset_name:
        # Pattern 1: SHA256 hash followed by or preceded by asset_name
        lines = body.splitlines()
        for line in lines:
            if asset_name in line:
                m = re.search(r"([a-fA-F0-9]{64})", line)
                if m:
                    return m.group(1).lower()

    return None


def download_asset_file(
    download_url: str,
    dest_path: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """
    Download asset file with progress reporting and atomic write.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(".deb.part")

    req = get_github_request(download_url)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total_size = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with open(temp_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total_size)
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise DebtainiumError(f"Download failed: {e}")

    temp_path.replace(dest_path)
    return dest_path


def verify_file_sha256(file_path: Path, expected_hash: str) -> bool:
    """
    Compute and verify SHA-256 hash of a local file.
    """
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    actual_hash = hasher.hexdigest().lower()
    return actual_hash == expected_hash.lower().strip()


def inspect_deb(deb_path: Path) -> Dict[str, str]:
    """
    Extract Debian control fields from a downloaded .deb file.
    """
    info: Dict[str, str] = {}
    try:
        res = subprocess.run(
            ["dpkg-deb", "-f", str(deb_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        current_key = None
        for line in res.stdout.splitlines():
            if line.startswith(" ") and current_key:
                info[current_key] += "\n" + line.strip()
            elif ":" in line:
                k, v = line.split(":", 1)
                current_key = k.strip()
                info[current_key] = v.strip()
    except Exception as e:
        sys.stderr.write(f"Failed to inspect deb: {e}\n")
    return info


def install_deb_package(deb_path: Path) -> Tuple[int, str, str]:
    """
    Install a .deb package using `pkexec apt-get install -y ./<filename>.deb`.
    This resolves dependencies via APT and invokes polkit for privilege escalation.
    Never runs the main application as root.
    """
    if not deb_path.exists():
        raise FileNotFoundError(f"Package file not found: {deb_path}")

    # NOTE: pkexec always runs the program in the target user's home directory
    # (/root), ignoring the cwd we pass here, so a relative "./<filename>" would
    # not be found ("E: Unsupported file ... given on commandline"). Always hand
    # apt-get an absolute path.
    cmd = ["pkexec", "apt-get", "install", "-y", str(deb_path.resolve())]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or proc.stdout.strip() or f"Exit code {proc.returncode}"
            raise InstallationError(f"APT installation failed ({err_msg})")
        invalidate_version_cache()
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        raise InstallationError("pkexec is not installed on this system.")
    except Exception as e:
        raise InstallationError(f"Error executing pkexec: {e}")


def uninstall_deb_package(package_name: str) -> Tuple[int, str, str]:
    """
    Remove an installed package using `pkexec apt-get remove -y <package_name>`.
    """
    if not package_name:
        raise ValueError("Package name cannot be empty.")

    # "--" stops apt-get from interpreting a package name that starts with "-"
    # as an option.
    cmd = ["pkexec", "apt-get", "remove", "-y", "--", package_name]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or proc.stdout.strip() or f"Exit code {proc.returncode}"
            raise InstallationError(f"APT removal failed ({err_msg})")
        invalidate_version_cache(package_name)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        raise InstallationError("pkexec is not installed on this system.")
    except Exception as e:
        raise InstallationError(f"Error executing pkexec: {e}")
