import datetime
import hashlib
import hmac
import html
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.realpath(os.path.join(APP_DIR, "..", "web"))
sys.path.insert(0, APP_DIR)

import db
import dns_cf
import jmap_client
import log_parser
import mailer

HOST = os.environ.get("VMAIL_ADMIN_HOST", "127.0.0.1")
PORT = int(os.environ.get("VMAIL_ADMIN_PORT", "8096"))
SESSION_TTL = 86400 * 3
MAX_LOGIN_FAILS = 8
LOGIN_WINDOW_SEC = 600
MAX_SESSIONS = 500
_SESSIONS = {}
_FAILED_LOGINS = {}
_SESS_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"^[^@\s\r\n<>\"'\\]+@[^@\s\r\n<>\"'\\]+\.[a-zA-Z0-9-]{2,}$")
_DOMAIN_RE = re.compile(r"^(?=.{3,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

TRANSPARENT_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def _sign_token(sid: str) -> str:
    sig = hmac.new(db.get_master_secret(), sid.encode("ascii"), hashlib.sha256).hexdigest()[:24]
    return f"{sid}.{sig}"


def _verify_token(cookie_val: str):
    if not cookie_val or "." not in cookie_val:
        return None
    sid, sig = cookie_val.rsplit(".", 1)
    expected = hmac.new(db.get_master_secret(), sid.encode("ascii"), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, expected):
        return None
    with _SESS_LOCK:
        sess = _SESSIONS.get(sid)
        if not sess:
            return None
        if sess["exp"] < time.time():
            _SESSIONS.pop(sid, None)
            return None
        return sess


def _ip_allowed(client_ip: str, rules: list) -> bool:
    if not rules:
        return True
    try:
        ip_obj = ipaddress.ip_address(client_ip.strip())
    except Exception:
        return client_ip in rules
    for r in rules:
        r = r.strip()
        if not r:
            continue
        try:
            if "/" in r:
                if ip_obj in ipaddress.ip_network(r, strict=False):
                    return True
            elif ip_obj == ipaddress.ip_address(r):
                return True
        except Exception:
            if client_ip == r:
                return True
    return False


class VmailHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Vmail"
    sys_version = ""

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(30.0)
        except Exception:
            pass

    def log_message(self, format, *args):
        pass

    def _client_ip(self) -> str:
        xff = (self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return xff or self.client_address[0]

    def _is_https(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
        return proto == "https"

    def _check_origin(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip().lower()
        if not host:
            return True
        try:
            origin_host = urllib.parse.urlparse(origin).netloc.lower()
            return origin_host == host
        except Exception:
            return False

    def _send_common_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, code: int, obj: dict, extra_headers=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self._send_common_security_headers()
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            if not getattr(self, "_head_only", False):
                self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_bytes(self, code: int, ctype: str, data: bytes, cache: str = "no-cache"):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self._send_common_security_headers()
            self.end_headers()
            if not getattr(self, "_head_only", False):
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _read_json(self):
        raw_len = (self.headers.get("Content-Length") or "0").strip()
        try:
            ln = int(raw_len)
        except ValueError:
            raise ValueError("Content-Length không hợp lệ.")
        if ln <= 0:
            return {}
        if ln > 8 * 1024 * 1024:
            raise ValueError("Payload quá lớn")
        raw = self.rfile.read(ln).decode("utf-8", errors="strict")
        if not raw.strip():
            return {}
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("Dữ liệu JSON không hợp lệ.")
        if not isinstance(obj, dict):
            raise ValueError("Payload JSON phải là object.")
        return obj

    def _get_session(self):
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return None
        c = SimpleCookie()
        try:
            c.load(raw_cookie)
        except Exception:
            return None
        m = c.get("vmail_session")
        if not m:
            return None
        return _verify_token(m.value)

    def _require_admin(self):
        sess = self._get_session()
        if not sess:
            self._send_json(401, {"ok": False, "error": "Chưa đăng nhập hoặc phiên đã hết hạn."})
            return None
        return sess

    def do_HEAD(self):
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # Public tracking / health endpoints
        if path == "/vmail-api/v1/health":
            return self._send_json(200, {"ok": True, "service": "Vmail", "version": "1.1.0"})

        if path == "/vmail-api/v1/track/open":
            tok = (qs.get("t") or [""])[0].strip()
            if tok and not getattr(self, "_head_only", False):
                with db.session(commit=True) as conn:
                    row = conn.execute("SELECT id, campaign_id, opened_at FROM campaign_items WHERE tracking_token=?", (tok,)).fetchone()
                    if row and not row["opened_at"]:
                        conn.execute("UPDATE campaign_items SET opened_at=? WHERE id=?", (int(time.time()), row["id"]))
                        conn.execute("UPDATE campaigns SET open_count=open_count+1 WHERE id=?", (row["campaign_id"],))
            return self._send_bytes(200, "image/gif", TRANSPARENT_GIF, "no-store")

        if path == "/vmail-api/v1/unsubscribe":
            tok = (qs.get("t") or [""])[0].strip()
            email_show = ""
            if tok:
                with db.session(commit=True) as conn:
                    row = conn.execute("SELECT id, campaign_id, contact_id, email FROM campaign_items WHERE tracking_token=?", (tok,)).fetchone()
                    if row:
                        email_show = row["email"]
                        if not getattr(self, "_head_only", False):
                            upd = conn.execute("UPDATE contacts SET status='unsubscribed' WHERE id=? AND status!='unsubscribed'", (row["contact_id"],)).rowcount
                            if upd > 0:
                                conn.execute("UPDATE campaigns SET unsub_count=unsub_count+1 WHERE id=?", (row["campaign_id"],))
            page = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hủy đăng ký nhận thư - Vmail</title><style>body{{font-family:Arial,sans-serif;background:#f6f8fc;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}.card{{background:#fff;padding:32px;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:440px;text-align:center}}h2{{color:#1a73e8;margin-top:0}}p{{color:#444;line-height:1.6}}</style></head><body><div class="card"><h2>Đã hủy đăng ký thành công</h2><p>Địa chỉ <strong>{html.escape(email_show) or 'của bạn'}</strong> sẽ không còn nhận thêm email từ chiến dịch này.</p></div></body></html>""".encode("utf-8")
            return self._send_bytes(200, "text/html; charset=utf-8", page, "no-store")

        # Admin API GETs
        if path == "/api/me":
            sess = self._get_session()
            if not sess:
                return self._send_json(401, {"ok": False, "authenticated": False})
            return self._send_json(200, {"ok": True, "authenticated": True, "user": sess["user"]})

        if path.startswith("/api/"):
            sess = self._require_admin()
            if not sess:
                return
            try:
                return self._handle_api_get(path, qs, sess)
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})

        # Static Frontend under WEB_DIR (realpath + commonpath traversal protection)
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        candidate = os.path.realpath(os.path.join(WEB_DIR, rel))
        try:
            in_web = os.path.commonpath([WEB_DIR, candidate]) == WEB_DIR
        except ValueError:
            in_web = False
        safe_path = candidate if (in_web and os.path.isfile(candidate)) else os.path.join(WEB_DIR, "index.html")
        ctype, _ = mimetypes.guess_type(safe_path)
        if safe_path.endswith(".svg"):
            ctype = "image/svg+xml"
        with open(safe_path, "rb") as f:
            data = f.read()
        cache = "no-cache" if safe_path.endswith(".html") else "public, max-age=3600"
        return self._send_bytes(200, ctype or "application/octet-stream", data, cache)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Public Transactional Mail API (BillionMail-compatible speed & simplicity)
        if path == "/vmail-api/v1/send":
            return self._handle_public_send()

        if path == "/api/login":
            if not self._check_origin():
                return self._send_json(403, {"ok": False, "error": "Origin không hợp lệ."})
            return self._handle_login()

        if path == "/api/logout":
            if not self._check_origin():
                return self._send_json(403, {"ok": False, "error": "Origin không hợp lệ."})
            return self._handle_logout()

        if path.startswith("/api/"):
            if not self._check_origin():
                return self._send_json(403, {"ok": False, "error": "Yêu cầu bị chặn bởi chính sách Same-Origin (CSRF)."})
            sess = self._require_admin()
            if not sess:
                return
            try:
                return self._handle_api_post(path, sess)
            except Exception as e:
                return self._send_json(400, {"ok": False, "error": str(e)})

        return self._send_json(404, {"ok": False, "error": "Not found"})

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            if not self._check_origin():
                return self._send_json(403, {"ok": False, "error": "Yêu cầu bị chặn bởi chính sách Same-Origin (CSRF)."})
            sess = self._require_admin()
            if not sess:
                return
            try:
                return self._handle_api_patch(path, sess)
            except Exception as e:
                return self._send_json(400, {"ok": False, "error": str(e)})
        return self._send_json(404, {"ok": False, "error": "Not found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            if not self._check_origin():
                return self._send_json(403, {"ok": False, "error": "Yêu cầu bị chặn bởi chính sách Same-Origin (CSRF)."})
            sess = self._require_admin()
            if not sess:
                return
            try:
                return self._handle_api_delete(path, sess)
            except Exception as e:
                return self._send_json(400, {"ok": False, "error": str(e)})
        return self._send_json(404, {"ok": False, "error": "Not found"})

    def _handle_login(self):
        client_ip = self._client_ip()
        now = time.time()
        with _SESS_LOCK:
            if len(_FAILED_LOGINS) > 500:
                stale = [ip for ip, ts in _FAILED_LOGINS.items() if not ts or now - ts[-1] >= LOGIN_WINDOW_SEC]
                for ip in stale:
                    _FAILED_LOGINS.pop(ip, None)
            hist = [t for t in _FAILED_LOGINS.get(client_ip, []) if now - t < LOGIN_WINDOW_SEC]
            if hist:
                _FAILED_LOGINS[client_ip] = hist
            else:
                _FAILED_LOGINS.pop(client_ip, None)
            if len(hist) >= MAX_LOGIN_FAILS:
                return self._send_json(429, {"ok": False, "error": "Quá nhiều lần đăng nhập sai. Vui lòng thử lại sau ít phút."})
        try:
            body = self._read_json()
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": str(e)})
        user = (body.get("username") or "").strip()
        pwd = body.get("password") or ""
        ok, info, err = jmap_client.verify_admin_login(user, pwd)
        if not ok:
            with _SESS_LOCK:
                hist = [t for t in _FAILED_LOGINS.get(client_ip, []) if now - t < LOGIN_WINDOW_SEC]
                hist.append(now)
                _FAILED_LOGINS[client_ip] = hist
            return self._send_json(401, {"ok": False, "error": err})
        # Store sender cred encrypted for API / campaign / test sends
        db.save_sender_cred(info["email"], pwd)
        sid = secrets.token_urlsafe(24)
        with _SESS_LOCK:
            _FAILED_LOGINS.pop(client_ip, None)
            expired = [k for k, v in _SESSIONS.items() if v["exp"] < now]
            for k in expired:
                _SESSIONS.pop(k, None)
            if len(_SESSIONS) >= MAX_SESSIONS:
                oldest_sid = min(_SESSIONS.items(), key=lambda kv: kv[1]["exp"])[0]
                _SESSIONS.pop(oldest_sid, None)
            _SESSIONS[sid] = {"user": info, "password": pwd, "exp": now + SESSION_TTL}
        tok = _sign_token(sid)
        sec_attr = "; Secure" if self._is_https() else ""
        cookie_hdr = f"vmail_session={tok}; Path=/admin/; HttpOnly; SameSite=Lax{sec_attr}; Max-Age={SESSION_TTL}"
        return self._send_json(200, {"ok": True, "user": info}, {"Set-Cookie": cookie_hdr})

    def _handle_logout(self):
        raw_cookie = self.headers.get("Cookie", "")
        c = SimpleCookie()
        try:
            c.load(raw_cookie)
            if "vmail_session" in c:
                sid = c["vmail_session"].value.rsplit(".", 1)[0]
                with _SESS_LOCK:
                    _SESSIONS.pop(sid, None)
        except Exception:
            pass
        sec_attr = "; Secure" if self._is_https() else ""
        return self._send_json(200, {"ok": True}, {"Set-Cookie": f"vmail_session=; Path=/admin/; HttpOnly; SameSite=Lax{sec_attr}; Max-Age=0"})

    def _handle_public_send(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return self._send_json(401, {"ok": False, "error": "Thiếu header Authorization: Bearer <api_key>"})
        raw_key = auth[7:].strip()
        khash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        with db.session(commit=True) as conn:
            row = conn.execute("SELECT * FROM api_keys WHERE key_hash=?", (khash,)).fetchone()
            if not row or not row["enabled"]:
                return self._send_json(403, {"ok": False, "error": "API Key không hợp lệ hoặc đã bị vô hiệu hóa."})

            client_ip = self._client_ip()
            allowed_ips = [x.strip() for x in (row["allowed_ips"] or "").split(",") if x.strip()]
            if allowed_ips and not _ip_allowed(client_ip, allowed_ips):
                return self._send_json(403, {"ok": False, "error": f"IP {client_ip} không nằm trong danh sách cho phép."})

            today = datetime.date.today().isoformat()
            sent_today = row["sent_today"] if row["last_reset"] == today else 0
            if row["daily_limit"] > 0 and sent_today >= row["daily_limit"]:
                return self._send_json(429, {"ok": False, "error": "Đã vượt quá giới hạn gửi trong ngày của API Key."})

            try:
                body = self._read_json()
            except Exception as e:
                return self._send_json(400, {"ok": False, "error": str(e)})

            to_email = (body.get("to") or "").strip()
            subject = (body.get("subject") or "").strip()
            html_body = body.get("html") or ""
            text_body = body.get("text") or ""
            from_email = (body.get("from") or row["default_from"]).strip()
            from_name = (body.get("from_name") or "Vmail").strip()
            reply_to = (body.get("reply_to") or "").strip()

            if not _EMAIL_RE.match(to_email) or not _EMAIL_RE.match(from_email) or not subject:
                return self._send_json(400, {"ok": False, "error": "Thiếu 'to'/'from' hợp lệ hoặc 'subject'."})
            if any("\r" in v or "\n" in v for v in (to_email, from_email, from_name, subject, reply_to)):
                return self._send_json(400, {"ok": False, "error": "Header email không hợp lệ."})
            if reply_to and not _EMAIL_RE.match(reply_to):
                return self._send_json(400, {"ok": False, "error": "Địa chỉ 'reply_to' không hợp lệ."})

            # Enforce sender domain belongs to local Vmail domains or key's default_from domain
            from_dom = from_email.split("@", 1)[1].lower()
            def_dom = row["default_from"].split("@", 1)[1].lower() if "@" in (row["default_from"] or "") else ""
            if from_dom != def_dom:
                try:
                    local_doms = {d.get("name", "").lower() for d in jmap_client.list_domains()}
                except Exception:
                    local_doms = set()
                if from_dom not in local_doms:
                    return self._send_json(403, {"ok": False, "error": f"Tên miền gửi '{from_dom}' không thuộc hệ thống Vmail."})

            conn.execute(
                "UPDATE api_keys SET sent_today=?, last_reset=? WHERE id=?",
                (sent_today + 1, today, row["id"]),
            )

        try:
            res = mailer.send_email(
                from_email=from_email,
                from_name=from_name,
                to_email=to_email,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
                reply_to=reply_to,
                source="api",
                verify_delivery=bool(body.get("verify", False)),
            )
            return self._send_json(200, {"ok": True, "data": res})
        except Exception as e:
            db.add_send_log("api", from_email, to_email, subject, "failed", str(e))
            return self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_api_get(self, path: str, qs: dict, sess: dict):
        if path == "/api/overview":
            domains = jmap_client.list_domains()
            accounts = jmap_client.list_accounts()
            routes, strat = jmap_client.get_relays_and_strategy()
            queue = jmap_client.list_queue()
            logs = log_parser.parse_logs(limit=40)
            health = log_parser.get_system_health()
            default_route = ((strat.get("route") or {}).get("else") or "").strip("'")
            with db.session(commit=False) as conn:
                camp_count = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
                contact_count = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='subscribed'").fetchone()[0]
                apikey_count = conn.execute("SELECT COUNT(*) FROM api_keys WHERE enabled=1").fetchone()[0]
            return self._send_json(200, {
                "ok": True,
                "stats": {
                    "domains": len(domains),
                    "accounts": len(accounts),
                    "queue": len(queue),
                    "campaigns": camp_count,
                    "contacts": contact_count,
                    "api_keys": apikey_count,
                    "default_route": default_route,
                    "totals": logs["totals"],
                    "today": logs["today"],
                    "daily": logs["daily"],
                },
                "health": health,
                "recent_events": logs["events"][:15],
            })

        if path == "/api/domains":
            domains = jmap_client.list_domains()
            out = []
            for d in domains:
                out.append({
                    "id": d.get("id"),
                    "name": d.get("name"),
                    "enable": d.get("enable", True),
                    "catchAllAddress": d.get("catchAllAddress") or "",
                    "createdAt": d.get("createdAt"),
                    "dnsZoneFile": d.get("dnsZoneFile") or "",
                })
            return self._send_json(200, {"ok": True, "domains": out})

        if path == "/api/accounts":
            accts = jmap_client.list_accounts()
            domains = {d["id"]: d["name"] for d in jmap_client.list_domains()}
            out = []
            for a in accts:
                aliases_raw = a.get("aliases") or {}
                aliases = []
                for _, av in sorted(aliases_raw.items()):
                    if isinstance(av, dict) and av.get("name"):
                        dom_n = domains.get(av.get("domainId"), "")
                        aliases.append(f"{av['name']}@{dom_n}" if dom_n else av["name"])
                quotas = a.get("quotas") or {}
                max_bytes = quotas.get("maxDiskQuota") or 0
                out.append({
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "email": a.get("emailAddress"),
                    "domainId": a.get("domainId"),
                    "domain": domains.get(a.get("domainId"), ""),
                    "description": a.get("description") or "",
                    "role": (a.get("roles") or {}).get("@type") or "User",
                    "usedBytes": a.get("usedDiskQuota") or 0,
                    "quotaMb": int(max_bytes / (1024 * 1024)) if max_bytes else 0,
                    "aliases": aliases,
                    "createdAt": a.get("createdAt"),
                })
            return self._send_json(200, {"ok": True, "accounts": out})

        if path == "/api/relays":
            routes, strat = jmap_client.get_relays_and_strategy()
            default_route = ((strat.get("route") or {}).get("else") or "").strip("'")
            return self._send_json(200, {"ok": True, "routes": routes, "default_route": default_route})

        if path == "/api/apikeys":
            with db.session(commit=False) as conn:
                rows = conn.execute("SELECT id, name, key_prefix, default_from, allowed_ips, daily_limit, sent_today, last_reset, enabled, created_at FROM api_keys ORDER BY created_at DESC").fetchall()
            return self._send_json(200, {"ok": True, "keys": [dict(r) for r in rows]})

        if path == "/api/lists":
            with db.session(commit=False) as conn:
                rows = conn.execute(
                    "SELECT l.*, (SELECT COUNT(*) FROM contacts c WHERE c.list_id=l.id AND c.status='subscribed') AS active_count, "
                    "(SELECT COUNT(*) FROM contacts c WHERE c.list_id=l.id) AS total_count FROM contact_lists l ORDER BY l.created_at DESC"
                ).fetchall()
            return self._send_json(200, {"ok": True, "lists": [dict(r) for r in rows]})

        if path == "/api/contacts":
            list_id = (qs.get("list_id") or [""])[0]
            with db.session(commit=False) as conn:
                if list_id:
                    rows = conn.execute("SELECT * FROM contacts WHERE list_id=? ORDER BY created_at DESC LIMIT 500", (list_id,)).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM contacts ORDER BY created_at DESC LIMIT 500").fetchall()
            return self._send_json(200, {"ok": True, "contacts": [dict(r) for r in rows]})

        if path == "/api/templates":
            with db.session(commit=False) as conn:
                rows = conn.execute("SELECT * FROM templates ORDER BY updated_at DESC").fetchall()
            return self._send_json(200, {"ok": True, "templates": [dict(r) for r in rows]})

        if path == "/api/campaigns":
            with db.session(commit=False) as conn:
                rows = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
            return self._send_json(200, {"ok": True, "campaigns": [dict(r) for r in rows]})

        if path == "/api/spam":
            settings, mkeys, lookups = jmap_client.get_spam_bundle()
            hosts_txt = open("/etc/hosts", "r", encoding="utf-8", errors="replace").read() if os.path.exists("/etc/hosts") else ""
            sinkhole_count = sum(1 for ln in hosts_txt.splitlines() if ln.strip().startswith("0.0.0.0 ") and not ln.strip().endswith("localhost"))
            telemetry_ok = os.environ.get("VMAIL_TELEMETRY_DISABLED", "1") == "1"
            vendor_ok = sinkhole_count > 0 or telemetry_ok
            return self._send_json(200, {
                "ok": True,
                "settings": settings,
                "domain_lists": mkeys,
                "http_lookups": lookups,
                "isolation": {
                    "telemetry_isolated": telemetry_ok,
                    "vendor_isolated": vendor_ok,
                },
            })

        if path == "/api/logs":
            logs = log_parser.parse_logs(limit=150)
            return self._send_json(200, {"ok": True, **logs})

        if path == "/api/queue":
            return self._send_json(200, {"ok": True, "queue": jmap_client.list_queue()})

        if path == "/api/settings/cf":
            email, key = dns_cf.load_cf_creds()
            masked = (key[:4] + "..." + key[-4:]) if len(key) > 10 else ("****" if key else "")
            return self._send_json(200, {"ok": True, "email": email, "key_masked": masked, "configured": bool(email and key)})

        return self._send_json(404, {"ok": False, "error": "Endpoint không tồn tại."})

    def _handle_api_post(self, path: str, sess: dict):
        body = self._read_json()

        if path == "/api/domains":
            name = (body.get("name") or "").strip().lower().rstrip(".")
            if not _DOMAIN_RE.match(name):
                raise ValueError("Tên miền không hợp lệ (ví dụ: company.com).")
            res = jmap_client.create_domain(name)
            return self._send_json(200, {"ok": True, "domain": res})

        if path == "/api/domains/sync-cf":
            summary = dns_cf.sync_all_cloudflare()
            return self._send_json(200, {"ok": True, "summary": summary})

        m_check = path.startswith("/api/domains/") and path.endswith("/check-dns")
        if m_check:
            dname = urllib.parse.unquote(path[len("/api/domains/") : -len("/check-dns")]).strip().lower()
            domains = jmap_client.list_domains()
            dom_obj = next((d for d in domains if d["name"] == dname), None)
            if not dom_obj:
                raise ValueError("Không tìm thấy tên miền.")
            return self._send_json(200, {"ok": True, "dns": dns_cf.check_domain_dns(dom_obj)})

        if path.startswith("/api/domains/") and path.endswith("/fix-dns"):
            dname = urllib.parse.unquote(path[len("/api/domains/") : -len("/fix-dns")]).strip().lower()
            return self._send_json(200, {"ok": True, "result": dns_cf.fix_single_domain(dname)})

        if path.startswith("/api/domains/") and path.endswith("/catchall"):
            did = urllib.parse.unquote(path[len("/api/domains/") : -len("/catchall")])
            addr = (body.get("catchAllAddress") or "").strip().lower() or None
            if addr and not _EMAIL_RE.match(addr):
                raise ValueError("Địa chỉ Catch-All không hợp lệ.")
            jmap_client.update_domain(did, {"catchAllAddress": addr})
            return self._send_json(200, {"ok": True})

        if path == "/api/accounts":
            email = (body.get("email") or "").strip().lower()
            password = str(body.get("password") or "")
            desc = str(body.get("description") or "").strip()[:120]
            role = body.get("role") or "User"
            if role not in ("User", "Admin"):
                role = "User"
            quota_mb = max(0, min(int(body.get("quotaMb") or 0), 10485760))
            aliases = body.get("aliases") or []
            if not isinstance(aliases, list):
                raise ValueError("Danh sách alias không hợp lệ.")
            if not _EMAIL_RE.match(email) or len(password) < 6:
                raise ValueError("Email không hợp lệ hoặc mật khẩu dưới 6 ký tự.")
            local, dom_name = email.split("@", 1)
            domains = {d["name"]: d["id"] for d in jmap_client.list_domains()}
            if dom_name not in domains:
                raise ValueError(f"Tên miền {dom_name} chưa được thêm vào hệ thống.")
            res = jmap_client.create_account(local, domains[dom_name], password, desc, role, quota_mb, aliases)
            db.save_sender_cred(email, password)
            return self._send_json(200, {"ok": True, "account": res})

        if path == "/api/relays/upsert":
            rname = (body.get("name") or "").strip()
            rhost = (body.get("host") or "").strip()
            rport = int(body.get("port") or 587)
            if not rname or not re.match(r"^[a-zA-Z0-9._-]{2,64}$", rname):
                raise ValueError("Tên route không hợp lệ (chỉ gồm chữ, số, -, _).")
            if not rhost or any(c.isspace() for c in rhost):
                raise ValueError("SMTP Host không hợp lệ.")
            if rport < 1 or rport > 65535:
                raise ValueError("Cổng SMTP Relay phải từ 1 đến 65535.")
            jmap_client.upsert_relay(
                route_name=rname,
                host=rhost,
                port=rport,
                implicit_tls=bool(body.get("implicitTls", False)),
                username=str(body.get("authUsername") or "").strip(),
                secret=str(body.get("authSecret") or "").strip(),
                description=str(body.get("description") or "").strip()[:160],
            )
            if body.get("setDefault"):
                jmap_client.set_default_outbound_route(rname)
            return self._send_json(200, {"ok": True})

        if path == "/api/relays/default":
            rname = (body.get("route") or "").strip()
            if not rname:
                raise ValueError("Thiếu tên route.")
            jmap_client.set_default_outbound_route(rname)
            return self._send_json(200, {"ok": True})

        if path == "/api/relays/test-conn":
            rport = int(body.get("port") or 587)
            if rport < 1 or rport > 65535:
                raise ValueError("Cổng SMTP không hợp lệ (1-65535).")
            res = mailer.test_relay_connection(
                host=body.get("host") or "",
                port=rport,
                implicit_tls=bool(body.get("implicitTls", False)),
                username=body.get("authUsername") or "",
                secret=body.get("authSecret") or "",
            )
            return self._send_json(200, {"ok": True, "conn": res})

        if path == "/api/test-send":
            from_email = (body.get("from") or sess["user"]["email"]).strip()
            to_email = (body.get("to") or "").strip()
            subject = (body.get("subject") or "[Vmail] Thư kiểm tra kết nối SMTP").strip()
            content = str(body.get("body") or "Đây là email kiểm tra gửi từ hệ thống Vmail.")
            if not _EMAIL_RE.match(from_email):
                raise ValueError("Địa chỉ người gửi không hợp lệ.")
            if not _EMAIL_RE.match(to_email):
                raise ValueError("Địa chỉ người nhận không hợp lệ.")
            smtp_user = None
            smtp_pass = None
            if from_email.lower() == sess["user"]["email"].lower():
                smtp_user = sess["user"]["email"]
                smtp_pass = sess["password"]
            safe_body = html.escape(content).replace("\n", "<br/>")
            html_b = f"<div style='font-family:Arial,sans-serif;padding:16px;border:1px solid #e5e7eb;border-radius:10px'><h3 style='color:#1a73e8;margin-top:0'>Vmail SMTP Test</h3><p>{safe_body}</p><hr/><small style='color:#6b7280'>Gửi lúc {time.strftime('%Y-%m-%d %H:%M:%S')}</small></div>"
            res = mailer.send_email(
                from_email=from_email,
                from_name="Vmail System",
                to_email=to_email,
                subject=subject,
                html_body=html_b,
                text_body=content,
                smtp_user=smtp_user,
                smtp_pass=smtp_pass,
                source="test",
                verify_delivery=True,
            )
            return self._send_json(200, {"ok": True, "delivery": res})

        if path == "/api/apikeys":
            name = (body.get("name") or "API Key").strip()[:100]
            default_from = (body.get("default_from") or sess["user"]["email"]).strip()
            if not _EMAIL_RE.match(default_from):
                raise ValueError("Email gửi mặc định cho API Key không hợp lệ.")
            allowed_ips = (body.get("allowed_ips") or "").strip()
            daily_limit = max(0, min(int(body.get("daily_limit") or 300), 1000000))
            raw_key = "vm_" + secrets.token_urlsafe(28)
            khash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            kid = "ak_" + secrets.token_hex(6)
            with db.session(commit=True) as conn:
                conn.execute(
                    "INSERT INTO api_keys(id, name, key_prefix, key_hash, default_from, allowed_ips, daily_limit, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (kid, name, raw_key[:10] + "...", khash, default_from, allowed_ips, daily_limit, int(time.time())),
                )
            return self._send_json(200, {"ok": True, "id": kid, "raw_key": raw_key})

        if path == "/api/lists":
            name = (body.get("name") or "").strip()[:120]
            if not name:
                raise ValueError("Thiếu tên danh sách.")
            lid = "list_" + secrets.token_hex(5)
            with db.session(commit=True) as conn:
                conn.execute(
                    "INSERT INTO contact_lists(id, name, description, created_at) VALUES (?,?,?,?)",
                    (lid, name, str(body.get("description") or "").strip()[:240], int(time.time())),
                )
            return self._send_json(200, {"ok": True, "id": lid})

        if path == "/api/contacts/import":
            list_id = (body.get("list_id") or "list_default").strip()
            raw_text = str(body.get("raw") or "")
            tags = (body.get("tags") or "").strip()[:120]
            added, skipped = 0, 0
            now = int(time.time())
            with db.session(commit=True) as conn:
                exists_list = conn.execute("SELECT 1 FROM contact_lists WHERE id=?", (list_id,)).fetchone()
                if not exists_list:
                    raise ValueError("Nhóm danh bạ không tồn tại.")
                for line in raw_text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = [p.strip() for p in re.split(r"[,;\t]", line, maxsplit=1)] if ("," in line or ";" in line or "\t" in line) else [line]
                    email = parts[0].lower()
                    cname = parts[1][:120] if len(parts) > 1 else ""
                    if not _EMAIL_RE.match(email):
                        skipped += 1
                        continue
                    try:
                        conn.execute(
                            "INSERT INTO contacts(id, list_id, email, name, tags, status, created_at) VALUES (?,?,?,?,?,?,?)",
                            ("ct_" + secrets.token_hex(6), list_id, email, cname, tags, "subscribed", now),
                        )
                        added += 1
                    except Exception:
                        skipped += 1
            return self._send_json(200, {"ok": True, "added": added, "skipped": skipped})

        if path == "/api/templates":
            tid = (body.get("id") or "").strip() or ("tpl_" + secrets.token_hex(5))
            name = (body.get("name") or "").strip()[:120]
            subject = (body.get("subject") or "").strip()[:250]
            html_body = str(body.get("html_body") or "")
            text_body = str(body.get("text_body") or "")
            if not name or not subject or not html_body:
                raise ValueError("Cần nhập đủ tên mẫu, tiêu đề và nội dung HTML.")
            now = int(time.time())
            with db.session(commit=True) as conn:
                conn.execute(
                    "INSERT INTO templates(id, name, subject, html_body, text_body, created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, subject=excluded.subject, html_body=excluded.html_body, text_body=excluded.text_body, updated_at=excluded.updated_at",
                    (tid, name, subject, html_body, text_body, now, now),
                )
            return self._send_json(200, {"ok": True, "id": tid})

        if path == "/api/campaigns":
            name = (body.get("name") or "").strip()[:120]
            list_id = (body.get("list_id") or "").strip()
            template_id = (body.get("template_id") or "").strip()
            from_email = (body.get("from_email") or sess["user"]["email"]).strip()
            from_name = (body.get("from_name") or "Vmail").strip()[:100]
            reply_to = (body.get("reply_to") or from_email).strip()
            if not _EMAIL_RE.match(from_email) or not _EMAIL_RE.match(reply_to):
                raise ValueError("Địa chỉ From hoặc Reply-To không hợp lệ.")
            subject = (body.get("subject") or "").strip()[:250]
            delay_ms = max(300, min(int(body.get("delay_ms") or 1500), 60000))
            daily_limit = max(1, min(int(body.get("daily_limit") or 250), 100000))
            track_opens = 1 if body.get("track_opens", True) else 0
            include_unsub = 1 if body.get("include_unsub", True) else 0

            cid = "cmp_" + secrets.token_hex(5)
            now = int(time.time())
            status_init = "running" if body.get("auto_start") else "draft"
            started_init = now if body.get("auto_start") else 0
            with db.session(commit=True) as conn:
                tpl = conn.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
                contacts = conn.execute("SELECT * FROM contacts WHERE list_id=? AND status='subscribed'", (list_id,)).fetchall()
                if not name or not tpl or not contacts:
                    raise ValueError("Cần chọn tên chiến dịch, mẫu email hợp lệ và danh sách có ít nhất 1 liên hệ đang đăng ký.")
                conn.execute(
                    "INSERT INTO campaigns(id, name, list_id, template_id, from_email, from_name, reply_to, subject, delay_ms, daily_limit, track_opens, include_unsub, status, total_count, started_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, name, list_id, template_id, from_email, from_name, reply_to, subject or tpl["subject"], delay_ms, daily_limit, track_opens, include_unsub, status_init, len(contacts), started_init, now),
                )
                for c in contacts:
                    conn.execute(
                        "INSERT INTO campaign_items(id, campaign_id, contact_id, email, name, status, tracking_token) VALUES (?,?,?,?,?,?,?)",
                        ("ci_" + secrets.token_hex(6), cid, c["id"], c["email"], c["name"], "pending", secrets.token_urlsafe(16)),
                    )
            return self._send_json(200, {"ok": True, "id": cid})

        if path.startswith("/api/campaigns/") and path.endswith("/action"):
            cid = path[len("/api/campaigns/") : -len("/action")]
            act = body.get("action")
            if act not in ("start", "pause", "delete"):
                raise ValueError("Hành động chiến dịch không hợp lệ.")
            with db.session(commit=True) as conn:
                if act == "start":
                    conn.execute("UPDATE campaigns SET status='running', last_error='', started_at=CASE WHEN started_at=0 THEN ? ELSE started_at END WHERE id=?", (int(time.time()), cid))
                elif act == "pause":
                    conn.execute("UPDATE campaigns SET status='paused' WHERE id=?", (cid,))
                elif act == "delete":
                    conn.execute("DELETE FROM campaigns WHERE id=?", (cid,))
            return self._send_json(200, {"ok": True})

        if path == "/api/spam/settings":
            patch = {}
            for k in ("enable", "trustContacts", "trustReplies"):
                if k in body:
                    patch[k] = bool(body[k])
            for k in ("scoreSpam", "scoreDiscard", "scoreReject"):
                if k in body:
                    v = float(body[k])
                    if v < 0 or v > 100:
                        raise ValueError(f"Giá trị {k} phải nằm trong khoảng 0 - 100.")
                    patch[k] = v
            if all(k in patch for k in ("scoreSpam", "scoreDiscard", "scoreReject")):
                if not (patch["scoreSpam"] <= patch["scoreDiscard"] <= patch["scoreReject"]):
                    raise ValueError("Ngưỡng Spam <= Discard <= Reject mới hợp lệ.")
            jmap_client.update_spam_settings(patch)
            return self._send_json(200, {"ok": True})

        if path == "/api/spam/domain-list":
            ns = body.get("namespace")
            key = (body.get("key") or "").strip().lower().rstrip(".")
            if ns not in ("trusted-domains", "blocked-domains", "spam-traps") or not _DOMAIN_RE.match(key):
                raise ValueError("Tên miền whitelist/blacklist không hợp lệ.")
            res = jmap_client.add_memory_key(ns, key, False)
            return self._send_json(200, {"ok": True, "item": res})

        if path == "/api/spam/lookup-toggle":
            lid = (body.get("id") or "").strip()
            if not lid:
                raise ValueError("Thiếu ID bộ lọc.")
            jmap_client.toggle_http_lookup(lid, bool(body.get("enable")))
            return self._send_json(200, {"ok": True})

        if path == "/api/settings/cf":
            email = (body.get("email") or "").strip()
            key = (body.get("api_key") or "").strip()
            if not _EMAIL_RE.match(email) or len(key) < 10:
                raise ValueError("Email hoặc Cloudflare API Key không hợp lệ.")
            dns_cf.save_cf_creds(email, key)
            return self._send_json(200, {"ok": True})

        return self._send_json(404, {"ok": False, "error": "Endpoint POST không tồn tại."})

    def _handle_api_patch(self, path: str, sess: dict):
        body = self._read_json()
        if path.startswith("/api/accounts/"):
            aid = urllib.parse.unquote(path[len("/api/accounts/") :])
            accts = jmap_client.list_accounts()
            cur = next((a for a in accts if a["id"] == aid), None)
            if not cur:
                raise ValueError("Không tìm thấy tài khoản.")
            patch = {}
            if "description" in body:
                patch["description"] = str(body["description"] or "").strip()[:120]
            if "role" in body and body["role"] in ("User", "Admin"):
                if body["role"] == "User" and cur.get("role") == "Admin":
                    if aid == sess["user"]["id"]:
                        raise ValueError("Không thể tự hạ quyền tài khoản quản trị đang đăng nhập.")
                    admin_count = sum(1 for a in accts if a.get("role") == "Admin")
                    if admin_count <= 1:
                        raise ValueError("Hệ thống phải giữ tối thiểu 1 tài khoản Admin.")
                patch["roles"] = {"@type": body["role"]}
            if "quotaMb" in body:
                qmb = max(0, min(int(body["quotaMb"] or 0), 10485760))
                patch["quotas"] = {"maxDiskQuota": qmb * 1024 * 1024} if qmb > 0 else {}
            if "aliases" in body:
                if not isinstance(body["aliases"], list):
                    raise ValueError("Danh sách alias không hợp lệ.")
                alias_map = {}
                for idx, al in enumerate(body["aliases"] or []):
                    al_local = str(al).strip().lower().split("@", 1)[0]
                    if al_local and re.match(r"^[a-z0-9._+-]{1,64}$", al_local):
                        alias_map[str(idx)] = {"name": al_local, "domainId": cur["domainId"], "enable": True}
                patch["aliases"] = alias_map
            if body.get("password"):
                pw = str(body["password"])
                if len(pw) < 6:
                    raise ValueError("Mật khẩu mới tối thiểu 6 ký tự.")
                patch["credentials"] = {"0": {"@type": "Password", "secret": pw}}
                db.save_sender_cred(cur["emailAddress"], pw)
            jmap_client.update_account(aid, patch)
            return self._send_json(200, {"ok": True})
        return self._send_json(404, {"ok": False, "error": "Not found"})

    def _handle_api_delete(self, path: str, sess: dict):
        if path.startswith("/api/domains/"):
            did = urllib.parse.unquote(path[len("/api/domains/") :])
            jmap_client.delete_domain(did)
            return self._send_json(200, {"ok": True})
        if path.startswith("/api/accounts/"):
            aid = urllib.parse.unquote(path[len("/api/accounts/") :])
            if aid == sess["user"]["id"]:
                raise ValueError("Không thể xóa tài khoản quản trị đang đăng nhập.")
            jmap_client.delete_account(aid)
            return self._send_json(200, {"ok": True})
        if path.startswith("/api/apikeys/"):
            kid = path[len("/api/apikeys/") :]
            with db.session(commit=True) as conn:
                conn.execute("DELETE FROM api_keys WHERE id=?", (kid,))
            return self._send_json(200, {"ok": True})
        if path.startswith("/api/lists/"):
            lid = path[len("/api/lists/") :]
            if lid == "list_default":
                raise ValueError("Không thể xóa nhóm danh bạ mặc định.")
            with db.session(commit=True) as conn:
                conn.execute("DELETE FROM contact_lists WHERE id=?", (lid,))
            return self._send_json(200, {"ok": True})
        if path.startswith("/api/contacts/"):
            cid = path[len("/api/contacts/") :]
            with db.session(commit=True) as conn:
                conn.execute("DELETE FROM contacts WHERE id=?", (cid,))
            return self._send_json(200, {"ok": True})
        if path.startswith("/api/templates/"):
            tid = path[len("/api/templates/") :]
            with db.session(commit=True) as conn:
                conn.execute("DELETE FROM templates WHERE id=?", (tid,))
            return self._send_json(200, {"ok": True})
        if path.startswith("/api/spam/domain-list/"):
            kid = urllib.parse.unquote(path[len("/api/spam/domain-list/") :])
            jmap_client.delete_memory_key(kid)
            return self._send_json(200, {"ok": True})
        if path.startswith("/api/queue/"):
            qid = urllib.parse.unquote(path[len("/api/queue/") :])
            jmap_client.delete_queue(qid)
            return self._send_json(200, {"ok": True})
        return self._send_json(404, {"ok": False, "error": "Not found"})


class _VmailHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    db.init_db()
    dns_cf.load_cf_creds()
    mailer.start_campaign_worker()
    srv = _VmailHTTPServer((HOST, PORT), VmailHandler)
    print(f"[Vmail] Listening on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
