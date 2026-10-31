"""
Debtainium CLI Interface
Handles command line operations: add, remove, list, check, update, notify.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from debtainium.core import (
    APP_VERSION,
    AppEntry,
    ChecksumMismatchError,
    DebtainiumError,
    InstallationError,
    clean_version,
    compare_versions,
    download_asset_file,
    export_apps,
    extract_sha256_digest,
    fetch_latest_release,
    get_cache_dir,
    get_installed_version,
    get_system_arch,
    inspect_deb,
    install_deb_package,
    is_update_available,
    load_apps,
    merge_imported_apps,
    normalize_repo,
    pick_release_asset,
    save_apps,
    verify_file_sha256,
)


def cmd_add(args: argparse.Namespace) -> int:
    """Add a new GitHub repository to track."""
    try:
        repo = normalize_repo(args.repo)
    except ValueError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    apps = load_apps()
    for app in apps:
        if app.repo.lower() == repo.lower():
            sys.stderr.write(f"Repository '{repo}' is already tracked as '{app.name}'.\n")
            return 1

    name = args.name or repo.split("/")[-1]
    deb_pkg = args.deb_pkg or name.lower()
    asset_pattern = args.pattern or ""

    print(f"Tracking '{repo}' (name: '{name}', deb package: '{deb_pkg}')...")

    # Fetch initial release metadata
    latest_version = ""
    try:
        release = fetch_latest_release(repo)
        tag = release.get("tag_name", "")
        latest_version = clean_version(tag)
        print(f"Latest release found on GitHub: {tag} (version: {latest_version})")

        asset = pick_release_asset(release, pattern=asset_pattern)
        if asset:
            print(f"Matched .deb asset: {asset.get('name')}")
        else:
            sys.stderr.write(f"Warning: No matching .deb asset found for architecture '{get_system_arch()}'.\n")
            sys.stderr.write("You can set a custom regex using --pattern.\n")
    except Exception as e:
        sys.stderr.write(f"Warning: Could not fetch initial release: {e}\n")

    installed_version = get_installed_version(deb_pkg) or ""
    if installed_version:
        print(f"Currently installed on system: {installed_version}")
    else:
        print(f"Package '{deb_pkg}' is not currently installed.")

    new_app = AppEntry(
        name=name,
        repo=repo,
        deb_pkg=deb_pkg,
        asset_pattern=asset_pattern,
        installed_version=installed_version,
        latest_version=latest_version,
        last_checked=datetime.datetime.now().isoformat(timespec="seconds"),
        auto_update=bool(getattr(args, "auto", False)),
    )
    apps.append(new_app)
    save_apps(apps)
    suffix = " [auto-update enabled]" if new_app.auto_update else ""
    print(f"Successfully added '{name}' to Debtainium.{suffix}")
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    """Enable or disable automatic installation for a tracked app."""
    identifier = args.target.strip().lower()
    apps = load_apps()
    matched = [
        app for app in apps
        if app.repo.lower() == identifier
        or app.name.lower() == identifier
        or app.deb_pkg.lower() == identifier
    ]
    if not matched:
        sys.stderr.write(f"No tracked application matched '{args.target}'.\n")
        return 1

    enabled = args.state == "on"
    for app in matched:
        app.auto_update = enabled
    save_apps(apps)

    names = ", ".join(a.name for a in matched)
    if enabled:
        print(f"Auto-update enabled for {names}.")
        print("Run 'debtainium update --auto' (or enable debtainium-autoupdate.timer)")
        print("to install these automatically when a release appears.")
    else:
        print(f"Auto-update disabled for {names}.")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    """Remove a tracked app by repo or name."""
    identifier = args.target.strip().lower()
    apps = load_apps()
    original_count = len(apps)
    remaining = [
        app for app in apps
        if app.repo.lower() != identifier and app.name.lower() != identifier and app.deb_pkg.lower() != identifier
    ]

    if len(remaining) == original_count:
        sys.stderr.write(f"No tracked application matched '{args.target}'.\n")
        return 1

    save_apps(remaining)
    print(f"Removed '{args.target}' from Debtainium.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List all tracked applications and their status."""
    apps = load_apps()
    if not apps:
        if getattr(args, "json", False):
            print(json.dumps({"apps": []}, indent=2))
            return 0
        print("No applications are currently tracked by Debtainium.")
        print("Add one using: debtainium add <owner/repo>")
        return 0

    # Refresh installed versions
    rows = []
    for app in apps:
        inst = get_installed_version(app.deb_pkg) or ""
        app.installed_version = inst

        if not inst:
            status = "Not installed"
        elif not app.latest_version:
            status = "Installed (unknown latest)"
        elif compare_versions(inst, "lt", app.latest_version):
            status = "Update available"
        else:
            status = "Up to date"

        rows.append((
            app.name,
            app.repo,
            app.deb_pkg,
            inst if inst else "-",
            app.latest_version if app.latest_version else "-",
            status,
            "auto" if app.auto_update else "-",
        ))

    save_apps(apps)

    if getattr(args, "json", False):
        payload = {"apps": [dict(zip(
            ("name", "repo", "deb_pkg", "installed", "latest", "status", "auto_update"),
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6] == "auto"),
        )) for r in rows]}
        print(json.dumps(payload, indent=2))
        return 0

    headers = ("NAME", "REPO", "DEB PKG", "INSTALLED", "LATEST", "STATUS", "AUTO")
    col_widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    header_str = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    sep_str = "  ".join("-" * col_widths[i] for i in range(len(headers)))
    print(header_str)
    print(sep_str)
    for r in rows:
        print("  ".join(r[i].ljust(col_widths[i]) for i in range(len(headers))))

    return 0


