import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
import glob
import html
import json
import os
import re
import smtplib
import socket
import ssl
import threading
import time
import uuid

import db

SMTP_HOST = os.environ.get("VMAIL_SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("VMAIL_SMTP_PORT", "4650"))
VMAIL_CFG = os.environ.get("VMAIL_CONFIG_PATH", "/etc/vmail/config.json")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_hdr(val: str, label: str = "header") -> str:
    s = (val or "").strip()
    if "\r" in s or "\n" in s or "\x00" in s:
        raise ValueError(f"Giá trị {label} không hợp lệ (chứa ký tự điều khiển dòng).")
    return s


def _get_public_base(from_email: str = "") -> str:
    env_url = os.environ.get("VMAIL_PUBLIC_URL", "").strip().rstrip("/")
    if env_url:
        return env_url
    if os.path.exists(VMAIL_CFG):
        try:
            with open(VMAIL_CFG, "r", encoding="utf-8") as f:
                u = (json.load(f).get("public_url") or "").strip().rstrip("/")
            if u:
                return u
        except Exception:
            pass
    if "@" in (from_email or ""):
        dom = from_email.split("@", 1)[1].strip()
        if dom:
            return f"https://mail.{dom}"
    return f"https://{socket.getfqdn()}"


def _find_latest_core_log():
    globs = [
        os.environ.get("VMAIL_CORE_LOG_GLOB", ""),
        "/var/log/vmail/core.log*",
    ]
    files = []
    for g in globs:
        if g:
            files.extend(glob.glob(g))
    files = sorted(set(files))
    return files[-1] if files else None


def test_relay_connection(host: str, port: int = 587, implicit_tls: bool = False, username: str = "", secret: str = ""):
    host = (host or "").strip()
    port = int(port or 587)
    if not host:
        raise ValueError("Thiếu SMTP Host.")
    t0 = time.time()
    ctx = ssl.create_default_context()
    if implicit_tls or port == 465:
        s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=10)
    else:
        s = smtplib.SMTP(host, port, timeout=10)
        s.ehlo()
        if s.has_extn("starttls"):
            s.starttls(context=ctx)
            s.ehlo()
    banner = str(s.ehlo_resp or "")
    auth_ok = None
    if username and secret and secret != "****":
        s.login(username.strip(), secret)
        auth_ok = True
    s.quit()
    return {
        "connected": True,
        "auth_tested": auth_ok is True,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "banner": banner[:180],
    }


def strip_html(raw: str) -> str:
    txt = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", "", raw or "", flags=re.I)
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"</p>", "\n\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    return html.unescape(re.sub(r"\n{3,}", "\n\n", txt)).strip()


