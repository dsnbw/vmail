import concurrent.futures
import json
import os
import re
import socket
import struct
import urllib.error
import urllib.request

import jmap_client

CF_CFG = os.environ.get("VMAIL_CF_CFG", "/etc/vmail/cloudflare.json")
DEFAULT_TUNNEL_SERVICE = os.environ.get("VMAIL_TUNNEL_SERVICE", "http://127.0.0.1:8080")
DEFAULT_INBOUND_WORKER = os.environ.get("VMAIL_INBOUND_WORKER", "vmail-inbound")

QTYPES = {"A": 1, "CNAME": 5, "MX": 15, "TXT": 16}


def load_cf_config() -> dict:
    if os.path.exists(CF_CFG):
        try:
            with open(CF_CFG, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


def load_cf_creds():
    d = load_cf_config()
    return d.get("email", ""), d.get("api_key", "")


def save_cf_creds(email: str, api_key: str, **extra):
    cfg = load_cf_config()
    cfg["email"] = email.strip()
    if api_key is not None and api_key.strip():
        cfg["api_key"] = api_key.strip()
    for k, v in extra.items():
        if v is not None:
            cfg[k] = v
    os.makedirs(os.path.dirname(CF_CFG), exist_ok=True)
    fd = os.open(CF_CFG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _resolve_cf_runtime():
    cfg = load_cf_config()
    acc_id = (cfg.get("account_id") or "").strip()
    tun_id = (cfg.get("tunnel_id") or "").strip()
    worker = (cfg.get("inbound_worker") or DEFAULT_INBOUND_WORKER).strip()
    service = (cfg.get("tunnel_service") or DEFAULT_TUNNEL_SERVICE).strip()
    dirty = False

    if not acc_id:
        r = cf_api("GET", "/accounts?per_page=10")
        accs = r.get("result") or []
        if accs:
            acc_id = accs[0].get("id", "")
            if acc_id:
                cfg["account_id"] = acc_id
                dirty = True

    if acc_id and not tun_id:
        r = cf_api("GET", f"/accounts/{acc_id}/cfd_tunnel?is_deleted=false&per_page=20")
        tuns = r.get("result") or []
        chosen = next((t for t in tuns if t.get("status") == "healthy"), tuns[0] if tuns else None)
        if chosen:
            tun_id = chosen.get("id", "")
            if tun_id:
                cfg["tunnel_id"] = tun_id
                dirty = True

    if dirty and cfg.get("email") and cfg.get("api_key"):
        save_cf_creds(cfg["email"], cfg["api_key"], **cfg)

    target = f"{tun_id}.cfargotunnel.com" if tun_id else ""
    return {
        "account_id": acc_id,
        "tunnel_id": tun_id,
        "tunnel_target": target,
        "tunnel_service": service,
        "inbound_worker": worker,
    }


def cf_api(method: str, endpoint: str, data=None):
    email, key = load_cf_creds()
    if not email or not key:
        return {"success": False, "errors": [{"message": "Chưa cấu hình Cloudflare Email / API Key."}]}
    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": key,
        "Content-Type": "application/json",
    }
    url = f"https://api.cloudflare.com/client/v4{endpoint}"
    req_data = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(err)
        except Exception:
            return {"success": False, "errors": [{"message": err[:300]}]}
    except Exception as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def _encode_qname(name: str) -> bytes:
    parts = [p for p in name.strip(".").split(".") if p]
    out = bytearray()
    for p in parts:
        b = p.encode("idna")
        out.append(len(b))
        out.extend(b)
    out.append(0)
    return bytes(out)


def _decode_name(buf: bytes, offset: int):
    labels = []
    jumped = False
    orig_next = offset
    seen = set()
    while offset < len(buf):
        if offset in seen:
            break
        seen.add(offset)
        length = buf[offset]
        if length == 0:
            if not jumped:
                orig_next = offset + 1
            break
        if (length & 0xC0) == 0xC0:
            ptr = ((length & 0x3F) << 8) | buf[offset + 1]
            if not jumped:
                orig_next = offset + 2
            offset = ptr
            jumped = True
        else:
            offset += 1
            labels.append(buf[offset : offset + length].decode("utf-8", errors="replace"))
            offset += length
    return ".".join(labels), orig_next


def query_dns(name: str, qtype: str = "TXT", server: str = "8.8.8.8", timeout: float = 2.5):
    qcode = QTYPES.get(qtype.upper(), 16)
    tid = int.from_bytes(os.urandom(2), "big")
    header = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    question = _encode_qname(name) + struct.pack("!HH", qcode, 1)
    pkt = header + question
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(pkt, (server, 53))
        resp, _ = sock.recvfrom(4096)
    except Exception:
        if server != "1.1.1.1":
            sock.close()
            return query_dns(name, qtype, "1.1.1.1", timeout)
        return []
    finally:
        sock.close()

    if len(resp) < 12:
        return []
    _, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", resp[:12])
    pos = 12
    for _ in range(qdcount):
        _, pos = _decode_name(resp, pos)
        pos += 4

    results = []
    for _ in range(ancount):
        if pos >= len(resp):
            break
        _, pos = _decode_name(resp, pos)
        if pos + 10 > len(resp):
            break
        rtype, rclass, ttl, rdlen = struct.unpack("!HHIH", resp[pos : pos + 10])
        pos += 10
        rdata = resp[pos : pos + rdlen]
        pos_end = pos + rdlen
        if rtype == 16:  # TXT
            i = 0
            chunks = []
            while i < len(rdata):
                ln = rdata[i]
                i += 1
                chunks.append(rdata[i : i + ln].decode("utf-8", errors="replace"))
                i += ln
            if rtype == qcode:
                results.append("".join(chunks))
        elif rtype == 15:  # MX
            if len(rdata) >= 2:
                pref = struct.unpack("!H", rdata[:2])[0]
                exch, _ = _decode_name(resp, pos + 2)
                if rtype == qcode:
                    results.append(f"{pref} {exch}")
        elif rtype == 5:  # CNAME
            cname, _ = _decode_name(resp, pos)
            if rtype == qcode:
                results.append(cname)
        elif rtype == 1 and len(rdata) == 4:
            if rtype == qcode:
                results.append(".".join(str(b) for b in rdata))
        pos = pos_end
    return results


def parse_zone_records(domain_name: str, zone_text: str):
    if not zone_text:
        return []
    joined = re.sub(r"\(\s*\n\s*", "", zone_text).replace(")\n", "\n")
    joined = re.sub(r'"\s+"', "", joined)
    records = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        m_dkim = re.match(r'^(\S+)\s+IN\s+TXT\s+"(v=DKIM1;[^"]+)"', line)
        if m_dkim:
            rec_name = m_dkim.group(1).rstrip(".")
            records.append({"type": "TXT", "name": rec_name, "content": m_dkim.group(2), "comment": "Vmail DKIM"})
    # Ensure standard SPF & DMARC
    records.append({
        "type": "TXT",
        "name": domain_name,
        "content": "v=spf1 include:spf.brevo.com include:_spf.mx.cloudflare.net ~all",
        "comment": "Vmail SPF (Brevo + Cloudflare)",
    })
    records.append({
        "type": "TXT",
        "name": f"_dmarc.{domain_name}",
        "content": f"v=DMARC1; p=none; rua=mailto:postmaster@{domain_name},mailto:rua@dmarc.brevo.com; aspf=r; adkim=r",
        "comment": "Vmail DMARC",
    })
    return records


def check_domain_dns(domain_obj: dict):
    name = domain_obj.get("name", "")
    zone_file = domain_obj.get("dnsZoneFile") or ""
    parsed = parse_zone_records(name, zone_file)
    dkim_selectors = [r["name"] for r in parsed if "_domainkey" in r["name"]]

    def _run():
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            f_mx = ex.submit(query_dns, name, "MX")
            f_txt = ex.submit(query_dns, name, "TXT")
            f_dmarc = ex.submit(query_dns, f"_dmarc.{name}", "TXT")
            f_dkim = [ex.submit(query_dns, sel, "TXT") for sel in dkim_selectors[:2]]
            f_b1 = ex.submit(query_dns, f"brevo1._domainkey.{name}", "CNAME")
            f_mail_cname = ex.submit(query_dns, f"mail.{name}", "CNAME")
            f_mail_a = ex.submit(query_dns, f"mail.{name}", "A")
            return {
                "mx": f_mx.result(),
                "txt": f_txt.result(),
                "dmarc": f_dmarc.result(),
                "dkim_vmail": [f.result() for f in f_dkim],
                "dkim_brevo": f_b1.result(),
                "webmail": f_mail_cname.result() or f_mail_a.result(),
            }

    live = _run()
    cf_rt = _resolve_cf_runtime() if os.path.exists(CF_CFG) else {}
    tun_target = cf_rt.get("tunnel_target") or "<tunnel-id>.cfargotunnel.com"
    spf_vals = [t for t in live["txt"] if t.lower().startswith("v=spf1")]
    dmarc_vals = [t for t in live["dmarc"] if t.lower().startswith("v=dmarc1")]
    brevo_code = [t for t in live["txt"] if t.lower().startswith("brevo-code:")]
    vmail_dkim_ok = len(dkim_selectors) > 0 and all(bool(v) for v in live["dkim_vmail"])

    mx_ok = len(live["mx"]) > 0
    spf_ok = len(spf_vals) > 0
    dmarc_ok = len(dmarc_vals) > 0
    dkim_any_ok = vmail_dkim_ok or len(live["dkim_brevo"]) > 0
    webmail_ok = len(live["webmail"]) > 0

    records = [
        {
            "type": "CNAME",
            "host": f"mail.{name}",
            "label": f"Webmail & Admin riêng (https://mail.{name})",
            "ok": webmail_ok,
            "actual": live["webmail"],
            "expected": f"CNAME proxied -> {tun_target}",
        },
        {
            "type": "MX",
            "host": name,
            "label": "Nhận thư (Cloudflare Routing / MX)",
            "ok": mx_ok,
            "actual": live["mx"],
            "expected": "10 route1.mx.cloudflare.net (bật Email Routing)",
        },
        {
            "type": "TXT",
            "host": name,
            "label": "SPF (Chống giả mạo người gửi)",
            "ok": spf_ok,
            "actual": spf_vals,
            "expected": "v=spf1 include:spf.brevo.com include:_spf.mx.cloudflare.net ~all",
        },
        {
            "type": "TXT",
            "host": f"_dmarc.{name}",
            "label": "DMARC (Báo cáo & Tin cậy Inbox)",
            "ok": dmarc_ok,
            "actual": dmarc_vals,
            "expected": f"v=DMARC1; p=none; rua=mailto:postmaster@{name}",
        },
    ]
    for idx, sel in enumerate(dkim_selectors[:2]):
        vals = live["dkim_vmail"][idx] if idx < len(live["dkim_vmail"]) else []
        exp = next((r["content"] for r in parsed if r["name"] == sel), "v=DKIM1; ...")
        records.append({
            "type": "TXT",
            "host": sel,
            "label": "DKIM Vmail Core",
            "ok": len(vals) > 0,
            "actual": vals,
            "expected": exp,
        })
    if live["dkim_brevo"] or brevo_code:
        records.append({
            "type": "CNAME",
            "host": f"brevo1._domainkey.{name}",
            "label": "DKIM Brevo Relay",
            "ok": len(live["dkim_brevo"]) > 0,
            "actual": live["dkim_brevo"],
            "expected": "CNAME tới b1.*.dkim.brevo.com",
        })

    checks = [mx_ok, spf_ok, dmarc_ok, dkim_any_ok, webmail_ok]
    score = sum(1 for c in checks if c)

    return {
        "domain": name,
        "score": score,
        "max_score": len(checks),
        "records": records,
        "webmail": {"ok": webmail_ok, "url": f"https://mail.{name}", "values": live["webmail"]},
        "mx": {"ok": mx_ok, "values": live["mx"]},
        "spf": {"ok": spf_ok, "values": spf_vals},
        "dmarc": {"ok": dmarc_ok, "values": dmarc_vals},
        "dkim_vmail": {"ok": vmail_dkim_ok, "selectors": dkim_selectors},
        "dkim_brevo": {"ok": len(live["dkim_brevo"]) > 0, "values": live["dkim_brevo"]},
        "brevo_code": {"ok": len(brevo_code) > 0, "values": brevo_code},
        "expected_records": parsed,
    }


def deploy_inbound_worker(bridge_url: str, bridge_secret: str, worker_name: str = None):
    cf_rt = _resolve_cf_runtime()
    acc_id = cf_rt.get("account_id")
    if not acc_id:
        return {"ok": False, "message": "Không tìm thấy Cloudflare Account ID."}
    wname = (worker_name or cf_rt.get("inbound_worker") or DEFAULT_INBOUND_WORKER).strip()
    worker_js = f"""export default {{
  async email(message, env, ctx) {{
    const rawBuf = await new Response(message.raw).arrayBuffer();
    const resp = await fetch("{bridge_url.rstrip('/')}/_inbound_bridge", {{
      method: "POST",
      headers: {{
        "User-Agent": "Cloudflare-Workers",
        "Content-Type": "application/octet-stream",
        "X-Bridge-Secret": env.BRIDGE_SECRET,
        "X-Mail-From": message.from || "",
        "X-Rcpt-To": message.to || ""
      }},
      body: rawBuf
    }});
    if (!resp.ok) {{
      const txt = (await resp.text()).slice(0, 400);
      if (resp.status === 422 || resp.status === 404) {{
        message.setReject("Recipient rejected: " + txt);
      }} else {{
        throw new Error("Inbound bridge failed: " + resp.status + " " + txt);
      }}
    }}
  }}
}};
"""
    boundary = "----VmailWorkerBoundary7f9a2b"
    metadata = json.dumps({
        "main_module": "worker.js",
        "compatibility_date": "2024-11-01",
        "bindings": [{"type": "secret_text", "name": "BRIDGE_SECRET", "text": bridge_secret}],
    })
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="metadata"\r\n'
        f"Content-Type: application/json\r\n\r\n{metadata}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
        f"Content-Type: application/javascript+module\r\n\r\n{worker_js}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    email, key = load_cf_creds()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{acc_id}/workers/scripts/{wname}",
        data=body,
        headers={
            "X-Auth-Email": email,
            "X-Auth-Key": key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        return {"ok": bool(res.get("success")), "worker": wname, "result": res}
    except urllib.error.HTTPError as e:
        return {"ok": False, "worker": wname, "message": e.read().decode("utf-8", errors="replace")[:300]}


def ensure_webmail_tunnel_hostnames(hostnames: list):
    clean = []
    for h in hostnames:
        h = (h or "").strip().lower()
        if h and h not in clean:
            clean.append(h)
    if not clean:
        return {"ok": True, "added": [], "message": "Không có hostname mới"}
    cf_rt = _resolve_cf_runtime()
    acc_id = cf_rt.get("account_id")
    tun_id = cf_rt.get("tunnel_id")
    tun_svc = cf_rt.get("tunnel_service") or DEFAULT_TUNNEL_SERVICE
    if not acc_id or not tun_id:
        return {"ok": False, "added": [], "message": "Chưa cấu hình Cloudflare Tunnel ID trong /etc/vmail/cloudflare.json"}
    cfg_res = cf_api("GET", f"/accounts/{acc_id}/cfd_tunnel/{tun_id}/configurations")
    if not cfg_res.get("success"):
        return {"ok": False, "added": [], "message": str(cfg_res.get("errors") or "Lỗi đọc Cloudflare Tunnel")}
    cfg = (cfg_res.get("result") or {}).get("config") or {}
    ingress = list(cfg.get("ingress") or [])
    existing_hosts = {str(item.get("hostname") or "").lower(): item for item in ingress if item.get("hostname")}
    catch_all = [item for item in ingress if not item.get("hostname")]
    regular = [item for item in ingress if item.get("hostname")]
    added = []
    for h in clean:
        if h not in existing_hosts:
            regular.append({"hostname": h, "service": tun_svc, "originRequest": {}})
            added.append(h)
    if not added:
        return {"ok": True, "added": [], "message": "Cloudflare Tunnel đã khớp tất cả hostname Webmail"}
    if not catch_all:
        catch_all = [{"service": "http_status:404"}]
    new_cfg = dict(cfg)
    new_cfg["ingress"] = regular + catch_all
    put_res = cf_api(
        "PUT",
        f"/accounts/{acc_id}/cfd_tunnel/{tun_id}/configurations",
        {"config": new_cfg},
    )
    if not put_res.get("success"):
        return {"ok": False, "added": [], "message": str(put_res.get("errors") or "Lỗi cập nhật Cloudflare Tunnel")}
    return {"ok": True, "added": added, "message": f"Đã thêm Tunnel Ingress cho: {', '.join(added)}"}


def _sync_cf_zone(zone_id: str, domain_name: str, domain_obj: dict, cf_rt: dict = None):
    if cf_rt is None:
        cf_rt = _resolve_cf_runtime()
    tun_target = cf_rt.get("tunnel_target") or ""
    inbound_worker = cf_rt.get("inbound_worker") or DEFAULT_INBOUND_WORKER
    logs = []
    recs = parse_zone_records(domain_name, domain_obj.get("dnsZoneFile") or "")
    existing_res = cf_api("GET", f"/zones/{zone_id}/dns_records?per_page=100")
    existing = existing_res.get("result") or []

    for r in recs:
        r_type = r["type"]
        r_name = r["name"]
        clean_content = r["content"].strip('"')
        payload = {
            "type": r_type,
            "name": r_name,
            "content": clean_content,
            "ttl": 1,
            "proxied": False,
            "comment": r.get("comment", ""),
        }
        matches = [x for x in existing if x.get("type") == r_type and x.get("name") == r_name]
        existing_rec = None
        if clean_content.startswith("v=spf1"):
            spf_m = [x for x in matches if x.get("content", "").strip('"').startswith("v=spf1")]
            existing_rec = spf_m[0] if spf_m else None
        elif clean_content.startswith("v=DMARC1"):
            dmarc_m = [x for x in matches if x.get("content", "").strip('"').startswith("v=DMARC1")]
            if dmarc_m:
                logs.append(f"Giữ nguyên {r_type} {r_name} hợp lệ")
                continue
            existing_rec = None
        elif matches:
            existing_rec = matches[0]

        if existing_rec:
            if existing_rec.get("content", "").strip('"') != clean_content:
                res = cf_api("PUT", f"/zones/{zone_id}/dns_records/{existing_rec['id']}", payload)
                logs.append(f"Cập nhật {r_type} {r_name}: {'OK' if res.get('success') else res.get('errors')}")
            else:
                logs.append(f"Đã khớp {r_type} {r_name}")
        else:
            res = cf_api("POST", f"/zones/{zone_id}/dns_records", payload)
            logs.append(f"Tạo mới {r_type} {r_name}: {'OK' if res.get('success') else res.get('errors')}")

    # Ensure Webmail CNAME mail.<domain> -> Cloudflare Tunnel
    webmail_host = f"mail.{domain_name}"
    if tun_target:
        wm_matches = [
            x for x in existing
            if x.get("name") == webmail_host and x.get("type") in ("CNAME", "A", "AAAA")
        ]
        wm_payload = {
            "type": "CNAME",
            "name": webmail_host,
            "content": tun_target,
            "ttl": 1,
            "proxied": True,
            "comment": "Vmail Webmail Multi-Domain",
        }
        if not wm_matches:
            res = cf_api("POST", f"/zones/{zone_id}/dns_records", wm_payload)
            logs.append(f"Tạo Webmail CNAME {webmail_host}: {'OK' if res.get('success') else res.get('errors')}")
        elif len(wm_matches) == 1 and wm_matches[0].get("type") == "CNAME":
            cur = wm_matches[0]
            cur_c = str(cur.get("content") or "").strip(".")
            if cur_c == tun_target and cur.get("proxied") is True:
                logs.append(f"Đã khớp Webmail CNAME {webmail_host}")
            elif "cfargotunnel.com" in cur_c or cur_c == tun_target:
                res = cf_api("PUT", f"/zones/{zone_id}/dns_records/{cur['id']}", wm_payload)
                logs.append(f"Cập nhật Webmail CNAME {webmail_host}: {'OK' if res.get('success') else res.get('errors')}")
            else:
                logs.append(f"Giữ nguyên {webmail_host} (đang trỏ {cur.get('type')} -> {cur_c})")
        else:
            logs.append(f"Giữ nguyên {webmail_host} (đã có bản ghi riêng)")

    # Ensure Cloudflare Email Routing & Catch-All Worker
    er = (cf_api("GET", f"/zones/{zone_id}/email/routing").get("result")) or {}
    if not er.get("enabled"):
        r_en = cf_api("POST", f"/zones/{zone_id}/email/routing/enable", {})
        logs.append(f"Bật Email Routing: {'OK' if r_en.get('success') else r_en.get('errors')}")
    ca = (cf_api("GET", f"/zones/{zone_id}/email/routing/rules/catch_all").get("result")) or {}
    acts = ca.get("actions") or []
    already = ca.get("enabled") is True and len(acts) == 1 and acts[0].get("type") == "worker" and inbound_worker in (acts[0].get("value") or [])
    if not already:
        r_ca = cf_api(
            "PUT",
            f"/zones/{zone_id}/email/routing/rules/catch_all",
            {
                "actions": [{"type": "worker", "value": [inbound_worker]}],
                "matchers": [{"type": "all"}],
                "enabled": True,
                "name": "Route all catch-all to Vmail Inbound Bridge",
            },
        )
        logs.append(f"Cấu hình Catch-All Worker: {'OK' if r_ca.get('success') else r_ca.get('errors')}")
    else:
        logs.append(f"Inbound Worker {inbound_worker}: Đang hoạt động")
    return logs


def fix_single_domain(domain_name: str):
    cf_rt = _resolve_cf_runtime()
    zones_res = cf_api("GET", f"/zones?name={domain_name}")
    if not zones_res.get("success") or not zones_res.get("result"):
        raise RuntimeError(f"Tên miền {domain_name} không nằm trong tài khoản Cloudflare hiện tại.")
    zone = zones_res["result"][0]
    domains = jmap_client.list_domains()
    dom_obj = next((d for d in domains if d.get("name") == domain_name), None)
    if not dom_obj:
        created = jmap_client.create_domain(domain_name)
        domains = jmap_client.list_domains()
        dom_obj = next((d for d in domains if d.get("name") == domain_name), created)
    logs = _sync_cf_zone(zone["id"], domain_name, dom_obj, cf_rt=cf_rt)
    tun_res = ensure_webmail_tunnel_hostnames([f"mail.{domain_name}"])
    logs.append(tun_res.get("message") or "Tunnel checked")
    er_status = next((x for x in logs if "Worker" in x or "Email Routing" in x), f"Worker {cf_rt['inbound_worker']} OK")
    return {"domain": domain_name, "logs": logs, "email_routing": er_status, "tunnel": tun_res, "dns": check_domain_dns(dom_obj)}


def sync_all_cloudflare():
    cf_rt = _resolve_cf_runtime()
    zones_res = cf_api("GET", "/zones?per_page=50")
    if not zones_res.get("success"):
        raise RuntimeError(str(zones_res.get("errors") or "Lỗi kết nối Cloudflare API"))
    zones = zones_res.get("result") or []
    domains = jmap_client.list_domains()
    by_name = {d["name"]: d for d in domains}
    added = []
    summary = []
    webmail_hosts = []
    for z in zones:
        dname = z["name"]
        webmail_hosts.append(f"mail.{dname}")
        if dname not in by_name:
            jmap_client.create_domain(dname)
            added.append(dname)
            domains = jmap_client.list_domains()
            by_name = {d["name"]: d for d in domains}
        dom_obj = by_name[dname]
        logs = _sync_cf_zone(z["id"], dname, dom_obj, cf_rt=cf_rt)
        summary.append({"domain": dname, "logs": logs})
    tun_res = ensure_webmail_tunnel_hostnames(webmail_hosts)
    return {
        "total_cf_zones": len(zones),
        "added_domains": added,
        "webmail_hosts": webmail_hosts,
        "tunnel": tun_res,
        "results": summary,
    }
