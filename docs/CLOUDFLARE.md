# Tích hợp Cloudflare (DNS, Tunnel & Inbound Email Routing)

Vmail được thiết kế để chạy mượt mà trên cả **VPS truyền thống** lẫn **Server đặt sau NAT / IP nhà mạng** (bị nhà mạng chặn cổng 25 chiều vào hoặc không có IPv4 tĩnh).

---

## Kiến trúc luồng nhận & Webmail qua Cloudflare

```
Internet Sender ──▶ Cloudflare Edge MX (Email Routing)
                         │
                         ▼
             Cloudflare Email Worker (email-routing-worker.js)
                         │  POST https://mail.<primary-domain>/_inbound_bridge
                         │  Headers: X-Bridge-Secret, X-Mail-From, X-Rcpt-To
                         ▼
               Cloudflare Tunnel (cloudflared HTTP/2)
                         │
                         ▼
            Nginx (127.0.0.1:8080) ──┬─▶ /_inbound_bridge ─▶ Vmail Bridge (:8095) ─▶ Core SMTP (:2525)
                                     ├─▶ /admin/          ─▶ Vmail Admin (:8096)
                                     └─▶ /                ─▶ Webmail Gmail UI (PHP-FPM)
```

---

## Tự động hóa 1-Click trong Vmail

Khi cấu hình **Cloudflare Email** + **Global API Key** trong **Vmail Admin -> Tên miền & DNS** (hoặc trong lúc chạy `install.sh`):

1. Bấm **🔄 Đồng bộ toàn bộ Cloudflare** (hoặc chạy `vmail sync`):
   - Quét toàn bộ Zone (tên miền) trong tài khoản Cloudflare và thêm vào Vmail Core.
   - Tự động cập nhật bản ghi **TXT DKIM** sinh tự động từ Vmail Core, **SPF** và **DMARC** cho từng tên miền.
   - Tự động tạo `CNAME mail.<tenmien>` (Proxied) trỏ vào Cloudflare Tunnel và bổ sung hostname vào cấu hình **Cloudflare Tunnel Ingress** -> mỗi tên miền đều có Webmail & Admin riêng tại `https://mail.<tenmien>/`.
   - Bật **Cloudflare Email Routing** và gắn rule **Catch-All** trỏ vào Worker `vmail-inbound` để nhận thư mọi địa chỉ nội bộ về đúng hộp thư.

---

## Chiều gửi Outbound (tránh 100% vào Spam)

- Nếu server đặt tại nhà mạng/VPS bị chặn cổng 25 outbound hoặc chưa có PTR rDNS: cấu hình **SMTP Relay** (Brevo, Resend, SendGrid, Amazon SES, Mailgun...) trực tiếp tại tab **SMTP Relay & Test** trong `/admin/`.
- Kiểm tra điểm DNS `5/5` trực tiếp trong tab **Tên miền & DNS** -> nút **Check DNS**:
  1. `CNAME mail.<domain>` (Webmail & Admin đa tên miền)
  2. `MX` (Cloudflare Email Routing hoặc MX riêng)
  3. `TXT SPF`
  4. `TXT _dmarc`
  5. `TXT DKIM`
