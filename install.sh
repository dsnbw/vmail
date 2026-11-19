#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Vmail Mail Server & Gmail-Style Webmail — Universal Self-Healing Installer
# Version: 1.2.0
# Lightweight, high-speed (OPcache JIT + Keepalive + Gzip Static + HTTP/2/3),
# zero-license, multi-domain, multi-OS, Cloudflare-ready.
# OS Supported: Ubuntu, Debian, Armbian, Rocky, AlmaLinux, RHEL, CentOS, Fedora,
#               Amazon Linux, openSUSE/SLES, Alpine Linux, Arch Linux
#               (Bare-metal, VPS, LXC, Docker, PRoot)
#
# Quick 1-line install:
#   curl -fsSL https://raw.githubusercontent.com/dsnbw/vmail/main/install.sh | sudo bash -s -- --domain example.com
# ==============================================================================

VMAIL_VERSION="${VMAIL_VERSION:-v1.2.1}"
VMAIL_REPO="${VMAIL_REPO:-dsnbw/vmail}"
ROUNDCUBE_VERSION="${VMAIL_ROUNDCUBE_VERSION:-1.6.10}"
WEBMAIL_ROOT="/var/www/vmail-webmail"
WEBMAIL_DB_DIR="/var/lib/vmail-webmail"
ADMIN_DIR="/opt/vmail"
CFG_DIR="/etc/vmail"
LOG_DIR="/var/log/vmail"

PRIMARY_DOMAIN="${VMAIL_DOMAIN:-}"
MAIL_HOST="${VMAIL_MAIL_HOST:-}"
ADMIN_EMAIL="${VMAIL_ADMIN_EMAIL:-}"
ADMIN_PASS="${VMAIL_ADMIN_PASS:-}"
CF_EMAIL="${VMAIL_CF_EMAIL:-}"
CF_KEY="${VMAIL_CF_KEY:-}"
TUNNEL_TOKEN="${VMAIL_TUNNEL_TOKEN:-}"
RELAY_HOST="${VMAIL_RELAY_HOST:-}"
RELAY_PORT="${VMAIL_RELAY_PORT:-587}"
RELAY_USER="${VMAIL_RELAY_USER:-}"
RELAY_PASS="${VMAIL_RELAY_PASS:-}"
RELAY_TLS="0"
HTTP_PORT="${VMAIL_HTTP_PORT:-8080}"
NON_INTERACTIVE="0"

usage() {
  cat <<'EOF'
Vmail Universal Self-Healing Installer (v1.2.1):
  --domain <domain>           Primary domain (e.g. example.com)
  --mail-host <hostname>      Webmail/MX hostname (default: mail.<domain>)
  --admin-email <email>       Admin mailbox (default: admin@<domain>)
  --admin-pass <password>     Admin password (auto-generated if omitted)
  --cf-email <email>          Cloudflare Account Email (optional auto DNS/Tunnel)
  --cf-key <api_key>          Cloudflare Global API Key (optional auto DNS/Tunnel)
  --tunnel-token <token>      Cloudflare Tunnel Token (optional NAT bypass)
  --relay-host <host>         Outbound SMTP Relay Host (optional, e.g. smtp-relay.brevo.com)
  --relay-port <port>         Outbound SMTP Relay Port (default: 587)
  --relay-user <user>         Outbound SMTP Relay Username
  --relay-pass <pass>         Outbound SMTP Relay Password/Key
  --relay-tls                 Use Implicit TLS (port 465) on Relay
  --http-port <port>          Nginx HTTP listen port (default: 8080; use 80 for direct VPS)
  --non-interactive           Do not prompt interactively
  -h, --help                  Show help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) PRIMARY_DOMAIN="$2"; shift 2 ;;
    --mail-host) MAIL_HOST="$2"; shift 2 ;;
    --admin-email) ADMIN_EMAIL="$2"; shift 2 ;;
    --admin-pass) ADMIN_PASS="$2"; shift 2 ;;
    --cf-email) CF_EMAIL="$2"; shift 2 ;;
    --cf-key) CF_KEY="$2"; shift 2 ;;
    --tunnel-token) TUNNEL_TOKEN="$2"; shift 2 ;;
    --relay-host) RELAY_HOST="$2"; shift 2 ;;
    --relay-port) RELAY_PORT="$2"; shift 2 ;;
    --relay-user) RELAY_USER="$2"; shift 2 ;;
    --relay-pass) RELAY_PASS="$2"; shift 2 ;;
    --relay-tls) RELAY_TLS="1"; shift ;;
    --http-port) HTTP_PORT="$2"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Please run install.sh as root (sudo bash install.sh)." >&2
  exit 1
fi