def send_email(
    from_email: str,
    to_email: str,
    subject: str,
    html_body: str = "",
    text_body: str = "",
    from_name: str = "Vmail",
    reply_to: str = "",
    smtp_user: str = None,
    smtp_pass: str = None,
    unsub_url: str = "",
    source: str = "api",
    verify_delivery: bool = False,
):
    from_email = _clean_hdr(from_email, "From")
    to_email = _clean_hdr(to_email, "To")
    subject = _clean_hdr(subject, "Subject")
    from_name = _clean_hdr(from_name, "From Name") or "Vmail"
    reply_to = _clean_hdr(reply_to, "Reply-To")
    if not smtp_user or not smtp_pass:
        u, p = db.get_sender_cred(from_email)
        if not u or not p:
            raise RuntimeError(
                f"Chưa có thông tin xác thực SMTP cho {from_email}. Hãy đăng nhập Vmail Admin lại hoặc cấu hình tài khoản gửi."
            )
        smtp_user, smtp_pass = u, p

    domain = from_email.split("@", 1)[1] if "@" in from_email else (socket.getfqdn() or "vmail.local")
    msg = EmailMessage()
    msg["From"] = formataddr((from_name or "Vmail", from_email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg_id = make_msgid(domain=domain)
    msg["Message-ID"] = msg_id
    if reply_to:
        msg["Reply-To"] = reply_to
    if unsub_url:
        msg["List-Unsubscribe"] = f"<{_clean_hdr(unsub_url, 'Unsubscribe')}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    plain = (text_body or "").strip() or strip_html(html_body) or subject
    msg.set_content(plain, subtype="plain", charset="utf-8")
    if html_body:
        msg.add_alternative(html_body, subtype="html", charset="utf-8")

    # Record current log offset before sending if verification requested
    log_path = None
    log_pos = 0
    if verify_delivery:
        log_path = _find_latest_core_log()
        if log_path:
            try:
                log_pos = os.path.getsize(log_path)
            except Exception:
                log_pos = 0

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=15) as smtp:
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg, from_addr=from_email, to_addrs=[to_email])

    delivery_info = {"submitted": True, "message_id": msg_id, "delivered": None, "code": None, "detail": "Đã xếp hàng gửi qua Vmail MTA"}

    if verify_delivery and log_path:
        deadline = time.time() + 6.0
        qid = None
        while time.time() < deadline:
            time.sleep(0.45)
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(log_pos)
                    chunk = f.read()
                for raw_line in chunk.splitlines():
                    line = ANSI_RE.sub("", raw_line)
                    if not qid and "queue.authenticated-message-queued" in line and to_email in line:
                        mq = re.search(r"queueId\s*=\s*(\d+)", line)
                        if mq:
                            qid = mq.group(1)
                    if "delivery.delivered" in line and ((qid and qid in line) or (to_email in line and from_email in line)):
                        if not qid:
                            mq2 = re.search(r"queueId\s*=\s*(\d+)", line)
                            if mq2:
                                qid = mq2.group(1)
                        mc = re.search(r"code\s*=\s*(\d+)", line)
                        md = re.search(r'details\s*=\s*"([^"]+)"', line)
                        mh = re.search(r'hostname\s*=\s*"([^"]+)"', line)
                        rhost = mh.group(1) if mh else "direct-mx"
                        is_external_relay = any(x in rhost.lower() for x in ("brevo", "resend", "sendgrid", "mailgun", "smtp.", "relay"))
                        delivery_info.update({
                            "delivered": True,
                            "via_relay": is_external_relay,
                            "queue_id": qid or "",
                            "code": mc.group(1) if mc else "250",
                            "relay_host": rhost,
                            "detail": md.group(1) if md else "Delivered",
                        })
                        db.add_send_log(source, from_email, to_email, subject, "relayed" if is_external_relay else "delivered", f"[{rhost}] {delivery_info['code']} {delivery_info['detail']}")
                        return delivery_info
                    if any(errk in line for errk in ("delivery.message-rejected", "delivery.auth-failed", "delivery.dsn-perm-fail")) and (
                        (qid and qid in line) or to_email in line
                    ):
                        md = re.search(r'details\s*=\s*"([^"]+)"', line)
                        delivery_info.update({
                            "delivered": False,
                            "queue_id": qid or "",
                            "code": "ERR",
                            "detail": md.group(1) if md else line.strip()[-180:],
                        })
                        db.add_send_log(source, from_email, to_email, subject, "failed", delivery_info["detail"])
                        return delivery_info
            except Exception:
                pass

    db.add_send_log(source, from_email, to_email, subject, "queued", f"Message-ID {msg_id}")
    return delivery_info


class CampaignWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="vmail-campaign-worker")
        self._stop_evt = threading.Event()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(1.5)

    def _tick(self):
        with db.session(commit=True) as conn:
            camp = conn.execute("SELECT * FROM campaigns WHERE status='running' ORDER BY started_at ASC LIMIT 1").fetchone()
            if not camp:
                return

            # Respect daily limit
            day_start = int(datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            sent_today = conn.execute(
                "SELECT COUNT(*) FROM campaign_items WHERE campaign_id=? AND status='sent' AND sent_at>=?",
                (camp["id"], day_start),
            ).fetchone()[0]
            if camp["daily_limit"] > 0 and sent_today >= camp["daily_limit"]:
                conn.execute(
                    "UPDATE campaigns SET status='paused', last_error=? WHERE id=?",
                    (f"Đã đạt giới hạn {camp['daily_limit']} thư/ngày để bảo vệ uy tín gửi.", camp["id"]),
                )
                return

            item = conn.execute(
                "SELECT * FROM campaign_items WHERE campaign_id=? AND status='pending' LIMIT 1",
                (camp["id"],),
            ).fetchone()

            if not item:
                conn.execute("UPDATE campaigns SET status='completed', finished_at=? WHERE id=?", (int(time.time()), camp["id"]))
                return

            tpl = conn.execute("SELECT * FROM templates WHERE id=?", (camp["template_id"],)).fetchone()
            if not tpl:
                conn.execute("UPDATE campaigns SET status='failed', last_error='Không tìm thấy mẫu email.' WHERE id=?", (camp["id"],))
                return

        name_val = item["name"] or item["email"].split("@")[0]
        public_base = _get_public_base(camp["from_email"])
        subj = (camp["subject"] or tpl["subject"]).replace("{{name}}", name_val).replace("{{email}}", item["email"]).replace("{{webmail_url}}", public_base)
        body_html = tpl["html_body"].replace("{{name}}", html.escape(name_val)).replace("{{email}}", html.escape(item["email"])).replace("{{webmail_url}}", html.escape(public_base))
        body_txt = (tpl["text_body"] or "").replace("{{name}}", name_val).replace("{{email}}", item["email"]).replace("{{webmail_url}}", public_base)

        unsub_url = f"{public_base}/vmail-api/v1/unsubscribe?t={item['tracking_token']}"
        if camp["include_unsub"]:
            body_html += f'<div style="margin-top:20px;padding-top:12px;border-top:1px solid #e5e7eb;font-size:12px;color:#6b7280;text-align:center">Không muốn nhận thêm thư? <a href="{unsub_url}" style="color:#1a73e8">Hủy đăng ký</a></div>'
            body_txt += f"\n\nHủy đăng ký nhận tin: {unsub_url}"
        if camp["track_opens"]:
            pixel = f'<img src="{public_base}/vmail-api/v1/track/open?t={item["tracking_token"]}" width="1" height="1" alt="" style="display:none"/>'
            body_html += pixel

        now = int(time.time())
        last_err = None
        for attempt in range(2):
            try:
                send_email(
                    from_email=camp["from_email"],
                    from_name=camp["from_name"],
                    to_email=item["email"],
                    subject=subj,
                    html_body=body_html,
                    text_body=body_txt,
                    reply_to=camp["reply_to"],
                    unsub_url=unsub_url if camp["include_unsub"] else "",
                    source="campaign",
                    verify_delivery=False,
                )
                last_err = None
                break
            except smtplib.SMTPResponseException as e:
                last_err = e
                if 400 <= e.smtp_code < 500 and attempt == 0:
                    time.sleep(2.0)
                    continue
                break
            except Exception as e:
                last_err = e
                break

        with db.session(commit=True) as conn3:
            if last_err is None:
                conn3.execute("UPDATE campaign_items SET status='sent', sent_at=? WHERE id=?", (now, item["id"]))
                conn3.execute("UPDATE campaigns SET sent_count=sent_count+1 WHERE id=?", (camp["id"],))
            else:
                err_msg = str(last_err)[:240]
                conn3.execute("UPDATE campaign_items SET status='failed', error=?, sent_at=? WHERE id=?", (err_msg, now, item["id"]))
                conn3.execute("UPDATE campaigns SET failed_count=failed_count+1, last_error=? WHERE id=?", (err_msg, camp["id"]))

        delay_sec = max(0.3, min(30.0, (camp["delay_ms"] or 1500) / 1000.0))
        time.sleep(delay_sec)


_WORKER = None


def start_campaign_worker():
    global _WORKER
    if _WORKER is None or not _WORKER.is_alive():
        _WORKER = CampaignWorker()
        _WORKER.start()
