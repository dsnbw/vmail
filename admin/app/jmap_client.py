import base64
import json
import os
import threading
import urllib.error
import urllib.request

VMAIL_CORE_URL = os.environ.get("VMAIL_CORE_URL", "http://127.0.0.1:8090")
ENGINE_CFG = os.environ.get("VMAIL_ENGINE_CFG", "/etc/vmail/engine.json")

_CAP_CACHE = None
_LOCK = threading.Lock()


def _load_engine_creds():
    if os.path.exists(ENGINE_CFG):
        try:
            with open(ENGINE_CFG, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("user") and d.get("password"):
                return d["user"], d["password"]
        except Exception:
            pass
    u = os.environ.get("VMAIL_CORE_USER", "").strip()
    p = os.environ.get("VMAIL_CORE_PASSWORD", "")
    if u and p:
        return u, p
    raise RuntimeError("Chưa cấu hình thông tin xác thực Vmail Core Engine (/etc/vmail/engine.json).")


def save_engine_creds(user: str, password: str):
    os.makedirs(os.path.dirname(ENGINE_CFG), exist_ok=True)
    fd = os.open(ENGINE_CFG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"user": user, "password": password}, f)


def _basic_header(user: str, password: str) -> str:
    tok = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {tok}"


def get_session(user: str = None, password: str = None):
    if user is None or password is None:
        user, password = _load_engine_creds()
    req = urllib.request.Request(
        f"{VMAIL_CORE_URL}/jmap/session",
        headers={"Authorization": _basic_header(user, password)},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_caps(auth_header: str):
    global _CAP_CACHE
    if _CAP_CACHE:
        return _CAP_CACHE
    with _LOCK:
        if _CAP_CACHE:
            return _CAP_CACHE
        req = urllib.request.Request(f"{VMAIL_CORE_URL}/jmap/session", headers={"Authorization": auth_header})
        with urllib.request.urlopen(req, timeout=8) as r:
            sess = json.loads(r.read().decode("utf-8"))
        _CAP_CACHE = list(sess.get("capabilities", {}).keys())
        return _CAP_CACHE


def jmap_call(calls: list, user: str = None, password: str = None):
    if user is None or password is None:
        user, password = _load_engine_creds()
    auth = _basic_header(user, password)
    caps = _get_caps(auth)
    payload = json.dumps({"using": caps, "methodCalls": calls}).encode("utf-8")
    req = urllib.request.Request(
        f"{VMAIL_CORE_URL}/jmap",
        data=payload,
        headers={"Authorization": auth, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        res = json.loads(r.read().decode("utf-8"))
    return res.get("methodResponses", [])


def verify_admin_login(username: str, password: str):
    if not username or not password:
        return False, None, "Vui lòng nhập tài khoản và mật khẩu."
    try:
        get_session(username, password)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, None, "Sai tên đăng nhập hoặc mật khẩu."
        return False, None, f"Lỗi xác thực engine (HTTP {e.code})."
    except Exception as e:
        return False, None, f"Không kết nối được Vmail Core Engine: {e}"

    try:
        res = jmap_call([["x:Account/get", {"accountId": "0", "ids": None}, "a"]], username, password)
        accts = res[0][1].get("list", [])
        uname_l = username.strip().lower()
        matched = None
        for a in accts:
            if (a.get("name") or "").lower() == uname_l or (a.get("emailAddress") or "").lower() == uname_l:
                matched = a
                break
        if not matched:
            return False, None, "Không tìm thấy hồ sơ tài khoản."
        role = (matched.get("roles") or {}).get("@type")
        if role != "Admin":
            return False, None, "Tài khoản này không có quyền quản trị hệ thống."
        # Persist validated engine creds if admin logs in
        save_engine_creds(username, password)
        return True, {
            "id": matched.get("id"),
            "name": matched.get("name"),
            "email": matched.get("emailAddress"),
            "description": matched.get("description") or matched.get("name"),
        }, ""
    except Exception as e:
        return False, None, f"Lỗi kiểm tra quyền quản trị: {e}"


def _extract_set_err(resp_payload: dict, key: str, action: str = "create"):
    if action == "create":
        nc = (resp_payload.get("notCreated") or {}).get(key)
        if nc:
            raise RuntimeError(nc.get("description") or nc.get("type") or str(nc))
        return (resp_payload.get("created") or {}).get(key)
    if action == "update":
        nu = (resp_payload.get("notUpdated") or {}).get(key)
        if nu:
            raise RuntimeError(nu.get("description") or nu.get("type") or str(nu))
        return (resp_payload.get("updated") or {}).get(key)
    if action == "destroy":
        nd = (resp_payload.get("notDestroyed") or {}).get(key)
        if nd:
            raise RuntimeError(nd.get("description") or nd.get("type") or str(nd))
        return True
    return None


def reload_settings():
    res = jmap_call([["x:Action/set", {"accountId": "0", "create": {"a": {"@type": "ReloadSettings"}}}, "act"]])
    _extract_set_err(res[0][1], "a", "create")
    return True


def reload_lookups():
    res = jmap_call([["x:Action/set", {"accountId": "0", "create": {"a": {"@type": "ReloadLookupStores"}}}, "act"]])
    _extract_set_err(res[0][1], "a", "create")
    return True


def list_domains():
    res = jmap_call([["x:Domain/get", {"accountId": "0", "ids": None}, "d"]])
    return res[0][1].get("list", [])


def create_domain(name: str):
    name = name.strip().lower()
    payload = {
        "name": name,
        "enable": True,
        "allowScimProvisioning": False,
        "allowRelaying": False,
        "reportAddressUri": f"mailto:postmaster@{name}",
        "subAddressing": {"@type": "Enabled"},
        "aliases": {},
        "dkimManagement": {
            "@type": "Automatic",
            "algorithms": {"Dkim1Ed25519Sha256": True, "Dkim1RsaSha256": True},
            "selectorTemplate": "v{version}-{algorithm}-{date-%Y%m%d}",
            "rotateAfter": 7776000000,
            "retireAfter": 604800000,
            "deleteAfter": 2592000000,
        },
        "certificateManagement": {"@type": "Manual"},
        "dnsManagement": {"@type": "Manual"},
    }
    res = jmap_call([["x:Domain/set", {"accountId": "0", "create": {"d1": payload}}, "d"]])
    return _extract_set_err(res[0][1], "d1", "create")


def update_domain(domain_id: str, patch: dict):
    res = jmap_call([["x:Domain/set", {"accountId": "0", "update": {domain_id: patch}}, "d"]])
    return _extract_set_err(res[0][1], domain_id, "update")


def delete_domain(domain_id: str):
    res = jmap_call([["x:Domain/set", {"accountId": "0", "destroy": [domain_id]}, "d"]])
    return _extract_set_err(res[0][1], domain_id, "destroy")


def list_accounts():
    res = jmap_call([["x:Account/get", {"accountId": "0", "ids": None}, "a"]])
    return res[0][1].get("list", [])


def create_account(local_part: str, domain_id: str, password: str, description: str = "", role: str = "User", quota_mb: int = 0, aliases: list = None):
    payload = {
        "@type": "User",
        "name": local_part.strip().lower(),
        "domainId": domain_id,
        "description": description.strip() or local_part.strip(),
        "credentials": {"0": {"@type": "Password", "secret": password}},
        "roles": {"@type": role if role in ("Admin", "User") else "User"},
        "locale": "vi-VN",
    }
    if quota_mb and int(quota_mb) > 0:
        payload["quotas"] = {"maxDiskQuota": int(quota_mb) * 1024 * 1024}
    if aliases:
        alias_map = {}
        idx = 0
        for al in aliases:
            al = al.strip().lower()
            if "@" in al:
                al_local, _ = al.split("@", 1)
            else:
                al_local = al
            if al_local:
                alias_map[str(idx)] = {"name": al_local, "domainId": domain_id, "enable": True}
                idx += 1
        if alias_map:
            payload["aliases"] = alias_map

    res = jmap_call([["x:Account/set", {"accountId": "0", "create": {"a1": payload}}, "a"]])
    return _extract_set_err(res[0][1], "a1", "create")


def update_account(account_id: str, patch: dict):
    res = jmap_call([["x:Account/set", {"accountId": "0", "update": {account_id: patch}}, "a"]])
    return _extract_set_err(res[0][1], account_id, "update")


def delete_account(account_id: str):
    res = jmap_call([["x:Account/set", {"accountId": "0", "destroy": [account_id]}, "a"]])
    return _extract_set_err(res[0][1], account_id, "destroy")


def get_relays_and_strategy():
    res = jmap_call([
        ["x:MtaRoute/get", {"accountId": "0", "ids": None}, "r"],
        ["x:MtaOutboundStrategy/get", {"accountId": "0", "ids": ["singleton"]}, "s"],
    ])
    routes = res[0][1].get("list", [])
    strat = (res[1][1].get("list") or [{}])[0]
    return routes, strat


def upsert_relay(route_name: str, host: str, port: int, implicit_tls: bool, username: str, secret: str, description: str = ""):
    routes, _ = get_relays_and_strategy()
    existing = next((r for r in routes if r.get("name") == route_name), None)
    payload = {
        "description": description or route_name,
        "host": host.strip(),
        "port": int(port),
        "protocol": "smtp",
        "implicitTls": bool(implicit_tls),
        "allowInvalidCerts": False,
        "authUsername": username.strip(),
    }
    if secret and secret != "****":
        payload["authSecret"] = {"@type": "Value", "secret": secret}

    if existing:
        rid = existing["id"]
        res = jmap_call([["x:MtaRoute/set", {"accountId": "0", "update": {rid: payload}}, "r"]])
        _extract_set_err(res[0][1], rid, "update")
    else:
        payload["@type"] = "Relay"
        payload["name"] = route_name
        if "authSecret" not in payload:
            payload["authSecret"] = {"@type": "Value", "secret": ""}
        res = jmap_call([["x:MtaRoute/set", {"accountId": "0", "create": {"r1": payload}}, "r"]])
        _extract_set_err(res[0][1], "r1", "create")
    reload_settings()
    return True


def set_default_outbound_route(route_name: str):
    patch = {
        "route": {
            "match": {"0": {"if": "is_local_domain(rcpt_domain)", "then": "'local'"}},
            "else": f"'{route_name}'",
        }
    }
    res = jmap_call([["x:MtaOutboundStrategy/set", {"accountId": "0", "update": {"singleton": patch}}, "s"]])
    _extract_set_err(res[0][1], "singleton", "update")
    reload_settings()
    return True


def get_spam_bundle():
    res = jmap_call([
        ["x:SpamSettings/get", {"accountId": "0", "ids": ["singleton"]}, "sp"],
        ["x:HttpLookup/get", {"accountId": "0", "ids": None}, "hl"],
    ])
    settings = (res[0][1].get("list") or [{}])[0]
    lookups = res[1][1].get("list", [])
    mkeys = []
    for ns in ("trusted-domains", "blocked-domains", "spam-traps"):
        qr = jmap_call([["x:MemoryLookupKey/query", {"accountId": "0", "filter": {"namespace": ns}, "limit": 200}, "q"]])
        ids = qr[0][1].get("ids") or []
        if ids:
            gr = jmap_call([["x:MemoryLookupKey/get", {"accountId": "0", "ids": ids}, "g"]])
            mkeys.extend(gr[0][1].get("list", []))
    return settings, mkeys, lookups


def update_spam_settings(patch: dict):
    res = jmap_call([["x:SpamSettings/set", {"accountId": "0", "update": {"singleton": patch}}, "sp"]])
    _extract_set_err(res[0][1], "singleton", "update")
    reload_settings()
    return True


def add_memory_key(namespace: str, key: str, is_glob: bool = False):
    payload = {"namespace": namespace, "key": key.strip().lower(), "isGlobPattern": bool(is_glob)}
    res = jmap_call([["x:MemoryLookupKey/set", {"accountId": "0", "create": {"k1": payload}}, "ml"]])
    out = _extract_set_err(res[0][1], "k1", "create")
    reload_lookups()
    return out


def delete_memory_key(kid: str):
    res = jmap_call([["x:MemoryLookupKey/set", {"accountId": "0", "destroy": [kid]}, "ml"]])
    _extract_set_err(res[0][1], kid, "destroy")
    reload_lookups()
    return True


def toggle_http_lookup(hid: str, enable: bool):
    res = jmap_call([["x:HttpLookup/set", {"accountId": "0", "update": {hid: {"enable": bool(enable)}}}, "hl"]])
    _extract_set_err(res[0][1], hid, "update")
    reload_settings()
    return True


def list_queue():
    res = jmap_call([["x:QueuedMessage/get", {"accountId": "0", "ids": None}, "q"]])
    return res[0][1].get("list", [])


def delete_queue(qid: str):
    res = jmap_call([["x:QueuedMessage/set", {"accountId": "0", "destroy": [qid]}, "q"]])
    _extract_set_err(res[0][1], qid, "destroy")
    return True