log()  { printf '\033[1;34m[Vmail]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }

# Robust resilient download helper (retries + automatic IPv4 fallback)
fetch_with_retry() {
  local url="$1"
  local dest="$2"
  local attempt
  for attempt in 1 2 3; do
    if curl -fsSL --connect-timeout 15 --max-time 300 "$url" -o "$dest" 2>/dev/null; then
      return 0
    fi
    if curl -4 -fsSL --connect-timeout 15 --max-time 300 "$url" -o "$dest" 2>/dev/null; then
      return 0
    fi
    warn "Download attempt ${attempt} failed for ${url}, retrying in $((attempt * 2))s..."
    sleep $((attempt * 2))
  done
  return 1
}

# Detect local repo vs standalone piped execution (curl | sudo bash)
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
REPO_DIR=""
TMP_BUNDLE_DIR=""
cleanup_tmp() {
  [ -n "${TMP_BUNDLE_DIR:-}" ] && rm -rf "$TMP_BUNDLE_DIR" 2>/dev/null || true
}
trap cleanup_tmp EXIT

if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
  CAND_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
  if [ -f "$CAND_DIR/admin/app/server.py" ] && [ -f "$CAND_DIR/core/install-core.sh" ]; then
    REPO_DIR="$CAND_DIR"
  fi
fi

# Interactive prompts when missing (reads from /dev/tty when piped!)
if [ "$NON_INTERACTIVE" != "1" ] && [ -r /dev/tty ]; then
  if [ -z "$PRIMARY_DOMAIN" ]; then
    read -rp "Primary Mail Domain (e.g. example.com): " PRIMARY_DOMAIN </dev/tty
  fi
  if [ -z "$MAIL_HOST" ] && [ -n "$PRIMARY_DOMAIN" ]; then
    read -rp "Webmail Hostname [mail.${PRIMARY_DOMAIN}]: " MAIL_HOST </dev/tty
  fi
  if [ -z "$ADMIN_EMAIL" ] && [ -n "$PRIMARY_DOMAIN" ]; then
    read -rp "Admin Email [admin@${PRIMARY_DOMAIN}]: " ADMIN_EMAIL </dev/tty
  fi
  if [ -z "$ADMIN_PASS" ]; then
    read -rsp "Admin Password (Enter to auto-generate): " ADMIN_PASS </dev/tty
    echo >/dev/tty
  fi
  if [ -z "$CF_EMAIL" ]; then
    read -rp "Cloudflare Account Email (Enter to skip now): " CF_EMAIL </dev/tty
    if [ -n "$CF_EMAIL" ] && [ -z "$CF_KEY" ]; then
      read -rsp "Cloudflare Global API Key: " CF_KEY </dev/tty
      echo >/dev/tty
    fi
  fi
fi

if [ -z "$PRIMARY_DOMAIN" ]; then
  echo "[ERROR] Primary domain is required (--domain example.com)." >&2
  exit 1
fi

PRIMARY_DOMAIN="$(echo "$PRIMARY_DOMAIN" | tr '[:upper:]' '[:lower:]' | sed 's/^\s*//;s/\s*$//;s/\.$//')"
MAIL_HOST="${MAIL_HOST:-mail.${PRIMARY_DOMAIN}}"
MAIL_HOST="$(echo "$MAIL_HOST" | tr '[:upper:]' '[:lower:]' | sed 's/^\s*//;s/\s*$//;s/\.$//')"
ADMIN_EMAIL="$(echo "${ADMIN_EMAIL:-admin@${PRIMARY_DOMAIN}}" | tr '[:upper:]' '[:lower:]' | sed 's/^\s*//;s/\s*$//')"

if ! echo "$PRIMARY_DOMAIN" | grep -Eq '^[a-z0-9.-]+\.[a-z]{2,63}$'; then
  echo "[ERROR] Invalid primary domain format: $PRIMARY_DOMAIN" >&2
  exit 1
fi

if [ -z "$ADMIN_PASS" ]; then
  ADMIN_PASS="$(tr -dc 'A-HJ-KM-Za-km-z2-9' </dev/urandom | head -c 16)"
  GEN_PASS_SHOWN="1"
else
  GEN_PASS_SHOWN="0"
fi

# Detect OS Family & Package Manager
OS_FAMILY="unknown"
PKG_MGR=""
if command -v apt-get >/dev/null 2>&1; then
  PKG_MGR="apt"; OS_FAMILY="debian"
elif command -v dnf >/dev/null 2>&1; then
  PKG_MGR="dnf"; OS_FAMILY="rhel"
elif command -v yum >/dev/null 2>&1; then
  PKG_MGR="yum"; OS_FAMILY="rhel"
elif command -v zypper >/dev/null 2>&1; then
  PKG_MGR="zypper"; OS_FAMILY="suse"
elif command -v apk >/dev/null 2>&1; then
  PKG_MGR="apk"; OS_FAMILY="alpine"
elif command -v pacman >/dev/null 2>&1; then
  PKG_MGR="pacman"; OS_FAMILY="arch"
else
  echo "[ERROR] No supported package manager found (apt, dnf, yum, zypper, apk, pacman)." >&2
  exit 1
fi

INIT_SYS="standalone"
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  INIT_SYS="systemd"
elif command -v rc-service >/dev/null 2>&1 && [ -d /run/openrc ]; then
  INIT_SYS="openrc"
fi

log "Detected OS: ${OS_FAMILY} (${PKG_MGR}) | Init: ${INIT_SYS} | Arch: $(uname -m)"

# Self-healing cross-distro dependency installer
install_packages() {
  case "$PKG_MGR" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      # Wait out concurrent apt/dpkg locks if present
      for _ in $(seq 1 20); do
        if ! fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1; then
          break
        fi
        warn "Waiting for another apt/dpkg process to finish..."
        sleep 3
      done
      dpkg --configure -a >/dev/null 2>&1 || true
      apt-get update -qq || { warn "apt-get update retried with --allow-releaseinfo-change"; apt-get update --allow-releaseinfo-change -qq || true; }
      if ! apt-get install -y -qq curl wget tar gzip unzip openssl python3 nginx \
        php-fpm php-xml php-mbstring php-intl php-zip php-gd php-curl php-sqlite3 >/dev/null 2>&1; then
        warn "Initial apt install hit an issue — running self-healing dpkg/apt recovery..."
        dpkg --configure -a >/dev/null 2>&1 || true
        apt-get -f install -y -qq >/dev/null 2>&1 || true
        apt-get install -y --fix-missing curl wget tar gzip unzip openssl python3 nginx \
          php-fpm php-xml php-mbstring php-intl php-zip php-gd php-curl php-sqlite3
      fi
      apt-get install -y -qq mariadb-server php-mysql >/dev/null 2>&1 || true
      apt-get install -y -qq php-imagick >/dev/null 2>&1 || true
      ;;
    dnf|yum)
      "$PKG_MGR" install -y -q epel-release >/dev/null 2>&1 || true
      if ! "$PKG_MGR" install -y -q curl wget tar gzip unzip openssl python3 nginx \
        php-fpm php-xml php-mbstring php-intl php-zip php-gd php-json php-pdo >/dev/null 2>&1; then
        warn "Retrying ${PKG_MGR} without optional php-json (built into PHP 8+)..."
        "$PKG_MGR" install -y curl wget tar gzip unzip openssl python3 nginx \
          php-fpm php-xml php-mbstring php-intl php-zip php-gd php-pdo php-opcache
      fi
      "$PKG_MGR" install -y -q php-opcache >/dev/null 2>&1 || true
      "$PKG_MGR" install -y -q mariadb-server php-mysqlnd >/dev/null 2>&1 || true
      ;;
    zypper)
      zypper --non-interactive refresh >/dev/null 2>&1 || true
      zypper --non-interactive install -y curl wget tar gzip unzip openssl python3 nginx \
        php8 php8-fpm php8-xmlreader php8-xmlwriter php8-dom php8-mbstring php8-intl \
        php8-zip php8-gd php8-curl php8-pdo php8-sqlite php8-opcache >/dev/null 2>&1 || \
      zypper --non-interactive install -y curl wget tar gzip unzip openssl python3 nginx \
        php php-fpm php-xml php-mbstring php-intl php-zip php-gd php-curl php-pdo php-sqlite
      zypper --non-interactive install -y mariadb php8-mysql >/dev/null 2>&1 || true
      ;;
    apk)
      apk update >/dev/null
      PHP_VER=""
      for cand in php85 php84 php83 php82 php81; do
        if apk search -x "$cand" >/dev/null 2>&1 && apk search -q -e "$cand" | grep -qx "$cand"; then
          PHP_VER="$cand"
          break
        fi
      done
      PHP_VER="${PHP_VER:-php83}"
      apk add --no-cache curl wget tar gzip unzip openssl python3 nginx sqlite \
        "$PHP_VER" "${PHP_VER}-fpm" "${PHP_VER}-xml" "${PHP_VER}-dom" "${PHP_VER}-mbstring" \
        "${PHP_VER}-intl" "${PHP_VER}-zip" "${PHP_VER}-gd" "${PHP_VER}-curl" \
        "${PHP_VER}-session" "${PHP_VER}-iconv" "${PHP_VER}-openssl" "${PHP_VER}-opcache" \
        "${PHP_VER}-pdo_sqlite" "${PHP_VER}-sqlite3" >/dev/null
      apk add --no-cache mariadb mariadb-client "${PHP_VER}-pdo_mysql" "${PHP_VER}-mysqli" >/dev/null 2>&1 || true
      ln -sf "/usr/bin/${PHP_VER}" /usr/bin/php 2>/dev/null || true
      ;;
    pacman)
      pacman -Sy --noconfirm --needed curl wget tar gzip unzip openssl python nginx sqlite \
        php php-fpm php-intl php-gd php-sqlite >/dev/null
      pacman -S --noconfirm --needed mariadb >/dev/null 2>&1 || true
      if [ -f /etc/php/php.ini ]; then
        sed -i 's/^;\(extension=pdo_mysql\)/\1/; s/^;\(extension=pdo_sqlite\)/\1/; s/^;\(extension=sqlite3\)/\1/; s/^;\(extension=intl\)/\1/; s/^;\(extension=iconv\)/\1/; s/^;\(extension=gd\)/\1/; s/^;\(zend_extension=opcache\)/\1/' /etc/php/php.ini || true
      fi
      ;;
  esac
}

