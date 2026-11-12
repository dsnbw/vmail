# Vmail Transactional & Admin API Guide

Vmail cung cấp REST API siêu nhẹ tương thích ứng dụng web/backend (Node.js, Python, PHP/Laravel, Go) để gửi OTP, email giao dịch, theo dõi tỷ lệ mở và quản lý hủy đăng ký.

---

## 1. Health Check (Public)

```http
GET /vmail-api/v1/health
```

Phản hồi mẫu:
```json
{
  "ok": true,
  "service": "Vmail",
  "version": "1.0.0"
}
```

---

## 2. Gửi Email Giao dịch (Transactional Send API)

Tạo **API Key** trong **Vmail Admin (`/admin/`) -> Transactional API**.

### Endpoint

```http
POST /vmail-api/v1/send
Authorization: Bearer vm_xxxxxxxxxxxxxxxxxxxxxxxx
Content-Type: application/json
```

### Request Body

| Trường | Bắt buộc | Mô tả |
|---|---|---|
| `to` | Có | Địa chỉ email người nhận |
| `subject` | Có | Tiêu đề email (chặn CR/LF injection) |
| `html` | Có* | Nội dung HTML (*hoặc truyền `text`) |
| `text` | Tùy chọn | Nội dung plain-text (tự trích xuất từ HTML nếu bỏ trống) |
| `from` | Tùy chọn | Email gửi (phải thuộc domain trên hệ thống Vmail; mặc định lấy theo API Key) |
| `from_name` | Tùy chọn | Tên hiển thị người gửi (mặc định `"Vmail"`) |
| `reply_to` | Tùy chọn | Địa chỉ nhận phản hồi |
| `verify` | Tùy chọn | `true` để đợi tối đa 6s xác minh `250 OK` thực tế từ log MTA/Relay |

### Ví dụ cURL

```bash
curl -X POST https://mail.example.com/vmail-api/v1/send \
  -H "Authorization: Bearer vm_your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "to": "user@gmail.com",
    "from": "noreply@example.com",
    "from_name": "My App",
    "subject": "Mã OTP xác thực đăng nhập",
    "html": "<h2>Mã của bạn: 482910</h2><p>Hết hạn sau 5 phút.</p>",
    "verify": true
  }'
```

### Ví dụ Node.js (`fetch`)

```js
const res = await fetch("https://mail.example.com/vmail-api/v1/send", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${process.env.VMAIL_API_KEY}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    to: "customer@gmail.com",
    subject: "Xác nhận đơn hàng #1024",
    html: "<p>Đơn hàng #1024 đã được xác nhận.</p>",
    verify: true,
  }),
});
const data = await res.json();
```

### Ví dụ Python

```python
import requests

resp = requests.post(
    "https://mail.example.com/vmail-api/v1/send",
    headers={"Authorization": "Bearer vm_your_api_key_here"},
    json={
        "to": "customer@gmail.com",
        "subject": "Xác nhận tài khoản",
        "html": "<p>Chào mừng bạn gia nhập hệ thống.</p>",
        "verify": True,
    },
    timeout=15,
)
print(resp.json())
```

---

## 3. Tracking & Unsubscribe Endpoints

- `GET /vmail-api/v1/track/open?t=<token>`: Ghi nhận lượt mở email trong chiến dịch và trả pixel GIF 1×1.
- `GET /vmail-api/v1/unsubscribe?t=<token>`: Đánh dấu liên hệ `unsubscribed` một chạm (RFC 8058 `List-Unsubscribe=One-Click`).

---

## 4. Các biện pháp bảo mật áp dụng trên API

- Hash API Key bằng **SHA-256** trước khi lưu SQLite (key gốc chỉ hiển thị đúng 1 lần khi tạo).
- Hỗ trợ giới hạn **IP / CIDR Allowlist** và **Daily Quota** trên từng API Key.
- Xác thực tên miền người gửi (`from`) thuộc danh sách tên miền nội bộ Vmail nhằm chống giả mạo domain ngoài.
- Kiểm tra chặt ký tự CR/LF (`\r`, `\n`, `\x00`) ngăn **SMTP Header Injection**.
- Phiên quản trị `/admin/api/*` ký HMAC-SHA256, cookie `HttpOnly; SameSite=Lax; Secure`, kết hợp rate-limit chống brute-force.
