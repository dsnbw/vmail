from collections import deque
import datetime
import glob
import json
import os
import re
import socket
import threading
import db

CORE_LOG_GLOB = os.environ.get("VMAIL_CORE_LOG_GLOB", "/var/log/vmail/core.log*")
BRIDGE_LOG = os.environ.get("VMAIL_BRIDGE_LOG", "/var/log/vmail/bridge.log")
CONFIG_PATH = os.environ.get("VMAIL_CONFIG_PATH", "/etc/vmail/config.json")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_CACHE_LOCK = threading.Lock()
_FILE_CACHE = {}  # fp -> {"inode": int, "offset": int, "size": int, "mtime_ns": int, "totals": dict, "by_day": dict, "events": deque}
_MAX_CACHED_EVENTS_PER_FILE = 200


def _core_log_files():
    files = []
    for g in (CORE_LOG_GLOB, "/var/log/vmail/core.log*"):
        if g:
            files.extend(glob.glob(g))
    return sorted(set(files))[-7:]


def _kv_extract(line: str, key: str) -> str:
    m = re.search(rf'\b{re.escape(key)}\s*=\s*"([^"]*)"', line)
    if m:
        return m.group(1)
    m = re.search(rf'\b{re.escape(key)}\s*=\s*([^\s,]+)', line)
    return m.group(1) if m else ""


def _parse_file_incremental(fp: str):
    st = os.stat(fp)
    inode = getattr(st, "st_ino", 0)
    size = st.st_size
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))

    with _CACHE_LOCK:
        entry = _FILE_CACHE.get(fp)
        if entry and entry["inode"] == inode and entry["size"] == size and entry["mtime_ns"] == mtime_ns:
            return entry["totals"], entry["by_day"], list(entry["events"])

        # Reset if file truncated/replaced or unseen
        if not entry or entry["inode"] != inode or size < entry["offset"]:
            entry = {
                "inode": inode,
                "offset": 0,
                "size": 0,
                "mtime_ns": 0,
                "totals": {"delivered": 0, "failed": 0, "inbound": 0, "spam": 0},
                "by_day": {},
                "events": deque(maxlen=_MAX_CACHED_EVENTS_PER_FILE),
            }

        start_offset = entry["offset"]
        totals = dict(entry["totals"])
        by_day = {d: dict(v) for d, v in entry["by_day"].items()}
        events = deque(entry["events"], maxlen=_MAX_CACHED_EVENTS_PER_FILE)

    new_offset = start_offset
    with open(fp, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)
        for raw_line in f:
            line = ANSI_RE.sub("", raw_line).strip()
            if not line or len(line) < 20:
                continue
            day = line[:10]
            ts = line[:20].strip()
            day_bucket = by_day.setdefault(day, {"delivered": 0, "failed": 0, "inbound": 0, "spam": 0})

            if "delivery.delivered" in line:
                totals["delivered"] += 1
                day_bucket["delivered"] += 1
                events.append({
                    "time": ts,
                    "type": "delivered",
                    "status": "delivered",
                    "from": _kv_extract(line, "from"),
                    "to": _kv_extract(line, "to"),
                    "host": _kv_extract(line, "hostname") or "local",
                    "code": _kv_extract(line, "code") or "250",
                    "detail": f"[{_kv_extract(line, 'hostname') or 'local'}] {_kv_extract(line, 'details') or 'Delivered'}",
                    "elapsed": _kv_extract(line, "elapsed"),
                    "queueId": _kv_extract(line, "queueId"),
                })
            elif any(k in line for k in ("delivery.message-rejected", "delivery.auth-failed", "delivery.dsn-perm-fail", "delivery.temp-fail")):
                totals["failed"] += 1
                day_bucket["failed"] += 1
                events.append({
                    "time": ts,
                    "type": "failed",
                    "status": "failed",
                    "from": _kv_extract(line, "from"),
                    "to": _kv_extract(line, "to"),
                    "host": _kv_extract(line, "hostname") or "-",
                    "code": _kv_extract(line, "code") or "ERR",
                    "detail": _kv_extract(line, "details") or _kv_extract(line, "reason") or line[25:140].strip(),
                    "elapsed": _kv_extract(line, "elapsed"),
                    "queueId": _kv_extract(line, "queueId"),
                })
            elif "message-ingest.ham" in line:
                totals["inbound"] += 1
                day_bucket["inbound"] += 1
                events.append({
                    "time": ts,
                    "type": "inbound",
                    "status": "inbound",
                    "from": _kv_extract(line, "from"),
                    "to": _kv_extract(line, "to"),
                    "host": "Inbound MX",
                    "code": "200",
                    "detail": f"Đã nhận vào hộp thư ({_kv_extract(line, 'size') or '0'} bytes)",
                    "elapsed": _kv_extract(line, "elapsed"),
                    "queueId": _kv_extract(line, "queueId"),
                })
            elif "message-ingest.spam" in line or ("spam." in line and ("reject" in line.lower() or "discard" in line.lower() or "junk" in line.lower() or "classified" in line.lower())):
                if "spam.model-not-found" not in line:
                    totals["spam"] += 1
                    day_bucket["spam"] += 1
                    events.append({
                        "time": ts,
                        "type": "spam",
                        "status": "spam",
                        "from": _kv_extract(line, "from"),
                        "to": _kv_extract(line, "to"),
                        "host": "Anti-Spam",
                        "code": "SPAM",
                        "detail": line[25:140].strip(),
                        "elapsed": "",
                        "queueId": _kv_extract(line, "queueId"),
                    })
        new_offset = f.tell()

    with _CACHE_LOCK:
        if len(_FILE_CACHE) > 20:
            _FILE_CACHE.clear()
        _FILE_CACHE[fp] = {
            "inode": inode,
            "offset": new_offset,
            "size": size,
            "mtime_ns": mtime_ns,
            "totals": totals,
            "by_day": by_day,
            "events": events,
        }
    return totals, by_day, list(events)


