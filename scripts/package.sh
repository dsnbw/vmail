#!/usr/bin/env bash
set -euo pipefail

# Build clean portable Vmail release tarball + SHA256SUMS.txt
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-v1.2.0}"
DIST_DIR="${REPO_DIR}/dist"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

mkdir -p "$DIST_DIR" "${STAGE_DIR}/vmail-${VERSION}"

for item in admin bridge cli cloudflare core docs nginx scripts systemd webmail install.sh README.md LICENSE .gitattributes .gitignore; do
  if [ -e "${REPO_DIR}/${item}" ]; then
    cp -a "${REPO_DIR}/${item}" "${STAGE_DIR}/vmail-${VERSION}/"
  fi
done

find "${STAGE_DIR}" -name '__pycache__' -o -name '*.pyc' -o -name '.DS_Store' | xargs -r rm -rf

OUT_TAR="${DIST_DIR}/vmail-${VERSION}.tar.gz"
tar -czf "$OUT_TAR" -C "$STAGE_DIR" "vmail-${VERSION}"
(cd "$DIST_DIR" && sha256sum "vmail-${VERSION}.tar.gz" > SHA256SUMS.txt)
echo "[OK] Built ${OUT_TAR}"
cat "${DIST_DIR}/SHA256SUMS.txt"
