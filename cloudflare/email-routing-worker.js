/**
 * Vmail Cloudflare Email Routing Worker
 * Receives inbound emails at Cloudflare Edge (bypassing blocked port 25 on residential/NAT ISPs)
 * and forwards raw RFC-5322 bytes securely to Vmail Inbound Bridge over HTTPS.
 *
 * Required Worker Environment Variables:
 *   - BRIDGE_URL (optional if hardcoded during deployment): e.g. "https://mail.example.com/_inbound_bridge"
 *   - BRIDGE_SECRET (Encrypted Secret): shared HMAC/constant-time secret matching /etc/vmail/inbound_bridge.secret
 */
export default {
  async email(message, env, ctx) {
    const endpoint = env.BRIDGE_URL || "https://{{MAIL_HOST}}/_inbound_bridge";
    const rawBuf = await new Response(message.raw).arrayBuffer();
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: {
        "User-Agent": "Cloudflare-Workers",
        "Content-Type": "application/octet-stream",
        "X-Bridge-Secret": env.BRIDGE_SECRET,
        "X-Mail-From": message.from || "",
        "X-Rcpt-To": message.to || ""
      },
      body: rawBuf
    });
    if (!resp.ok) {
      const txt = (await resp.text()).slice(0, 400);
      if (resp.status === 422 || resp.status === 404) {
        message.setReject("Recipient rejected: " + txt);
      } else {
        throw new Error("Inbound bridge failed: " + resp.status + " " + txt);
      }
    }
  }
};
