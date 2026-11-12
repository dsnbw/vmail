#!/usr/bin/env bash
set -euo pipefail

# Vmail Core Engine Installer
# Installs the open-source AGPL mail core binary as /usr/local/bin/vmail-core
# and writes /etc/vmail/core-storage.json cleanly.

CORE_VERSION="${VMAIL_CORE_VERSION:-v1.1.0}"
UPSTREAM_REPO="${VMAIL_CORE_REPO:-stalwartlabs/stalwart}"
CORE_BIN="/usr/local/bin/vmail-core"
CLI_BIN="/usr/local/bin/vmail-core-cli"
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
  echo "[Vmail] Installed local core binary -> $CORE_BIN"
  exit 0
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) TARGET="x86_64-unknown-linux-gnu" ;;
  aarch64|arm64) TARGET="aarch64-unknown-linux-gnu" ;;
  armv7l) TARGET="arm-unknown-linux-gnueabihf" ;;
  *)
    echo "[ERROR] Unsupported CPU architecture: $ARCH" >&2
    exit 1
    ;;
esac

if [ -x "$CORE_BIN" ]; then
  echo "[Vmail] Core binary already exists at $CORE_BIN"
  exit 0
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

PKG_URL="https://github.com/${UPSTREAM_REPO}/releases/download/${CORE_VERSION}/stalwart-${TARGET}.tar.gz"
echo "[Vmail] Downloading mail core engine (${CORE_VERSION}, ${TARGET})..."
curl -fsSL "$PKG_URL" -o "$TMP_DIR/core.tar.gz"
tar -xzf "$TMP_DIR/core.tar.gz" -C "$TMP_DIR"

FOUND_BIN="$(find "$TMP_DIR" -maxdepth 2 -type f -perm /111 ! -name '*.so*' | head -n 1)"
if [ -z "$FOUND_BIN" ]; then
  echo "[ERROR] Could not locate core engine binary in archive." >&2
  exit 1
fi
install -m 755 "$FOUND_BIN" "$CORE_BIN"

CLI_URL="https://github.com/${UPSTREAM_REPO}/releases/download/${CORE_VERSION}/stalwart-cli-${TARGET}.tar.gz"
if curl -fsSL "$CLI_URL" -o "$TMP_DIR/cli.tar.gz" 2>/dev/null; then
  tar -xzf "$TMP_DIR/cli.tar.gz" -C "$TMP_DIR"
  FOUND_CLI="$(find "$TMP_DIR" -maxdepth 2 -type f -perm /111 ! -name "$FOUND_BIN" ! -name '*.so*' | grep -i cli | head -n 1 || true)"
  if [ -n "$FOUND_CLI" ]; then
    install -m 755 "$FOUND_CLI" "$CLI_BIN"
  fi
fi

echo "[Vmail] Core engine installed cleanly at $CORE_BIN"
