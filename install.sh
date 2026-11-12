#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Vmail Mail Server & Gmail-Style Webmail — Automated Installer
# Lightweight, high-speed, zero-license, multi-domain, Cloudflare-ready
# OS Supported: Ubuntu 22.04/24.04, Debian 11/12 (VPS & Container/PRoot)
# ==============================================================================

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUNDCUBE_VERSION="${VMAIL_ROUNDCUBE_VERSION:-1.6.9}"
WEBMAIL_ROOT="/var/www/vmail-webmail"
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
Vmail Installer Options:
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
  --http-port <port>          Nginx HTTP listen port (default: 8080, use 80 if standalone)
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

log() { printf '\033[1;34m[Vmail]\033[0m %s\n' "$*"; }

# Interactive prompts when missing
if [ "$NON_INTERACTIVE" != "1" ]; then
  if [ -z "$PRIMARY_DOMAIN" ]; then
    read -rp "Primary Mail Domain (e.g. example.com): " PRIMARY_DOMAIN
  fi
  if [ -z "$MAIL_HOST" ] && [ -n "$PRIMARY_DOMAIN" ]; then
    read -rp "Webmail Hostname [mail.${PRIMARY_DOMAIN}]: " MAIL_HOST
  fi
  if [ -z "$ADMIN_EMAIL" ] && [ -n "$PRIMARY_DOMAIN" ]; then
    read -rp "Admin Email [admin@${PRIMARY_DOMAIN}]: " ADMIN_EMAIL
  fi
  if [ -z "$ADMIN_PASS" ]; then
    read -rsp "Admin Password (Enter to auto-generate): " ADMIN_PASS
    echo
  fi
  if [ -z "$CF_EMAIL" ]; then
    read -rp "Cloudflare Account Email (Enter to skip now): " CF_EMAIL
    if [ -n "$CF_EMAIL" ] && [ -z "$CF_KEY" ]; then
      read -rsp "Cloudflare Global API Key: " CF_KEY
      echo
    fi
  fi
fi

if [ -z "$PRIMARY_DOMAIN" ]; then
  echo "[ERROR] Primary domain is required (--domain example.com)." >&2
  exit 1
fi

PRIMARY_DOMAIN="$(echo "$PRIMARY_DOMAIN" | tr '[:upper:]' '[:lower:]' | xargs)"
MAIL_HOST="${MAIL_HOST:-mail.${PRIMARY_DOMAIN}}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@${PRIMARY_DOMAIN}}"
if [ -z "$ADMIN_PASS" ]; then
  ADMIN_PASS="$(tr -dc 'A-HJ-KM-Za-km-z2-9@#%' </dev/urandom | head -c 16)"
  GEN_PASS_SHOWN="1"
else
  GEN_PASS_SHOWN="0"
fi

log "Installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl wget tar gzip unzip openssl python3 nginx mariadb-server \
  php-fpm php-mysql php-xml php-mbstring php-intl php-zip php-gd php-curl php-imagick >/dev/null

mkdir -p "$ADMIN_DIR/app" "$ADMIN_DIR/web" "$ADMIN_DIR/data" "$CFG_DIR" "$LOG_DIR" "$WEBMAIL_ROOT"
chmod 700 "$CFG_DIR" "$ADMIN_DIR/data"

# 1. Install Vmail Core Engine & Init Admin Credentials
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

cat > "$CFG_DIR/config.json" <<EOF
{
  "primary_domain": "${PRIMARY_DOMAIN}",
  "mail_host": "${MAIL_HOST}",
  "public_url": "https://${MAIL_HOST}",
  "admin_email": "${ADMIN_EMAIL}"
}
EOF
chmod 600 "$CFG_DIR/config.json"