log "Installing system dependencies..."
install_packages

# If executed via `curl ... | bash`, download official Vmail release bundle now (after curl/tar exist!)
if [ -z "$REPO_DIR" ]; then
  TMP_BUNDLE_DIR="$(mktemp -d)"
  log "Downloading Vmail ${VMAIL_VERSION} bundle from github.com/${VMAIL_REPO}..."
  REL_TAR="https://github.com/${VMAIL_REPO}/releases/download/${VMAIL_VERSION}/vmail-${VMAIL_VERSION}.tar.gz"
  MAIN_TAR="https://github.com/${VMAIL_REPO}/archive/refs/heads/main.tar.gz"
  if fetch_with_retry "$REL_TAR" "$TMP_BUNDLE_DIR/vmail.tar.gz"; then
    if fetch_with_retry "https://github.com/${VMAIL_REPO}/releases/download/${VMAIL_VERSION}/SHA256SUMS.txt" "$TMP_BUNDLE_DIR/SHA256SUMS.txt" 2>/dev/null; then
      EXP="$(grep -F "  vmail-${VMAIL_VERSION}.tar.gz" "$TMP_BUNDLE_DIR/SHA256SUMS.txt" | awk '{print $1}' || true)"
      if [ -n "$EXP" ] && command -v sha256sum >/dev/null 2>&1; then
        ACT="$(sha256sum "$TMP_BUNDLE_DIR/vmail.tar.gz" | awk '{print $1}')"
        [ "$EXP" = "$ACT" ] || { echo "[ERROR] Vmail bundle SHA256 mismatch!" >&2; exit 1; }
        ok "Vmail bundle SHA256 verified."
      fi
    fi
  else
    warn "Release tarball unavailable, falling back to latest main branch archive..."
    fetch_with_retry "$MAIN_TAR" "$TMP_BUNDLE_DIR/vmail.tar.gz"
  fi
  tar -xzf "$TMP_BUNDLE_DIR/vmail.tar.gz" -C "$TMP_BUNDLE_DIR"
  FOUND_SERVER="$(find "$TMP_BUNDLE_DIR" -maxdepth 4 -path '*/admin/app/server.py' | head -n 1 || true)"
  if [ -z "$FOUND_SERVER" ]; then
    echo "[ERROR] Failed to extract Vmail repository payload." >&2
    exit 1
  fi
  REPO_DIR="$(dirname "$(dirname "$(dirname "$FOUND_SERVER")")")"
  ok "Extracted Vmail source bundle -> ${REPO_DIR}"
