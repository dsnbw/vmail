#!/usr/bin/env python3
from email.utils import formatdate, parseaddr
import hmac
import json
import os
import re
import smtplib
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET_CANDIDATES = [
    os.environ.get("VMAIL_BRIDGE_SECRET_FILE", ""),
    "/etc/vmail/inbound_bridge.secret",
]
VMAIL_CFG = os.environ.get("VMAIL_CONFIG_PATH", "/etc/vmail/config.json")
SMTP_HOST = os.environ.get("VMAIL_BRIDGE_SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("VMAIL_BRIDGE_SMTP_PORT", "2525"))
LISTEN_HOST = os.environ.get("VMAIL_BRIDGE_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("VMAIL_BRIDGE_LISTEN_PORT", "8095"))
MAX_BYTES = 30 * 1024 * 1024


def _load_secret() -> str:
    env_sec = os.environ.get("VMAIL_BRIDGE_SECRET", "").strip()
    if env_sec:
        return env_sec
    for path in SECRET_CANDIDATES:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                s = f.read().strip()
            if s:
                return s
    raise RuntimeError("Missing inbound bridge secret file (/etc/vmail/inbound_bridge.secret).")


def _default_mail_host(rcpt_to: str) -> str:
    if "@" in (rcpt_to or ""):
        dom = rcpt_to.split("@", 1)[1].strip().lower()
        dom = re.sub(r"[^a-z0-9.\-]", "", dom)
        if dom:
            return f"mail.{dom}"
    if os.path.exists(VMAIL_CFG):
        try:
            with open(VMAIL_CFG, "r", encoding="utf-8") as f:
                h = (json.load(f).get("mail_host") or "").strip()
            if h:
                return h
        except Exception:
            pass
    return socket.getfqdn() or "vmail.local"


def _prepend_vmail_received(raw_bytes: bytes, rcpt_to: str) -> bytes:
    eol = b"\r\n" if b"\r\n" in raw_bytes[:512] else b"\n"
    mail_host = _default_mail_host(rcpt_to)
    date_str = formatdate(usegmt=True)
    rec = (
        f"Received: from route1.mx.cloudflare.net (cloudflare-email-routing [127.0.0.1])"
    ).encode("ascii") + eol + (
        f"\tby {mail_host} (Vmail SMTP) with ESMTP;"
    ).encode("ascii") + eol + (
        f"\t{date_str}"
    ).encode("ascii") + eol
    return rec + raw_bytes


BRIDGE_SECRET = _load_secret()


def read_chunked_body(rfile):
    chunks = []
    total = 0
    while True:
        line = rfile.readline(65536)
        if not line:
            break
        size_hex = line.split(b";", 1)[0].strip()
        if not size_hex:
            continue
        size = int(size_hex, 16)
        if size == 0:
            while True:
                trailer = rfile.readline(65536)
                if not trailer or trailer in (b"\r\n", b"\n"):
                    break
            break
        total += size
        if total > MAX_BYTES:
            raise ValueError("body too large")
        data = rfile.read(size)
        chunks.append(data)
        rfile.readline(4)
    return b"".join(chunks)


_EMAIL_RE = re.compile(r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]+$")


class InboundHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Vmail-Bridge"
    sys_version = ""

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(30.0)
        except Exception:
            pass

    def log_message(self, format, *args):
        sys.stdout.write("[VmailBridge] " + (format % args) + "\n")
        sys.stdout.flush()

    def send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        if self.path == "/_inbound_bridge":
            self.send_json(200, {"status": "ok", "service": "vmail_inbound_bridge"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/_inbound_bridge":
            self.send_json(404, {"error": "not found"})
            return

        provided = self.headers.get("X-Bridge-Secret", "").strip()
        if not provided or not hmac.compare_digest(provided, BRIDGE_SECRET):
            self.send_json(403, {"ok": False, "error": "forbidden"})
            return

        try:
            te = self.headers.get("Transfer-Encoding", "").lower()
            if "chunked" in te:
                raw_bytes = read_chunked_body(self.rfile)
            else:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BYTES:
                    self.send_json(400, {"ok": False, "error": f"invalid body size: {length}"})
                    return
                raw_bytes = self.rfile.read(length)
        except Exception as e:
            self.send_json(400, {"ok": False, "error": f"read error: {e}"})
            return

        mail_from = parseaddr(self.headers.get("X-Mail-From", ""))[1].strip()
        rcpt_to = parseaddr(self.headers.get("X-Rcpt-To", ""))[1].strip()
        if not rcpt_to or not _EMAIL_RE.match(rcpt_to):
            self.send_json(400, {"ok": False, "error": "missing or invalid X-Rcpt-To"})
            return
        if "\r" in mail_from or "\n" in mail_from or (mail_from and not _EMAIL_RE.match(mail_from)):
            self.send_json(400, {"ok": False, "error": "invalid X-Mail-From"})
            return

        stamped_bytes = _prepend_vmail_received(raw_bytes, rcpt_to)

        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.ehlo("route1.mx.cloudflare.net")
                mail_opts = ["SMTPUTF8"] if s.has_extn("smtputf8") else []
                refused = s.sendmail(mail_from, [rcpt_to], stamped_bytes, mail_options=mail_opts)
                if refused:
                    self.send_json(422, {"ok": False, "error": str(refused)})
                    return
            print(f"[MailBridge] Delivered {mail_from or '<>'} -> {rcpt_to} ({len(raw_bytes)} bytes)", flush=True)
            self.send_json(200, {"ok": True, "from": mail_from, "to": rcpt_to, "size": len(raw_bytes)})
        except smtplib.SMTPRecipientsRefused as e:
            print(f"[MailBridge] Recipient refused {rcpt_to}: {e.recipients}", flush=True)
            self.send_json(422, {"ok": False, "error": f"recipient refused: {e.recipients}"})
        except smtplib.SMTPResponseException as e:
            print(f"[MailBridge] SMTP error {e.smtp_code}: {e.smtp_error}", flush=True)
            code = 422 if 500 <= e.smtp_code < 600 else 502
            self.send_json(code, {"ok": False, "smtp_code": e.smtp_code, "error": str(e.smtp_error)})
        except Exception as e:
            print(f"[MailBridge] Unexpected error: {e}", file=sys.stderr, flush=True)
            self.send_json(500, {"ok": False, "error": str(e)})


if __name__ == "__main__":
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), InboundHandler)
    print(f"[MailBridge] Listening on {LISTEN_HOST}:{LISTEN_PORT} -> {SMTP_HOST}:{SMTP_PORT}", flush=True)
    srv.serve_forever()
