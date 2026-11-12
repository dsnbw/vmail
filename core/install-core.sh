#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Vmail Core Engine Installer
# Installs the open-source AGPL mail core binary as /usr/local/bin/vmail-core
# Multi-arch & Multi-libc (glibc & Alpine musl) with SHA256 verification.
# ==============================================================================

CORE_RELEASE="${VMAIL_CORE_RELEASE:-core-v1.1.0}"
CORE_REPO="${VMAIL_CORE_REPO:-dsnbw/vmail}"
CORE_BIN="/usr/local/bin/vmail-core"
CFG_PATH="/etc/vmail/core-storage.json"
DATA_DIR="/var/lib/vmail-core"
LOG_DIR="/var/log/vmail"

mkdir -p /etc/vmail "$DATA_DIR" "$LOG_DIR"

if [ ! -f "$CFG_PATH" ]; then
  cat > "$CFG_PATH" <<'EOF'
{
  "@type": "RocksDb",
  "path": "/var/lib/vmail-core/",
  "blobSize": 4096,
  "bufferSize": 134217728,
  "poolWorkers": null,
  "cacheSize": null
}
EOF
  chmod 600 "$CFG_PATH"
fi

if [ -n "${VMAIL_CORE_BIN:-}" ] && [ -x "${VMAIL_CORE_BIN}" ]; then
  install -m 755 "${VMAIL_CORE_BIN}" "$CORE_BIN"
  echo "[Vmail] Installed custom core binary -> $CORE_BIN"
  exit 0
fi

# Detect libc flavor (glibc vs musl on Alpine)
LIBC="gnu"
if ls /lib/ld-musl-*.so* >/dev/null 2>&1 || (ldd --version 2>&1 | grep -qi musl); then
  LIBC="musl"
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)
    TARGET="x86_64-unknown-linux-${LIBC}"
    ;;
  aarch64|arm64)
    TARGET="aarch64-unknown-linux-${LIBC}"
    ;;
  armv7l|armv7|armhf)
    if [ "$LIBC" = "musl" ]; then
      TARGET="armv7-unknown-linux-musleabihf"
    else
      TARGET="armv7-unknown-linux-gnueabihf"
    fi
    ;;
  *)
    echo "[ERROR] Unsupported CPU architecture: $ARCH (${LIBC})" >&2
    exit 1
    ;;
esac

if [ -x "$CORE_BIN" ] && [ "${VMAIL_FORCE_REINSTALL:-0}" != "1" ]; then
  echo "[Vmail] Core binary already exists at $CORE_BIN ($TARGET)"
  exit 0
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

BASE_URL="https://github.com/${CORE_REPO}/releases/download/${CORE_RELEASE}"
PKG_NAME="vmail-core-${TARGET}.tar.gz"
PKG_URL="${BASE_URL}/${PKG_NAME}"

echo "[Vmail] Downloading mail core engine (${CORE_RELEASE}, ${TARGET})..."
curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 300 "$PKG_URL" -o "$TMP_DIR/$PKG_NAME"

# Verify SHA256 if checksum manifest is available
if curl -fsSL --retry 2 --connect-timeout 10 "${BASE_URL}/SHA256SUMS.txt" -o "$TMP_DIR/SHA256SUMS.txt" 2>/dev/null; then
  EXPECTED_HASH="$(grep -F "  ${PKG_NAME}" "$TMP_DIR/SHA256SUMS.txt" | awk '{print $1}' || true)"
  if [ -n "$EXPECTED_HASH" ] && command -v sha256sum >/dev/null 2>&1; then
    ACTUAL_HASH="$(sha256sum "$TMP_DIR/$PKG_NAME" | awk '{print $1}')"
    if [ "$EXPECTED_HASH" != "$ACTUAL_HASH" ]; then
      echo "[ERROR] Checksum mismatch for $PKG_NAME (expected $EXPECTED_HASH, got $ACTUAL_HASH)" >&2
      exit 1
    fi
    echo "[Vmail] SHA256 checksum verified."
  fi
fi

tar -xzf "$TMP_DIR/$PKG_NAME" -C "$TMP_DIR"
FOUND_BIN=""
if [ -f "$TMP_DIR/vmail-core" ]; then
  FOUND_BIN="$TMP_DIR/vmail-core"
else
  FOUND_BIN="$(find "$TMP_DIR" -maxdepth 2 -type f ! -name '*.tar.gz' ! -name '*.txt' ! -name '*.so*' | head -n 1 || true)"
fi

if [ -z "$FOUND_BIN" ] || [ ! -f "$FOUND_BIN" ]; then
  echo "[ERROR] Could not locate core engine binary inside $PKG_NAME." >&2
  exit 1
fi

install -m 755 "$FOUND_BIN" "$CORE_BIN"

# Quick sanity check that binary executes under current libc/arch
if ! "$CORE_BIN" --version >/dev/null 2>&1 && ! "$CORE_BIN" --help >/dev/null 2>&1; then
  echo "[ERROR] Installed $CORE_BIN cannot execute on this host ($TARGET)." >&2
  exit 1
fi

echo "[Vmail] Core engine installed cleanly at $CORE_BIN"