fi

mkdir -p "$ADMIN_DIR/app" "$ADMIN_DIR/web" "$ADMIN_DIR/core" "$ADMIN_DIR/data" "$CFG_DIR" "$LOG_DIR" "$WEBMAIL_ROOT" "$WEBMAIL_DB_DIR"
chmod 700 "$CFG_DIR" "$ADMIN_DIR/data"

# Stop nginx temporarily if default site holds port 8080/80 during core bootstrap
if [ "$INIT_SYS" = "systemd" ]; then
  systemctl stop nginx >/dev/null 2>&1 || true
elif [ "$INIT_SYS" = "openrc" ]; then
  rc-service nginx stop >/dev/null 2>&1 || true
else
  service nginx stop >/dev/null 2>&1 || pkill -f 'nginx: master' >/dev/null 2>&1 || true
fi

# 1. Install Vmail Core Engine & Init Config
log "Setting up Vmail Core Engine..."
bash "$REPO_DIR/core/install-core.sh"

if [ ! -f "$CFG_DIR/inbound_bridge.secret" ]; then
  openssl rand -hex 24 > "$CFG_DIR/inbound_bridge.secret"
  chmod 600 "$CFG_DIR/inbound_bridge.secret"
fi
BRIDGE_SECRET="$(cat "$CFG_DIR/inbound_bridge.secret")"

if [ ! -f "$CFG_DIR/secret.key" ]; then
  openssl rand -hex 32 > "$CFG_DIR/secret.key"
  chmod 600 "$CFG_DIR/secret.key"
fi

PUBLIC_IP="$(curl -fsS --max-time 4 https://api.ipify.org 2>/dev/null || curl -fsS --max-time 4 https://ifconfig.me 2>/dev/null || true)"

cat > "$CFG_DIR/config.json" <<EOF
{
  "primary_domain": "${PRIMARY_DOMAIN}",
  "mail_host": "${MAIL_HOST}",
  "public_url": "https://${MAIL_HOST}",
  "admin_email": "${ADMIN_EMAIL}",
  "http_port": ${HTTP_PORT},
  "public_ip": "${PUBLIC_IP}"
}
EOF
chmod 600 "$CFG_DIR/config.json"

