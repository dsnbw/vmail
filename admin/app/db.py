import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

DATA_DIR = "/opt/vmail/data"
ETC_DIR = "/etc/vmail"
DB_PATH = os.path.join(DATA_DIR, "vmail.db")
SECRET_PATH = os.path.join(ETC_DIR, "secret.key")

_LOCK = threading.Lock()


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(ETC_DIR, exist_ok=True)


def get_master_secret() -> bytes:
    ensure_dirs()
    if os.path.exists(SECRET_PATH):
        raw = open(SECRET_PATH, "rb").read().strip()
        if len(raw) >= 32:
            return raw
    key = secrets.token_hex(32).encode("ascii")
    fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def encrypt_secret(plaintext: str) -> str:
    key = hashlib.sha256(get_master_secret()).digest()
    nonce = secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    stream = bytearray()
    counter = 0
    while len(stream) < len(data):
        stream.extend(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    cipher = bytes(a ^ b for a, b in zip(data, stream[: len(data)]))
    mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(nonce + mac + cipher).decode("ascii")


def decrypt_secret(token: str) -> str:
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    if len(raw) < 32:
        raise ValueError("Invalid secret payload")
    nonce, mac, cipher = raw[:16], raw[16:32], raw[32:]
    key = hashlib.sha256(get_master_secret()).digest()
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, expected):
        raise ValueError("Secret MAC mismatch")
    stream = bytearray()
    counter = 0
    while len(stream) < len(cipher):
        stream.extend(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(a ^ b for a, b in zip(cipher, stream[: len(cipher)])).decode("utf-8")


def get_conn() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    with _LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                default_from TEXT NOT NULL,
                allowed_ips TEXT DEFAULT '',
                daily_limit INTEGER DEFAULT 300,
                sent_today INTEGER DEFAULT 0,
                last_reset TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contact_lists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id TEXT PRIMARY KEY,
                list_id TEXT NOT NULL,
                email TEXT NOT NULL,
                name TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                status TEXT DEFAULT 'subscribed',
                created_at INTEGER NOT NULL,
                UNIQUE(list_id, email),
                FOREIGN KEY(list_id) REFERENCES contact_lists(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS templates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                subject TEXT NOT NULL,
                html_body TEXT NOT NULL,
                text_body TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                list_id TEXT NOT NULL,
                template_id TEXT NOT NULL,
                from_email TEXT NOT NULL,
                from_name TEXT DEFAULT 'Vmail',
                reply_to TEXT DEFAULT '',
                subject TEXT NOT NULL,
                delay_ms INTEGER DEFAULT 1500,
                daily_limit INTEGER DEFAULT 250,
                track_opens INTEGER DEFAULT 1,
                include_unsub INTEGER DEFAULT 1,
                status TEXT DEFAULT 'draft',
                total_count INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                open_count INTEGER DEFAULT 0,
                unsub_count INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                started_at INTEGER DEFAULT 0,
                finished_at INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS campaign_items (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                contact_id TEXT NOT NULL,
                email TEXT NOT NULL,
                name TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                error TEXT DEFAULT '',
                tracking_token TEXT NOT NULL UNIQUE,
                opened_at INTEGER DEFAULT 0,
                sent_at INTEGER DEFAULT 0,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS send_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                from_email TEXT NOT NULL,
                to_email TEXT NOT NULL,
                subject TEXT DEFAULT '',
                status TEXT NOT NULL,
                detail TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sender_creds (
                email TEXT PRIMARY KEY,
                encrypted_pass TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kv_config (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            );
            """
        )

        # Seed default contact list & 3 starter templates if empty
        now = int(time.time())
        if cur.execute("SELECT COUNT(*) FROM contact_lists").fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO contact_lists(id, name, description, created_at) VALUES (?,?,?,?)",
                ("list_default", "Khách hàng mặc định", "Danh sách liên hệ khởi tạo sẵn của Vmail", now),
            )

        if cur.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
            starters = [
                (
                    "tpl_notify",
                    "Thông báo hệ thống / Giao dịch",
                    "[Thông báo] Cập nhật tài khoản của {{name}}",
                    """<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;padding:24px;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;color:#1f2937">
  <h2 style="margin:0 0 12px;color:#1a73e8">Xin chào {{name}},</h2>
  <p style="line-height:1.6">Hệ thống vừa ghi nhận cập nhật mới cho địa chỉ email <strong>{{email}}</strong>.</p>
  <p style="line-height:1.6">Nếu bạn cần hỗ trợ thêm, vui lòng phản hồi trực tiếp email này.</p>
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0"/>
  <p style="font-size:12px;color:#6b7280">Gửi từ hệ thống Vmail.</p>
</div>""",
                    "Xin chào {{name}},\n\nHệ thống vừa ghi nhận cập nhật mới cho {{email}}.",
                ),
                (
                    "tpl_welcome",
                    "Chào mừng thành viên mới",
                    "Chào mừng {{name}} đến với hệ thống của chúng tôi!",
                    """<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;padding:28px;background:#f8fafc;border-radius:12px;color:#0f172a">
  <div style="background:#ffffff;padding:24px;border-radius:10px;border:1px solid #e2e8f0">
    <h2 style="margin-top:0;color:#0b57d0">Chào mừng {{name}}!</h2>
    <p style="line-height:1.6">Cảm ơn bạn đã đăng ký nhận tin qua địa chỉ <strong>{{email}}</strong>.</p>
    <p style="line-height:1.6">Chúng tôi sẽ gửi tới bạn những cập nhật hữu ích và kịp thời nhất.</p>
  </div>
</div>""",
                    "Chào mừng {{name}}! Cảm ơn bạn đã đăng ký với {{email}}.",
                ),
                (
                    "tpl_newsletter",
                    "Bản tin / Chiến dịch ưu đãi",
                    "Cập nhật mới dành riêng cho {{name}}",
                    """<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;padding:24px;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;color:#1f2937">
  <span style="display:inline-block;padding:4px 10px;background:#e8f0fe;color:#1a73e8;border-radius:999px;font-size:12px;font-weight:bold">BẢN TIN MỚI</span>
  <h2 style="margin:12px 0;color:#111827">Kính gửi {{name}},</h2>
  <p style="line-height:1.65">Đây là bản tin mới nhất được thiết kế gọn nhẹ, tối ưu tỷ lệ vào Inbox và hiển thị chuẩn trên Webmail.</p>
  <p style="margin-top:20px"><a href="{{webmail_url}}" style="display:inline-block;background:#1a73e8;color:#ffffff;text-decoration:none;padding:10px 20px;border-radius:8px;font-weight:600">Xem chi tiết</a></p>
</div>""",
                    "Kính gửi {{name}}, xem bản tin mới nhất tại {{webmail_url}}",
                ),
            ]
            for tid, name, subj, html_b, txt_b in starters:
                cur.execute(
                    "INSERT INTO templates(id, name, subject, html_body, text_body, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (tid, name, subj, html_b, txt_b, now, now),
                )

        conn.commit()
        conn.close()


def save_sender_cred(email: str, password: str):
    if not email or not password:
        return
    enc = encrypt_secret(password)
    with _LOCK:
        conn = get_conn()
        conn.execute(
            "INSERT INTO sender_creds(email, encrypted_pass, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET encrypted_pass=excluded.encrypted_pass, updated_at=excluded.updated_at",
            (email.strip().lower(), enc, int(time.time())),
        )
        conn.commit()
        conn.close()


def get_sender_cred(email: str):
    conn = get_conn()
    row = conn.execute("SELECT encrypted_pass FROM sender_creds WHERE email=?", (email.strip().lower(),)).fetchone()
    if not row:
        row = conn.execute("SELECT email, encrypted_pass FROM sender_creds ORDER BY updated_at DESC LIMIT 1").fetchone()
        conn.close()
        if not row:
            return None, None
        return row["email"], decrypt_secret(row["encrypted_pass"])
    conn.close()
    return email.strip().lower(), decrypt_secret(row["encrypted_pass"])


def add_send_log(source: str, from_email: str, to_email: str, subject: str, status: str, detail: str = ""):
    with _LOCK:
        conn = get_conn()
        conn.execute(
            "INSERT INTO send_logs(source, from_email, to_email, subject, status, detail, created_at) VALUES (?,?,?,?,?,?,?)",
            (source, from_email, to_email, subject[:200], status, detail[:500], int(time.time())),
        )
        conn.commit()
        conn.close()
