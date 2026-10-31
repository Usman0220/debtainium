#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="${SCRIPT_DIR}/debtainium"
OUTPUT_DEB="${SCRIPT_DIR}/debtainium_1.0.0_all.deb"

echo "=========================================================="
echo " Building Debtainium Debian Package: debtainium_1.0.0_all.deb"
echo "=========================================================="

# Ensure changelog is compressed with gzip -9n
if [ -f "${PKG_DIR}/usr/share/doc/debtainium/changelog" ]; then
    gzip -9n -f "${PKG_DIR}/usr/share/doc/debtainium/changelog"
fi

# Ensure man page is compressed with gzip -9n
if [ -f "${PKG_DIR}/usr/share/man/man1/debtainium.1" ]; then
    gzip -9n -f "${PKG_DIR}/usr/share/man/man1/debtainium.1"
fi

echo "--> Applying strict Debian directory & file permissions..."
# Standard directories: 0755
find "${PKG_DIR}" -type d -exec chmod 755 {} +

# Standard files: 0644
find "${PKG_DIR}" -type f -exec chmod 644 {} +

# Executables & maintainer scripts: 0755
chmod 755 "${PKG_DIR}/DEBIAN/postinst"
chmod 755 "${PKG_DIR}/DEBIAN/prerm"
chmod 755 "${PKG_DIR}/usr/bin/debtainium"

echo "--> Packaging with dpkg-deb --build --root-owner-group..."
dpkg-deb --build --root-owner-group "${PKG_DIR}" "${OUTPUT_DEB}"

echo ""
echo "=========================================================="
echo " Package Information (dpkg-deb -I)"
echo "=========================================================="
dpkg-deb -I "${OUTPUT_DEB}"

echo ""
echo "=========================================================="
echo " Package File Tree (dpkg-deb -c)"
echo "=========================================================="
dpkg-deb -c "${OUTPUT_DEB}"

echo ""
echo "=========================================================="
echo " Lintian QA Package Validation"
echo "=========================================================="
lintian --tag-display-limit 0 "${OUTPUT_DEB}" || true

echo ""
echo "Build succeeded: ${OUTPUT_DEB}"