# 2. Deploy Vmail Admin Panel, Core Bootstrapper, Inbound Bridge & CLI
log "Deploying Vmail Control Panel, Inbound Bridge & CLI..."
cp -f "$REPO_DIR"/admin/app/*.py "$ADMIN_DIR/app/"
cp -f "$REPO_DIR"/admin/web/* "$ADMIN_DIR/web/"
cp -f "$REPO_DIR"/core/bootstrap-jmap.py "$ADMIN_DIR/core/bootstrap-jmap.py"
chmod 755 "$ADMIN_DIR/core/bootstrap-jmap.py"
install -m 755 "$REPO_DIR/bridge/mail_inbound_bridge.py" /usr/local/bin/mail_inbound_bridge.py
install -m 755 "$REPO_DIR/cli/vmail" /usr/local/bin/vmail

# 3. Install & Configure Service Definitions for Vmail Core BEFORE Nginx starts
install_systemd_units() {
  cp -f "$REPO_DIR"/systemd/vmail-*.service /etc/systemd/system/
  systemctl daemon-reload
}

install_openrc_units() {
  cat > /etc/init.d/vmail-core <<'EOF'
#!/sbin/openrc-run
name="vmail-core"
command="/usr/local/bin/vmail-core"
command_args="-c /etc/vmail/core-storage.json"
command_background="yes"
pidfile="/run/vmail-core.pid"
output_log="/var/log/vmail/core.out"
error_log="/var/log/vmail/core.out"
depend() { need net; }
EOF
  cat > /etc/init.d/vmail-bridge <<'EOF'
#!/sbin/openrc-run
name="vmail-bridge"
command="/usr/bin/python3"
command_args="-u /usr/local/bin/mail_inbound_bridge.py"
command_background="yes"
pidfile="/run/vmail-bridge.pid"
output_log="/var/log/vmail/bridge.log"
error_log="/var/log/vmail/bridge.log"
depend() { need net vmail-core; }
EOF
  cat > /etc/init.d/vmail-admin <<'EOF'
#!/sbin/openrc-run
name="vmail-admin"
command="/usr/bin/python3"
command_args="-u /opt/vmail/app/server.py"
command_background="yes"
pidfile="/run/vmail-admin.pid"
output_log="/var/log/vmail/vmail.log"
error_log="/var/log/vmail/vmail.log"
depend() { need net vmail-core; }
EOF
  chmod +x /etc/init.d/vmail-core /etc/init.d/vmail-bridge /etc/init.d/vmail-admin
}

install_standalone_supervisor() {
  cat > /usr/local/bin/vmail-services <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOG_DIR="/var/log/vmail"
CFG_DIR="/etc/vmail"
mkdir -p "$LOG_DIR"
start_all() {
  if ! pgrep -f "/usr/local/bin/[v]mail-core" >/dev/null 2>&1; then
    nohup /usr/local/bin/vmail-core -c "$CFG_DIR/core-storage.json" </dev/null >> "$LOG_DIR/core.out" 2>&1 &
  fi
  if ! pgrep -f "[m]ail_inbound_bridge.py" >/dev/null 2>&1; then
    nohup python3 -u /usr/local/bin/mail_inbound_bridge.py </dev/null >> "$LOG_DIR/bridge.log" 2>&1 &
  fi
  if ! pgrep -f "/opt/vmail/app/[s]erver.py" >/dev/null 2>&1; then
    nohup python3 -u /opt/vmail/app/server.py </dev/null >> "$LOG_DIR/vmail.log" 2>&1 &
  fi
  service mariadb start >/dev/null 2>&1 || true
  service nginx start >/dev/null 2>&1 || true
  find /etc/init.d -maxdepth 1 -name 'php*-fpm' -exec {} start \; >/dev/null 2>&1 || service php-fpm start >/dev/null 2>&1 || true
}
stop_all() {
  pkill -f '/opt/vmail/app/[s]erver.py' >/dev/null 2>&1 || true
  pkill -f '[m]ail_inbound_bridge.py' >/dev/null 2>&1 || true
  pkill -f '/usr/local/bin/[v]mail-core' >/dev/null 2>&1 || true
}
case "${1:-start}" in
  start) start_all ;;
  stop) stop_all ;;
  restart) stop_all; sleep 1; start_all ;;
  *) echo "Usage: vmail-services {start|stop|restart}" >&2; exit 1 ;;
esac
EOF
  chmod 755 /usr/local/bin/vmail-services
  if command -v crontab >/dev/null 2>&1; then
    (crontab -l 2>/dev/null | grep -v 'vmail-services' || true; echo "@reboot /usr/local/bin/vmail-services start >/dev/null 2>&1") | crontab - || true
  fi
}

if [ "$INIT_SYS" = "systemd" ]; then
  install_systemd_units
  systemctl enable vmail-core >/dev/null 2>&1 || true
  systemctl restart vmail-core
elif [ "$INIT_SYS" = "openrc" ]; then
  install_openrc_units
  rc-update add vmail-core default >/dev/null 2>&1 || true
  rc-service vmail-core restart
else
  install_standalone_supervisor
  if ! pgrep -f "/usr/local/bin/[v]mail-core" >/dev/null 2>&1; then
    nohup /usr/local/bin/vmail-core -c "$CFG_DIR/core-storage.json" </dev/null > "$LOG_DIR/core.out" 2>&1 &
  fi
fi

# Wait for Vmail Core Engine JMAP listener on either 8090 (configured) or 8080 (fresh bootstrap mode)
log "Waiting for Vmail Core Engine JMAP API..."
CORE_WAIT_OK="0"
for _ in $(seq 1 45); do
  c8090="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/jmap/session 2>/dev/null || echo 000)"
  c8080="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/jmap/session 2>/dev/null || echo 000)"
  if [ "$c8090" = "200" ] || [ "$c8090" = "401" ] || [ "$c8080" = "200" ] || [ "$c8080" = "401" ]; then
    CORE_WAIT_OK="1"
    break
  fi
  sleep 1
done

if [ "$CORE_WAIT_OK" != "1" ]; then
  warn "Core JMAP endpoint did not respond within 45s. Check $LOG_DIR/core.out"
fi

# Parse fresh temporary bootstrap password cleanly from core stdout
BOOT_USER="admin"
BOOT_PASS=""
if [ -f "$LOG_DIR/core.out" ]; then
  BOOT_PASS="$(python3 - <<'PY'
import os, re
p = "/var/log/vmail/core.out"
if os.path.exists(p):
    raw = open(p, "r", encoding="utf-8", errors="replace").read()
    clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    m = re.findall(r"username:\s*admin\s+password:\s*([^\s\r\n]+)", clean)
    if m:
        print(m[-1].strip())
PY
  )"
fi

RELAY_FLAGS=()
if [ -n "$RELAY_HOST" ]; then
  RELAY_FLAGS+=(--relay-host "$RELAY_HOST" --relay-port "$RELAY_PORT" --relay-user "$RELAY_USER" --relay-secret "$RELAY_PASS")
  if [ "$RELAY_TLS" = "1" ]; then
    RELAY_FLAGS+=(--relay-tls)
  fi
fi

# Bootstrap domain, listeners (frees 8080, binds 8090), admin account, ESMTP greeting, Anti-Spam & Relay
log "Bootstrapping Vmail Core Engine via JMAP..."
python3 "$REPO_DIR/core/bootstrap-jmap.py" \
  --domain "$PRIMARY_DOMAIN" \
  --mail-host "$MAIL_HOST" \
  --boot-user "$BOOT_USER" \
  --boot-pass "${BOOT_PASS:-$ADMIN_PASS}" \
  --admin-pass "$ADMIN_PASS" \
  --admin-email "$ADMIN_EMAIL" \
  "${RELAY_FLAGS[@]}" || warn "Bootstrap returned warning — verify /etc/vmail/engine.json if core was previously initialized with a custom admin password."

# 4. Install Roundcube + Gmail Skin & Initialize DB (MariaDB with auto SQLite fallback)
log "Setting up Vmail Gmail-style Webmail (Roundcube ${ROUNDCUBE_VERSION})..."
if [ ! -f "$WEBMAIL_ROOT/index.php" ]; then
  TMP_RC="$(mktemp -d)"
  fetch_with_retry \
    "https://github.com/roundcube/roundcubemail/releases/download/${ROUNDCUBE_VERSION}/roundcubemail-${ROUNDCUBE_VERSION}-complete.tar.gz" \
    "$TMP_RC/rc.tar.gz"
  tar -xzf "$TMP_RC/rc.tar.gz" -C "$TMP_RC"
  cp -a "$TMP_RC/roundcubemail-${ROUNDCUBE_VERSION}/." "$WEBMAIL_ROOT/"
  rm -rf "$TMP_RC"
fi
rm -rf "$WEBMAIL_ROOT/installer"
mkdir -p "$WEBMAIL_ROOT/skins/gmail"
cp -a "$REPO_DIR/webmail/skins/gmail/." "$WEBMAIL_ROOT/skins/gmail/"

# Pre-compress static webmail CSS/JS/SVG for zero-CPU Nginx gzip_static delivery
log "Pre-compressing static Webmail assets (gzip_static)..."
find "$WEBMAIL_ROOT/skins/gmail" "$WEBMAIL_ROOT/program/js" -type f \( -name '*.css' -o -name '*.js' -o -name '*.svg' \) -size +256c -exec gzip -9 -k -f {} \; 2>/dev/null || true

start_mariadb_if_possible() {
  if ! command -v mysql >/dev/null 2>&1 && ! command -v mariadb >/dev/null 2>&1; then
    return 1
  fi
  if [ ! -d /var/lib/mysql/mysql ]; then
    if command -v mariadb-install-db >/dev/null 2>&1; then
      mariadb-install-db --user=mysql --datadir=/var/lib/mysql >/dev/null 2>&1 || true
    elif command -v mysql_install_db >/dev/null 2>&1; then
      mysql_install_db --user=mysql --datadir=/var/lib/mysql >/dev/null 2>&1 || true
    fi
  fi
  if [ "$INIT_SYS" = "systemd" ]; then
    systemctl enable mariadb >/dev/null 2>&1 || true
    systemctl start mariadb >/dev/null 2>&1 || systemctl start mysqld >/dev/null 2>&1 || true
  elif [ "$INIT_SYS" = "openrc" ]; then
    rc-update add mariadb default >/dev/null 2>&1 || true
    rc-service mariadb start >/dev/null 2>&1 || true
  else
    service mariadb start >/dev/null 2>&1 || service mysql start >/dev/null 2>&1 || true
  fi
  local cli=""
  if command -v mariadb >/dev/null 2>&1; then cli="mariadb"; else cli="mysql"; fi
  "$cli" -e "SELECT 1;" >/dev/null 2>&1
}

DB_DSN=""
DES_KEY="$(openssl rand -hex 12)"
if start_mariadb_if_possible; then
  log "Initializing Roundcube database on MariaDB..."
  DB_CLI="mysql"
  command -v mariadb >/dev/null 2>&1 && DB_CLI="mariadb"
  RC_DB_NAME="vmail_webmail"
  RC_DB_USER="vmail_rc"
  RC_DB_PASS="$(openssl rand -hex 16)"
  if "$DB_CLI" -e "CREATE DATABASE IF NOT EXISTS \`${RC_DB_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
    CREATE USER IF NOT EXISTS '${RC_DB_USER}'@'localhost' IDENTIFIED BY '${RC_DB_PASS}';
    ALTER USER '${RC_DB_USER}'@'localhost' IDENTIFIED BY '${RC_DB_PASS}';
    GRANT ALL PRIVILEGES ON \`${RC_DB_NAME}\`.* TO '${RC_DB_USER}'@'localhost'; FLUSH PRIVILEGES;" >/dev/null 2>&1; then
    TABLE_CNT="$("$DB_CLI" -N -s -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${RC_DB_NAME}';" 2>/dev/null || echo 0)"
    if [ "${TABLE_CNT:-0}" = "0" ] && [ -f "$WEBMAIL_ROOT/SQL/mysql.initial.sql" ]; then
      "$DB_CLI" "${RC_DB_NAME}" < "$WEBMAIL_ROOT/SQL/mysql.initial.sql" >/dev/null 2>&1 || true
    fi
    DB_DSN="mysql://${RC_DB_USER}:${RC_DB_PASS}@localhost/${RC_DB_NAME}"
  fi
fi

if [ -z "$DB_DSN" ]; then
  warn "MariaDB unavailable — automatically activating zero-overhead SQLite backend for Roundcube..."
  SQLITE_FILE="${WEBMAIL_DB_DIR}/roundcube.db"
  if [ ! -s "$SQLITE_FILE" ] && [ -f "$WEBMAIL_ROOT/SQL/sqlite.initial.sql" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$SQLITE_FILE" < "$WEBMAIL_ROOT/SQL/sqlite.initial.sql"
    else
      python3 - <<PY
import sqlite3
c = sqlite3.connect("${SQLITE_FILE}")
c.executescript(open("${WEBMAIL_ROOT}/SQL/sqlite.initial.sql", encoding="utf-8").read())
c.commit()
c.close()
PY
    fi
  fi
  DB_DSN="sqlite:////${SQLITE_FILE#/}?mode=0640"
fi

python3 - <<PY
tpl = open("${REPO_DIR}/webmail/config.inc.php.template", "r", encoding="utf-8").read()
out = tpl.replace("{{DB_DSN}}", "${DB_DSN}").replace("{{DES_KEY}}", "${DES_KEY}")
open("${WEBMAIL_ROOT}/config/config.inc.php", "w", encoding="utf-8").write(out)
PY

# 5. Tune PHP 8.x OPcache (keep JIT disabled for full PRoot/container/SELinux W^X stability)
tune_php_perf() {
  local ini_content
  ini_content="; Vmail High-Performance PHP Tuning
opcache.enable=1
opcache.memory_consumption=128
opcache.interned_strings_buffer=16
opcache.max_accelerated_files=10000
opcache.revalidate_freq=60
opcache.validate_timestamps=1
opcache.enable_file_override=1
opcache.jit=disable
opcache.jit_buffer_size=0
realpath_cache_size=4096K
realpath_cache_ttl=600
error_reporting=E_ALL & ~E_DEPRECATED & ~E_NOTICE
display_errors=Off
"
  for d in /etc/php/*/fpm/conf.d /etc/php.d /etc/php*/conf.d /etc/php8/conf.d; do
    if [ -d "$d" ]; then
      printf '%s' "$ini_content" > "${d}/99-vmail-perf.ini" 2>/dev/null || true
    fi
  done
}