def check_single_app(app: AppEntry) -> Tuple[bool, str]:
    """
    Check a single app for updates. Returns (has_update, message).
    """
    arch = get_system_arch()
    inst = get_installed_version(app.deb_pkg) or ""
    app.installed_version = inst

    try:
        release = fetch_latest_release(app.repo)
    except Exception as e:
        return False, f"Failed to fetch release for {app.name}: {e}"

    tag = release.get("tag_name", "")
    latest = clean_version(tag)
    app.latest_version = latest
    app.last_checked = datetime.datetime.now().isoformat(timespec="seconds")

    asset = pick_release_asset(release, arch=arch, pattern=app.asset_pattern)
    if not asset:
        return False, f"No matching asset found for architecture '{arch}'"

    if not inst:
        return True, f"Not installed. Latest release available: {latest}"
    if compare_versions(inst, "lt", latest):
        return True, f"Update available: {inst} -> {latest}"
    return False, f"Up to date ({inst})"


def cmd_check(args: argparse.Namespace) -> int:
    """Check for updates across one or all tracked applications."""
    apps = load_apps()
    if not apps:
        print("No tracked applications. Run 'debtainium add <owner/repo>' first.")
        return 0

    target = args.repo.strip().lower() if args.repo else None
    to_check = [
        app for app in apps
        if target is None or app.repo.lower() == target or app.name.lower() == target or app.deb_pkg.lower() == target
    ]

    if not to_check:
        sys.stderr.write(f"No tracked application matched '{args.repo}'.\n")
        return 1

    print(f"Checking {len(to_check)} application(s) against GitHub Releases (arch: {get_system_arch()})...\n")
    updates_found = 0

    for app in to_check:
        print(f"• {app.name} ({app.repo}):")
        has_update, msg = check_single_app(app)
        if has_update:
            updates_found += 1
            print(f"  --> \033[1;32m{msg}\033[0m")
        else:
            print(f"  --> {msg}")

    save_apps(apps)
    print(f"\nDone. {updates_found} update(s) available.")
    return 0


def update_single_app(app: AppEntry) -> bool:
    """
    Download, verify, and install the latest .deb for a single app.
    """
    arch = get_system_arch()
    print(f"\n=== Updating {app.name} ({app.repo}) ===")

    # 1. Fetch latest release
    try:
        release = fetch_latest_release(app.repo)
    except Exception as e:
        sys.stderr.write(f"Failed to fetch latest release: {e}\n")
        return False

    tag = release.get("tag_name", "")
    latest = clean_version(tag)
    app.latest_version = latest

    # 2. Pick asset
    asset = pick_release_asset(release, arch=arch, pattern=app.asset_pattern)
    if not asset:
        sys.stderr.write(f"Error: No .deb asset matching arch '{arch}' found in release {tag}.\n")
        return False

    asset_name = asset["name"]
    download_url = asset["browser_download_url"]
    file_size = asset.get("size", 0)

    # 3. Cache path
    repo_slug = app.repo.replace("/", "_")
    target_dir = get_cache_dir() / repo_slug / latest
    target_file = target_dir / asset_name

    print(f"Asset: {asset_name} ({file_size // 1024} KB)")
    print(f"Download URL: {download_url}")

    # Check if already downloaded
    if not target_file.exists():
        print(f"Downloading to {target_file}...")

        def progress_cb(current: int, total: int):
            if total > 0:
                pct = int(current / total * 100)
                sys.stdout.write(f"\rDownloading: {pct}% ({current // 1024}/{total // 1024} KB)")
                sys.stdout.flush()

        try:
            download_asset_file(download_url, target_file, progress_cb)
            print("\nDownload complete.")
        except Exception as e:
            sys.stderr.write(f"\nDownload failed: {e}\n")
            return False
    else:
        print(f"Using cached file: {target_file}")

    # 4. SHA-256 verification if available
    expected_hash = extract_sha256_digest(release, asset_name)
    if expected_hash:
        print(f"Verifying SHA-256 digest: {expected_hash}...")
        if not verify_file_sha256(target_file, expected_hash):
            sys.stderr.write("Error: SHA-256 digest verification failed! File corrupted or tampered.\n")
            target_file.unlink(missing_ok=True)
            return False
        print("SHA-256 digest verified successfully.")
    else:
        print("Note: No SHA-256 digest provided by GitHub release; skipping checksum check.")

    # 5. Inspect package
    deb_info = inspect_deb(target_file)
    if deb_info.get("Package"):
        app.deb_pkg = deb_info["Package"]

    # 6. Install via pkexec apt-get install -y ./file.deb
    print("Installing package via pkexec and apt-get (you may be prompted for password)...")
    try:
        install_deb_package(target_file)
        print(f"Successfully installed {app.name}!")
    except InstallationError as e:
        sys.stderr.write(f"Installation failed: {e}\n")
        return False

    # 7. Update status
    new_installed = get_installed_version(app.deb_pkg) or latest
    app.installed_version = new_installed
    app.last_checked = datetime.datetime.now().isoformat(timespec="seconds")
    return True