def _tail_lines(path: str, max_bytes: int = 65536, max_lines: int = 120):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - max_bytes), os.SEEK_SET)
            raw = f.read().decode("utf-8", errors="replace")
        return raw.splitlines()[-max_lines:]
    except Exception:
        return []


def parse_logs(limit: int = 120):
    files = _core_log_files()
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    daily = {}
    for i in range(6, -1, -1):
        d = (datetime.datetime.utcnow() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        daily[d] = {"date": d, "delivered": 0, "failed": 0, "inbound": 0, "spam": 0}

    events = []
    totals = {"delivered": 0, "failed": 0, "inbound": 0, "spam": 0}
    today_totals = {"delivered": 0, "failed": 0, "inbound": 0, "spam": 0}

    for fp in files:
        try:
            f_tot, f_days, f_evts = _parse_file_incremental(fp)
            for k in totals:
                totals[k] += f_tot.get(k, 0)
            for day, vals in f_days.items():
                if day in daily:
                    for k in totals:
                        daily[day][k] += vals.get(k, 0)
                if day == today:
                    for k in totals:
                        today_totals[k] += vals.get(k, 0)
            events.extend(f_evts)
        except Exception:
            pass

    if os.path.exists(BRIDGE_LOG):
        for line in _tail_lines(BRIDGE_LOG, max_bytes=65536, max_lines=100):
            m = re.search(r"\[MailBridge\] Delivered (\S+) -> (\S+) \((\d+ bytes)\)", line)
            if m:
                events.append({
                    "time": "Inbound CF",
                    "type": "inbound",
                    "status": "inbound",
                    "from": m.group(1),
                    "to": m.group(2),
                    "host": "Cloudflare Email Routing",
                    "code": "200",
                    "detail": f"Nhận thư vào hộp thư ({m.group(3)})",
                    "elapsed": "-",
                    "queueId": "-",
                })

    try:
        with db.session(commit=False) as conn:
            rows = conn.execute(
                "SELECT source, from_email, to_email, subject, status, detail, created_at FROM send_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        vmail_logs = [
            {
                "time": datetime.datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                "source": r["source"],
                "from": r["from_email"],
                "to": r["to_email"],
                "subject": r["subject"],
                "status": r["status"],
                "detail": r["detail"],
            }
            for r in rows
        ]
    except Exception:
        vmail_logs = []

    events_sorted = list(reversed(events[-limit:]))
    return {
        "totals": totals,
        "today": today_totals,
        "daily": [daily[k] for k in sorted(daily)],
        "events": events_sorted,
        "vmail_logs": vmail_logs,
    }


def _check_port(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _detect_web_port() -> int:
    env_p = os.environ.get("VMAIL_HTTP_PORT", "").strip()
    if env_p.isdigit():
        return int(env_p)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                p = int(json.load(f).get("http_port") or 0)
            if p > 0:
                return p
        except Exception:
            pass
    for cand in (8080, 80, 443):
        if _check_port(cand):
            return cand
    return 8080


def _proc_stats(patterns):
    if isinstance(patterns, str):
        patterns = (patterns,)
    total_kb = 0
    found = False
    try:
        proc_entries = os.listdir("/proc")
    except Exception:
        return False, 0.0
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as cf:
                cmd = cf.read().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            if any(p in cmd for p in patterns):
                found = True
                with open(f"/proc/{entry}/status", "r", encoding="utf-8", errors="ignore") as sf:
                    for line in sf:
                        if line.startswith("VmRSS:"):
                            total_kb += int(line.split()[1])
                            break
        except Exception:
            pass
    return found, round(total_kb / 1024.0, 1)


def get_system_health():
    mem = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0]) // 1024
    except Exception:
        pass
    total_mb = mem.get("MemTotal", 0)
    avail_mb = mem.get("MemAvailable", 0)
    used_mb = max(0, total_mb - avail_mb)

    load_1m = "0.00"
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as f:
            load_1m = f.read().split()[0]
    except Exception:
        pass

    try:
        st = os.statvfs("/")
        disk_total_gb = round((st.f_blocks * st.f_frsize) / (1024**3), 1)
        disk_free_gb = round((st.f_bavail * st.f_frsize) / (1024**3), 1)
    except Exception:
        disk_total_gb, disk_free_gb = 0, 0

    web_port = _detect_web_port()
    ports = {
        "SMTP 4650": _check_port(4650),
        "IMAP 9930": _check_port(9930),
        "MTA 8090": _check_port(8090),
        "Inbound 8095": _check_port(8095),
        "Vmail 8096": _check_port(8096),
        f"Web {web_port}": _check_port(web_port),
    }

    proc_defs = [
        ("Vmail Core Engine", os.environ.get("VMAIL_CORE_BIN", "/usr/local/bin/vmail-core")),
        ("Vmail Control Panel", "/opt/vmail/app/server.py"),
        ("Inbound CF Bridge", ("mail_inbound_bridge.py", "vmail-bridge")),
        ("Cloudflared Tunnel", "cloudflared tunnel"),
    ]
    processes = []
    for label, pattern in proc_defs:
        up, rss_mb = _proc_stats(pattern)
        processes.append({"name": label, "running": up, "rss_mb": rss_mb})

    return {
        "ram_total_mb": total_mb,
        "ram_used_mb": used_mb,
        "ram_avail_mb": avail_mb,
        "disk_total_gb": disk_total_gb,
        "disk_free_gb": disk_free_gb,
        "load_1m": load_1m,
        "ports": ports,
        "processes": processes,
        "services": {
            "vmail_core": ports["MTA 8090"],
            "smtp_submission": ports["SMTP 4650"],
            "imap_server": ports["IMAP 9930"],
            "inbound_bridge": ports["Inbound 8095"],
            "vmail_panel": True,
            "webmail_nginx": ports[f"Web {web_port}"],
            "cloudflared": processes[3]["running"],
        },
    }