log "Tuning PHP OPcache (safe mode without W^X JIT)..."
tune_php_perf

# Start PHP-FPM & Auto-Detect Socket + Web User across all distros
start_php_fpm() {
  if [ "$INIT_SYS" = "systemd" ]; then
    for unit in php8.5-fpm php8.4-fpm php8.3-fpm php8.2-fpm php8.1-fpm php8.0-fpm php-fpm; do
      if systemctl list-unit-files "${unit}.service" >/dev/null 2>&1; then
        systemctl enable "$unit" >/dev/null 2>&1 || true
        systemctl restart "$unit" >/dev/null 2>&1 || true
      fi
    done
  elif [ "$INIT_SYS" = "openrc" ]; then
    for svc in php-fpm85 php-fpm84 php-fpm83 php-fpm82 php-fpm81 php-fpm; do
      if [ -x "/etc/init.d/${svc}" ]; then
        rc-update add "$svc" default >/dev/null 2>&1 || true
        rc-service "$svc" restart >/dev/null 2>&1 || true
      fi
    done
  else
    find /etc/init.d -maxdepth 1 -name 'php*-fpm' -exec {} restart \; >/dev/null 2>&1 || service php-fpm restart >/dev/null 2>&1 || true
  fi
}

start_php_fpm
sleep 1