# 2. Deploy Vmail Admin Panel, Inbound Bridge & CLI
log "Deploying Vmail Control Panel, Inbound Bridge & CLI..."
cp -f "$REPO_DIR"/admin/app/*.py "$ADMIN_DIR/app/"
cp -f "$REPO_DIR"/admin/web/* "$ADMIN_DIR/web/"
install -m 755 "$REPO_DIR/bridge/mail_inbound_bridge.py" /usr/local/bin/mail_inbound_bridge.py
install -m 755 "$REPO_DIR/cli/vmail" /usr/local/bin/vmail

# 3. Install Roundcube + Vmail Gmail UI Skin + MariaDB
log "Configuring MariaDB & Roundcube Webmail (Gmail UI)..."
service mariadb start >/dev/null 2>&1 || true
RC_DB_NAME="vmail_webmail"
RC_DB_USER="vmail_rc"
RC_DB_PASS="$(openssl rand -hex 16)"
DES_KEY="$(openssl rand -hex 12)"

mysql -e "CREATE DATABASE IF NOT EXISTS \`${RC_DB_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -e "CREATE USER IF NOT EXISTS '${RC_DB_USER}'@'localhost' IDENTIFIED BY '${RC_DB_PASS}';"
mysql -e "ALTER USER '${RC_DB_USER}'@'localhost' IDENTIFIED BY '${RC_DB_PASS}';"
mysql -e "GRANT ALL PRIVILEGES ON \`${RC_DB_NAME}\`.* TO '${RC_DB_USER}'@'localhost'; FLUSH PRIVILEGES;"

if [ ! -f "$WEBMAIL_ROOT/index.php" ]; then
  TMP_RC="$(mktemp -d)"
  curl -fsSL "https://github.com/roundcube/roundcubemail/releases/download/${ROUNDCUBE_VERSION}/roundcubemail-${ROUNDCUBE_VERSION}-complete.tar.gz" -o "$TMP_RC/rc.tar.gz"
  tar -xzf "$TMP_RC/rc.tar.gz" -C "$TMP_RC"
  cp -a "$TMP_RC/roundcubemail-${ROUNDCUBE_VERSION}/." "$WEBMAIL_ROOT/"
  rm -rf "$TMP_RC"
  mysql "${RC_DB_NAME}" < "$WEBMAIL_ROOT/SQL/mysql.initial.sql" 2>/dev/null || true
fi

rm -rf "$WEBMAIL_ROOT/installer"
mkdir -p "$WEBMAIL_ROOT/skins/gmail"
cp -a "$REPO_DIR/webmail/skins/gmail/." "$WEBMAIL_ROOT/skins/gmail/"

python3 - <<PY
tpl = open("${REPO_DIR}/webmail/config.inc.php.template", "r", encoding="utf-8").read()
out = tpl.replace("{{DB_DSN}}", "mysql://${RC_DB_USER}:${RC_DB_PASS}@localhost/${RC_DB_NAME}").replace("{{DES_KEY}}", "${DES_KEY}")
open("${WEBMAIL_ROOT}/config/config.inc.php", "w", encoding="utf-8").write(out)
PY
chmod 640 "$WEBMAIL_ROOT/config/config.inc.php"
chown -R www-data:www-data "$WEBMAIL_ROOT"

# 4. Configure Nginx Virtual Host (Multi-Domain ^mail\..+$)
PHP_SOCK="$(find /run/php -name 'php*-fpm.sock' 2>/dev/null | head -n 1 || true)"
if [ -n "$PHP_SOCK" ]; then
  PHP_FPM_TARGET="unix:${PHP_SOCK}"
else
  PHP_FPM_TARGET="127.0.0.1:9000"
fi

python3 - <<PY
tpl = open("${REPO_DIR}/nginx/vmail.conf.template", "r", encoding="utf-8").read()
out = (
    tpl.replace("{{HTTP_PORT}}", "${HTTP_PORT}")
       .replace("{{MAIL_HOST}}", "${MAIL_HOST}")
       .replace("{{WEBMAIL_ROOT}}", "${WEBMAIL_ROOT}")
       .replace("{{PHP_FPM_SOCK}}", "${PHP_FPM_TARGET}")
)
open("/etc/nginx/conf.d/vmail.conf", "w", encoding="utf-8").write(out)
PY
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
nginx -t

# 5. Start Services (systemd or direct background fallback)
HAS_SYSTEMD="0"
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  HAS_SYSTEMD="1"
fi

if [ "$HAS_SYSTEMD" = "1" ]; then
  log "Installing and enabling systemd services..."
  cp -f "$REPO_DIR"/systemd/vmail-*.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable vmail-core >/dev/null
  systemctl restart vmail-core
else
  log "Starting Vmail Core Engine..."
  if ! pgrep -f "/usr/local/bin/vmail-core" >/dev/null 2>&1; then
    nohup /usr/local/bin/vmail-core -c "$CFG_DIR/core-storage.json" > "$LOG_DIR/core.out" 2>&1 &
  fi
fi

# Wait for Vmail Core HTTP/JMAP port 8090
log "Waiting for Vmail Core Engine on 127.0.0.1:8090..."
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8090/jmap/session >/dev/null 2>&1 || [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/jmap/session || echo 000)" = "401" ]; then
    break
  fi
  sleep 1
done

# Detect or seed core fallback credentials on first install
BOOT_USER="admin"
BOOT_PASS="$(grep -oE 'password[^"]*"([^"]+)"' "$LOG_DIR/core.out" 2>/dev/null | tail -n 1 | sed -E 's/.*"([^"]+)"/\1/' || true)"
if [ -x /usr/local/bin/vmail-core-cli ] && [ -n "$BOOT_PASS" ]; then
  /usr/local/bin/vmail-core-cli -u http://127.0.0.1:8090 --credentials "admin:${BOOT_PASS}" server init-config >/dev/null 2>&1 || true
fi

# Bootstrap domain, admin account, Vmail ESMTP greeting, anti-spam & optional relay
RELAY_FLAGS=()
if [ -n "$RELAY_HOST" ]; then
  RELAY_FLAGS+=(--relay-host "$RELAY_HOST" --relay-port "$RELAY_PORT" --relay-user "$RELAY_USER" --relay-secret "$RELAY_PASS")
  if [ "$RELAY_TLS" = "1" ]; then
    RELAY_FLAGS+=(--relay-tls)
  fi
fi

python3 "$REPO_DIR/core/bootstrap-jmap.py" \
  --domain "$PRIMARY_DOMAIN" \
  --mail-host "$MAIL_HOST" \
  --admin-user "$BOOT_USER" \
  --admin-pass "${BOOT_PASS:-$ADMIN_PASS}" \
  --admin-email "$ADMIN_EMAIL" \
  "${RELAY_FLAGS[@]}" || echo "[WARN] If engine used an auto-generated bootstrap password, log in with it in /admin/ once to save credentials."

# Start Vmail Admin & Inbound Bridge & Reload Nginx
if [ "$HAS_SYSTEMD" = "1" ]; then
  systemctl enable vmail-admin vmail-bridge nginx php*-fpm >/dev/null 2>&1 || true
  systemctl restart vmail-admin vmail-bridge nginx
else
  service nginx restart || nginx -s reload
  pkill -f '[m]ail_inbound_bridge.py' >/dev/null 2>&1 || true
  pkill -f '/opt/vmail/app/[s]erver.py' >/dev/null 2>&1 || true
  nohup python3 -u /usr/local/bin/mail_inbound_bridge.py </dev/null > "$LOG_DIR/bridge.log" 2>&1 &
  nohup python3 -u /opt/vmail/app/server.py </dev/null > "$LOG_DIR/vmail.log" 2>&1 &
fi

# 6. Optional Cloudflare DNS + Tunnel + Inbound Worker Automation
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
  CF_ARCH="$(dpkg --print-architecture)"
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" -o /usr/local/bin/cloudflared
  chmod 755 /usr/local/bin/cloudflared
  cloudflared service install "$TUNNEL_TOKEN" >/dev/null 2>&1 || true
fi

echo
echo "=================================================================="
echo "  Vmail Mail Server Installation Complete!"
echo "=================================================================="
echo "  Webmail (Gmail UI):   https://${MAIL_HOST}/"
echo "  Admin Console:        https://${MAIL_HOST}/admin/"
echo "  Transactional API:    https://${MAIL_HOST}/vmail-api/v1/send"
echo "  Admin Mailbox:        ${ADMIN_EMAIL}"
if [ "$GEN_PASS_SHOWN" = "1" ]; then
  echo "  Admin Password:       ${ADMIN_PASS}   (Save this!)"
fi
echo "  CLI Management:       vmail status | domains | accounts | sync | test"
echo "=================================================================="