def cmd_update(args: argparse.Namespace) -> int:
    """Download and install updates for one, all, or auto-flagged apps."""
    apps = load_apps()
    if not apps:
        print("No tracked applications. Run 'debtainium add <owner/repo>' first.")
        return 0

    target = args.repo.strip().lower() if args.repo else None
    if target:
        candidates = [
            app for app in apps
            if app.repo.lower() == target or app.name.lower() == target or app.deb_pkg.lower() == target
        ]
        if not candidates:
            sys.stderr.write(f"No tracked application matched '{args.repo}'.\n")
            return 1
        to_update = candidates
    else:
        pool = [app for app in apps if app.auto_update] if args.auto else apps
        if args.auto and not pool:
            print("No applications are marked for auto-update.")
            print("Enable one with: debtainium auto <app> on")
            return 0

        label = "auto-update" if args.auto else "all"
        print(f"Checking {len(pool)} {label} application(s) before proceeding "
              f"(arch: {get_system_arch()})...")
        to_update = []
        for app in pool:
            has_update, msg = check_single_app(app)
            if has_update:
                to_update.append(app)
                print(f"• {app.name}: {msg}")

        if not to_update:
            print("All checked applications are up to date.")
            save_apps(apps)
            return 0

    success_count = 0
    for app in to_update:
        if update_single_app(app):
            success_count += 1

    save_apps(apps)
    print(f"\nUpdate summary: {success_count}/{len(to_update)} package(s) updated successfully.")
    return 0 if success_count == len(to_update) else 1