# Detect web/php runtime user
WEB_USER="www-data"
for cand_u in www-data nginx http apache wwwrun _www; do
  if id -u "$cand_u" >/dev/null 2>&1; then
    if [ -f /etc/nginx/nginx.conf ] && grep -Eq "^\s*user\s+${cand_u}\b" /etc/nginx/nginx.conf; then
      WEB_USER="$cand_u"
      break
    fi
  fi
done
if ! id -u "$WEB_USER" >/dev/null 2>&1; then
  for cand_u in www-data nginx http apache wwwrun nobody; do
    if id -u "$cand_u" >/dev/null 2>&1; then WEB_USER="$cand_u"; break; fi
  done
fi

chmod 640 "$WEBMAIL_ROOT/config/config.inc.php"
chown -R "${WEB_USER}:${WEB_USER}" "$WEBMAIL_ROOT" "$WEBMAIL_DB_DIR"
chmod -R 750 "$WEBMAIL_DB_DIR"

# Find active PHP-FPM socket or TCP listener
PHP_SOCK="$(find /run /var/run -maxdepth 3 \( -name 'php*-fpm.sock' -o -name 'www.sock' -o -name 'php-fpm.sock' \) 2>/dev/null | head -n 1 || true)"
if [ -n "$PHP_SOCK" ]; then
  PHP_FPM_TARGET="unix:${PHP_SOCK}"
  chmod 660 "$PHP_SOCK" 2>/dev/null || true
  chgrp "$WEB_USER" "$PHP_SOCK" 2>/dev/null || true
else
  PHP_FPM_TARGET="127.0.0.1:9000"
fi

# Choose Nginx conf directory (/etc/nginx/http.d on Alpine, /etc/nginx/conf.d elsewhere)
NGINX_CONF_DIR="/etc/nginx/conf.d"
if [ -d /etc/nginx/http.d ]; then
  NGINX_CONF_DIR="/etc/nginx/http.d"
fi
mkdir -p "$NGINX_CONF_DIR"

python3 - <<PY
tpl = open("${REPO_DIR}/nginx/vmail.conf.template", "r", encoding="utf-8").read()
out = (
    tpl.replace("{{HTTP_PORT}}", "${HTTP_PORT}")
       .replace("{{MAIL_HOST}}", "${MAIL_HOST}")
       .replace("{{WEBMAIL_ROOT}}", "${WEBMAIL_ROOT}")
       .replace("{{PHP_FPM_SOCK}}", "${PHP_FPM_TARGET}")
)
open("${NGINX_CONF_DIR}/vmail.conf", "w", encoding="utf-8").write(out)
PY

rm -f /etc/nginx/sites-enabled/default "${NGINX_CONF_DIR}/default.conf" 2>/dev/null || true

# Self-heal Nginx config if an older distro lacks http_gzip_static_module
if ! nginx -t >/dev/null 2>&1; then
  warn "Nginx test failed — attempting automatic compatibility healing..."
  sed -i '/gzip_static/d' "${NGINX_CONF_DIR}/vmail.conf" 2>/dev/null || true
  nginx -t
fi

# Self-heal SELinux & Firewall on RHEL/Rocky/Alma/Fedora/openSUSE
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null || true)" = "Enforcing" ]; then
  log "Applying SELinux policies for Nginx & Webmail..."
  command -v setsebool >/dev/null 2>&1 && setsebool -P httpd_can_network_connect 1 >/dev/null 2>&1 || true
  if command -v chcon >/dev/null 2>&1; then
    chcon -R -t httpd_sys_content_t "$WEBMAIL_ROOT" >/dev/null 2>&1 || true
    chcon -R -t httpd_sys_rw_content_t "$WEBMAIL_ROOT/temp" "$WEBMAIL_ROOT/logs" "$WEBMAIL_DB_DIR" >/dev/null 2>&1 || true
  fi
