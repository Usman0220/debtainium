#!/usr/bin/env bash
# Debtainium installer: fetch the newest release, verify it, install it.
#
#   curl -fsSL https://raw.githubusercontent.com/Usman0220/debtainium/main/install.sh | sudo bash
#
# Options:
#   --tag <tag>          install a specific release instead of the latest
#   --enable-timer       enable the 6-hour background update checker for you
#   --dry-run            download and verify, but do not install
#   -h, --help           show this help
#
# The SHA-256 check compares the package with the checksum file from the same
# release, so it catches truncated or corrupted downloads. It is not a
# signature: GPG verification of releases is planned for 1.3.0.

set -euo pipefail

OWNER_REPO="Usman0220/debtainium"
BASE="https://github.com/${OWNER_REPO}"
TAG=""
ENABLE_TIMER=0
DRY_RUN=0

usage() {
    cat <<'EOF'
Debtainium installer

Usage:
  curl -fsSL https://raw.githubusercontent.com/Usman0220/debtainium/main/install.sh | sudo bash

Options:
  --tag <tag>       install a specific release, e.g. --tag v1.1.0
  --enable-timer    enable the 6-hour background update checker (runs as you)
  --dry-run         download and verify the package, then stop
  -h, --help        show this message

Debtainium installs packages with polkit, so no extra repositories or keys
are added and nothing else on the system is changed. Uninstalling is simply:
  sudo apt remove debtainium
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --tag) [ $# -ge 2 ] || die "--tag needs a value"; TAG="$2"; shift 2 ;;
        --tag=*) TAG="${1#*=}"; shift ;;
        --enable-timer) ENABLE_TIMER=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run me as root: curl -fsSL https://raw.githubusercontent.com/Usman0220/debtainium/main/install.sh | sudo bash"

command -v apt-get >/dev/null 2>&1 || die "this installer needs apt-get (Debian or Ubuntu)"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required (package: coreutils)"

if [ -z "$TAG" ]; then
    printf 'Looking up the latest Debtainium release...\n'
    EFFECTIVE="$(curl -fsSIL -o /dev/null -w '%{url_effective}' "${BASE}/releases/latest")" \
        || die "could not reach GitHub; check your network connection"
    case "$EFFECTIVE" in
        */tag/*) TAG="${EFFECTIVE##*/tag/}" ;;
        *) die "GitHub did not return a release: $EFFECTIVE" ;;
    esac
fi
[ -n "$TAG" ] || die "could not determine a release tag"

VERSION="${TAG#v}"
DEB_NAME="debtainium_${VERSION}_all.deb"
SHA_NAME="${DEB_NAME}.sha256"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/debtainium-install.XXXXXX")"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT INT TERM

printf 'Downloading Debtainium %s...\n' "$VERSION"
curl -fsSL --proto '=https' --tlsv1.2 \
    -o "${WORKDIR}/${SHA_NAME}" "${BASE}/releases/download/${TAG}/${SHA_NAME}" \
    || die "no checksum file in release ${TAG}"
curl -fsSL --proto '=https' --tlsv1.2 --progress-bar \
    -o "${WORKDIR}/${DEB_NAME}" "${BASE}/releases/download/${TAG}/${DEB_NAME}" \
    || die "no .deb package in release ${TAG}"

printf 'Verifying SHA-256...\n'
( cd "$WORKDIR" && sha256sum --check --status "$SHA_NAME" ) \
    || die "checksum mismatch; the download is corrupt, nothing was installed"
DIGEST="$(cut -d' ' -f1 "${WORKDIR}/${SHA_NAME}")"
printf 'Checksum OK (%s)\n' "$DIGEST"

if [ "$DRY_RUN" -eq 1 ]; then
    printf 'Dry run: verified %s, not installing.\n' "$WORKDIR/$DEB_NAME"
    trap - EXIT
    printf 'Kept: %s\n' "$WORKDIR/$DEB_NAME"
    exit 0
fi

printf 'Installing with apt...\n'
DEBIAN_FRONTEND=noninteractive apt-get install -y "${WORKDIR}/${DEB_NAME}"

INSTALLED="$(dpkg-query --show --showformat='${Version}' debtainium 2>/dev/null || echo '?')"
printf '\nDebtainium %s installed.\n' "$INSTALLED"

if [ "$ENABLE_TIMER" -eq 1 ]; then
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && command -v systemctl >/dev/null 2>&1; then
        TARGET_UID="$(id -u "$SUDO_USER")"
        printf 'Enabling the background update checker for %s...\n' "$SUDO_USER"
        sudo -u "$SUDO_USER" \
            XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
            systemctl --user enable --now debtainium-notify.timer \
            || printf 'Could not enable it automatically; run: systemctl --user enable --now debtainium-notify.timer\n'
    else
        printf 'Run this as your normal user to enable the checker:\n  systemctl --user enable --now debtainium-notify.timer\n'
    fi
else
    printf 'Optional, check for updates every 6 hours:\n  systemctl --user enable --now debtainium-notify.timer\n'
fi

printf 'Launch the GUI with "debtainium gui", or see "debtainium --help".\n'