def cmd_notify(args: argparse.Namespace) -> int:
    """
    Background check command intended for systemd user timer.
    Checks all tracked applications and calls notify-send if updates are available.
    """
    apps = load_apps()
    if not apps:
        return 0

    updates_found: List[Tuple[AppEntry, str, str]] = []
    for app in apps:
        inst = get_installed_version(app.deb_pkg) or ""
        app.installed_version = inst
        try:
            release = fetch_latest_release(app.repo)
            tag = release.get("tag_name", "")
            latest = clean_version(tag)
            app.latest_version = latest
            app.last_checked = datetime.datetime.now().isoformat(timespec="seconds")

            if is_update_available(inst, latest):
                updates_found.append((app, inst or "none", latest))
        except Exception as e:
            sys.stderr.write(f"Debtainium notify check failed for {app.name}: {e}\n")

    save_apps(apps)

    if updates_found:
        count = len(updates_found)
        title = f"Debtainium: {count} Update{'s' if count > 1 else ''} Available"
        lines = [f"{app.name}: {old} → {new}" for app, old, new in updates_found]
        body = "\n".join(lines)

        print(f"{title}\n{body}")

        # Send desktop notification
        try:
            subprocess.run(
                [
                    "notify-send",
                    "-a", "Debtainium",
                    "-i", "debtainium",
                    "-u", "normal",
                    title,
                    body,
                ],
                check=False,
            )
        except FileNotFoundError:
            pass
    else:
        print("Debtainium notify: All tracked packages are up to date.")

    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Export the tracked-app list as portable JSON."""
    apps = load_apps()
    payload = export_apps(apps)
    text = json.dumps(payload, indent=2) + "\n"

    if args.file:
        path = Path(args.file).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            sys.stderr.write(f"Error: could not write {path}: {e}\n")
            return 1
        print(f"Exported {len(apps)} application(s) to {path}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Import tracked apps from JSON produced by `debtainium export`."""
    try:
        if args.file == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.file).expanduser().read_text(encoding="utf-8")
        data = json.loads(raw)
    except OSError as e:
        sys.stderr.write(f"Error: could not read {args.file}: {e}\n")
        return 1
    except json.JSONDecodeError as e:
        sys.stderr.write(f"Error: {args.file} is not valid JSON: {e}\n")
        return 1

    entries = data.get("apps") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        sys.stderr.write("Error: expected an 'apps' list in the import file.\n")
        return 1

    imported: List[AppEntry] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        try:
            repo = normalize_repo(str(item.get("repo", "")))
        except ValueError as e:
            sys.stderr.write(f"Warning: skipping entry ({e})\n")
            continue
        clean = dict(item)
        clean["repo"] = repo
        imported.append(AppEntry.from_dict(clean))

    if not imported:
        sys.stderr.write("Error: no importable applications found.\n")
        return 1

    existing = load_apps()
    merged, added, updated = merge_imported_apps(existing, imported, replace=args.replace)
    save_apps(merged)
    mode = "replaced with" if args.replace else "merged:"
    print(f"Import {mode} {len(imported)} entr(y/ies) -> {added} added, {updated} updated, "
          f"{len(merged)} tracked in total.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="debtainium",
        description="Obtainium-style installer and updater for GitHub Release .deb packages on Debian/Ubuntu.",
    )
    parser.add_argument(
        "--version", action="version", version=f"debtainium {APP_VERSION}"
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    # add
    p_add = subparsers.add_parser("add", help="Track a new GitHub repository")
    p_add.add_argument("repo", help="GitHub repo 'owner/repo' or full GitHub URL")
    p_add.add_argument("--name", help="Display name for the application")
    p_add.add_argument("--pattern", help="Custom regex pattern for .deb asset matching")
    p_add.add_argument("--deb-pkg", help="Debian package name if different from repo name")
    p_add.add_argument("--auto", action="store_true", help="Install updates automatically")
    p_add.set_defaults(func=cmd_add)

    # remove
    p_rem = subparsers.add_parser("remove", help="Remove a tracked repository")
    p_rem.add_argument("target", help="Repository or application name to remove")
    p_rem.set_defaults(func=cmd_remove)

    # list
    p_list = subparsers.add_parser("list", help="List all tracked applications")
    p_list.add_argument("--json", action="store_true", help="Machine-readable output")
    p_list.set_defaults(func=cmd_list)

    # check
    p_chk = subparsers.add_parser("check", help="Check for updates on GitHub")
    p_chk.add_argument("repo", nargs="?", help="Specific repo to check (checks all if omitted)")
    p_chk.set_defaults(func=cmd_check)

    # update
    p_upd = subparsers.add_parser("update", help="Download and install updates")
    p_upd.add_argument("repo", nargs="?", help="Specific repo to update (updates all eligible if omitted)")
    p_upd.add_argument("--all", action="store_true", help="Update every tracked app with an update")
    p_upd.add_argument("--auto", action="store_true",
                       help="Only update apps flagged with 'debtainium auto <app> on'")
    p_upd.set_defaults(func=cmd_update)

    # auto
    p_auto = subparsers.add_parser("auto", help="Enable/disable automatic installation for an app")
    p_auto.add_argument("target", help="Repository, application or package name")
    p_auto.add_argument("state", choices=("on", "off"), help="'on' or 'off'")
    p_auto.set_defaults(func=cmd_auto)

    # export / import
    p_exp = subparsers.add_parser("export", help="Export tracked apps as JSON")
    p_exp.add_argument("-f", "--file", help="Write to this file instead of stdout")
    p_exp.set_defaults(func=cmd_export)

    p_imp = subparsers.add_parser("import", help="Import tracked apps from JSON")
    p_imp.add_argument("file", help="JSON file to read ('-' for stdin)")
    p_imp.add_argument("--replace", action="store_true",
                       help="Replace the current list instead of merging")
    p_imp.set_defaults(func=cmd_import)

    # notify
    p_not = subparsers.add_parser("notify", help="Periodic check and desktop notification (used by systemd timer)")
    p_not.set_defaults(func=cmd_notify)

    return parser


def run_cli(args: Optional[List[str]] = None) -> int:
    """Entry point for CLI execution."""
    parser = build_parser()
    parsed = parser.parse_args(args)
    if not parsed.subcommand:
        parser.print_help()
        return 0
    return parsed.func(parsed)