fi

if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  log "Opening mail & web ports in firewalld..."
  for p in "${HTTP_PORT}/tcp" "25/tcp" "80/tcp" "443/tcp" "465/tcp" "587/tcp" "993/tcp"; do
    firewall-cmd --permanent --add-port="$p" >/dev/null 2>&1 || true
  done
  firewall-cmd --reload >/dev/null 2>&1 || true
fi

# 6. Enable & Start All Services
if [ "$INIT_SYS" = "systemd" ]; then
  systemctl enable vmail-admin vmail-bridge nginx >/dev/null 2>&1 || true
  systemctl restart vmail-admin vmail-bridge nginx
elif [ "$INIT_SYS" = "openrc" ]; then
  rc-update add vmail-admin default >/dev/null 2>&1 || true
  rc-update add vmail-bridge default >/dev/null 2>&1 || true
  rc-update add nginx default >/dev/null 2>&1 || true
  rc-service vmail-admin restart
  rc-service vmail-bridge restart
  rc-service nginx restart
else
  service nginx restart || nginx
  pkill -f '[m]ail_inbound_bridge.py' >/dev/null 2>&1 || true
  pkill -f '/opt/vmail/app/[s]erver.py' >/dev/null 2>&1 || true
  nohup python3 -u /usr/local/bin/mail_inbound_bridge.py </dev/null > "$LOG_DIR/bridge.log" 2>&1 &
  nohup python3 -u /opt/vmail/app/server.py </dev/null > "$LOG_DIR/vmail.log" 2>&1 &
fi

# 7. Optional Cloudflare DNS + Tunnel + Inbound Worker Automation
if [ -n "$CF_EMAIL" ] && [ -n "$CF_KEY" ]; then
  log "Configuring Cloudflare DNS, Multi-Domain Webmail & Inbound Worker..."
  python3 - <<PY
import sys
sys.path.insert(0, "${ADMIN_DIR}/app")
import dns_cf
dns_cf.save_cf_creds("${CF_EMAIL}", "${CF_KEY}", mail_host="${MAIL_HOST}", inbound_worker="vmail-inbound")
w_res = dns_cf.deploy_inbound_worker("https://${MAIL_HOST}", "${BRIDGE_SECRET}", "vmail-inbound")
print("[Cloudflare Worker]", w_res)
sync_res = dns_cf.sync_all_cloudflare()
print("[Cloudflare Sync] Total zones:", sync_res.get("total_cf_zones"), "Added domains:", sync_res.get("added_domains"))
PY
fi

if [ -n "$TUNNEL_TOKEN" ] && ! command -v cloudflared >/dev/null 2>&1; then
  log "Installing cloudflared tunnel daemon..."
  case "$(uname -m)" in
    x86_64|amd64) CF_ARCH="amd64" ;;
    aarch64|arm64) CF_ARCH="arm64" ;;
    armv7l|armhf) CF_ARCH="arm" ;;
    *) CF_ARCH="" ;;
  esac
  if [ -n "$CF_ARCH" ]; then
    fetch_with_retry "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" /usr/local/bin/cloudflared
    chmod 755 /usr/local/bin/cloudflared
    cloudflared service install "$TUNNEL_TOKEN" >/dev/null 2>&1 || true
  fi
fi

# 8. Built-in Post-Install Self-Verification & Auto-Heal
log "Verifying live service endpoints..."
sleep 2
VERIFY_OK="1"
for url in "http://127.0.0.1:8096/vmail-api/v1/health" "http://127.0.0.1:${HTTP_PORT}/vmail-api/v1/health"; do
  if curl -fsS --max-time 5 -H "Host: ${MAIL_HOST}" "$url" | grep -q '"ok":true\|"ok": true'; then
    ok "Health OK -> $url"
  else
    warn "Check failed -> $url"
    VERIFY_OK="0"
  fi
done
if [ "$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${MAIL_HOST}" "http://127.0.0.1:${HTTP_PORT}/" || echo 000)" = "200" ]; then
  ok "Webmail Nginx+PHP-FPM OK -> http://127.0.0.1:${HTTP_PORT}/ (Host: ${MAIL_HOST})"
else
  warn "Webmail PHP check returned non-200 on http://127.0.0.1:${HTTP_PORT}/"
  VERIFY_OK="0"
fi
if [ "$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${MAIL_HOST}" "http://127.0.0.1:${HTTP_PORT}/admin/" || echo 000)" = "200" ]; then
  ok "Admin Console OK -> http://127.0.0.1:${HTTP_PORT}/admin/"
else
  warn "Admin Console check returned non-200"
  VERIFY_OK="0"
fi

if [ "$VERIFY_OK" != "1" ]; then
  warn "Running automatic self-healing via vmail doctor --fix..."
  vmail doctor --fix || true
fi

echo
echo "=================================================================="
if [ "$VERIFY_OK" = "1" ]; then
  echo "  Vmail Mail Server v1.2.1 Installation Verified & Ready!"
else
  echo "  Vmail Installed (check status with: vmail doctor)"
fi
echo "=================================================================="
echo "  Webmail (Gmail UI):   https://${MAIL_HOST}/"
echo "  Admin Console:        https://${MAIL_HOST}/admin/"
echo "  Transactional API:    https://${MAIL_HOST}/vmail-api/v1/send"
echo "  Admin Mailbox:        ${ADMIN_EMAIL}"
if [ "$GEN_PASS_SHOWN" = "1" ]; then
  echo "  Admin Password:       ${ADMIN_PASS}   (Save this!)"
fi
echo "  CLI Management:       vmail status | doctor --fix | domains | accounts | sync | test"
echo "=================================================================="
