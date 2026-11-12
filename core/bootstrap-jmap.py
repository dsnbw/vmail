#!/usr/bin/env python3
"""
Vmail Core JMAP Bootstrapper
Self-healing & idempotent setup for BOTH fresh installations (Bootstrap Mode)
and existing cores:
- Completes Bootstrap Mode (x:Bootstrap/set) if active & saves generated permanent admin creds
- Pins JMAP/HTTP/SMTP/IMAP/POP3/ManageSieve ports (8090/8443/2525/4650/1430/9930/9950/4190)
  so Nginx owns port 8080/80 cleanly without conflicts
- Configures clean log path /var/log/vmail/core.log (no ANSI garbage)
- Configures Vmail ESMTP greeting, automatic DKIM signing, Anti-Spam rules
- Ensures primary domain + user-requested admin mailbox & password exist
- Configures optional outbound SMTP Relay & saves encrypted creds in SQLite
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.environ.get("VMAIL_APP_DIR", "/opt/vmail/app"))
import db
import jmap_client


TARGET_LISTENERS = [
    {"name": "smtp", "bind": {"[::]:2525": True}, "protocol": "smtp", "useTls": True, "implicitTls": False},
    {"name": "submissions", "bind": {"[::]:4650": True}, "protocol": "smtp", "useTls": True, "implicitTls": True},
    {"name": "imap", "bind": {"[::]:1430": True}, "protocol": "imap", "useTls": True, "implicitTls": False},
    {"name": "imaps", "bind": {"[::]:9930": True}, "protocol": "imap", "useTls": True, "implicitTls": True},
    {"name": "pop3s", "bind": {"[::]:9950": True}, "protocol": "pop3", "useTls": True, "implicitTls": True},
    {"name": "sieve", "bind": {"[::]:4190": True}, "protocol": "manageSieve", "useTls": True, "implicitTls": False},
    {"name": "https", "bind": {"[::]:8443": True}, "protocol": "http", "useTls": True, "implicitTls": True},
    {"name": "http", "bind": {"[::]:8090": True}, "protocol": "http", "useTls": True, "implicitTls": False},
]


def _probe_base_url(user: str, password: str) -> str:
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    candidates = [os.environ.get("VMAIL_CORE_URL", "").strip(), "http://127.0.0.1:8090", "http://127.0.0.1:8080"]
    seen = set()
    for base in candidates:
        if not base or base in seen:
            continue
        seen.add(base)
        try:
            req = urllib.request.Request(f"{base}/jmap/session", headers={"Authorization": auth})
            with urllib.request.urlopen(req, timeout=4) as r:
                if r.status == 200:
                    return base
        except Exception:
            continue
    return "http://127.0.0.1:8090"


def _raw_jmap(base_url: str, user: str, password: str, calls: list):
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    with urllib.request.urlopen(urllib.request.Request(f"{base_url}/jmap/session", headers={"Authorization": auth}), timeout=8) as r:
        sess = json.loads(r.read().decode("utf-8"))
    caps = list(sess.get("capabilities", {}).keys())
    payload = json.dumps({"using": caps, "methodCalls": calls}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/jmap",
        data=payload,
        headers={"Authorization": auth, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8")).get("methodResponses", [])


def _maybe_complete_bootstrap(base_url: str, boot_user: str, boot_pass: str, domain: str, mail_host: str):
    """Detect if Vmail Core is in initial Bootstrap Mode and complete it cleanly."""
    try:
        resp = _raw_jmap(base_url, boot_user, boot_pass, [["x:Bootstrap/get", {"accountId": "0", "ids": ["singleton"]}, "b0"]])
    except Exception:
        return boot_user, boot_pass, False

    if not resp or resp[0][0] != "x:Bootstrap/get":
        return boot_user, boot_pass, False
    items = (resp[0][1] or {}).get("list") or []
    if not items:
        return boot_user, boot_pass, False

    print("[Bootstrap] Core is in fresh Bootstrap Mode -> completing initial setup...", flush=True)
    b_res = _raw_jmap(
        base_url,
        boot_user,
        boot_pass,
        [[
            "x:Bootstrap/set",
            {
                "accountId": "0",
                "update": {
                    "singleton": {
                        "defaultDomain": domain,
                        "defaultHostname": mail_host,
                        "generateDkimKeys": True,
                        "requestTlsCertificate": False,
                        "tracer": {
                            "@type": "Log",
                            "path": "/var/log/vmail/",
                            "prefix": "core.log",
                            "rotate": "daily",
                            "ansi": False,
                            "enable": True,
                            "level": "info",
                            "lossy": False,
                            "multiline": False,
                            "events": {},
                            "eventsPolicy": "exclude",
                        },
                    }
                },
            },
            "b1",
        ]],
    )
    upd = ((b_res[0][1] or {}).get("updated") or {}).get("singleton") or {}
    not_upd = (b_res[0][1] or {}).get("notUpdated") or {}
    if not_upd:
        raise RuntimeError(f"Bootstrap failed: {not_upd}")
    perm_user = upd.get("username") or boot_user
    perm_pass = upd.get("secret") or boot_pass
    print(f"[Bootstrap] Bootstrap complete! Core admin credential initialized ({perm_user}).", flush=True)
    return perm_user, perm_pass, True


def _ensure_listeners_and_tracer(base_url: str, user: str, password: str):
    resp = _raw_jmap(
        base_url,
        user,
        password,
        [
            ["x:NetworkListener/get", {"accountId": "0"}, "nl"],
            ["x:Tracer/get", {"accountId": "0"}, "tr"],
            ["x:MtaRoute/get", {"accountId": "0"}, "mr"],
        ],
    )
    listeners = (resp[0][1] or {}).get("list") or []
    tracers = (resp[1][1] or {}).get("list") or []
    routes = (resp[2][1] or {}).get("list") or []

    by_name = {x.get("name"): x for x in listeners if x.get("name")}
    create_nl = {}
    update_nl = {}
    for spec in TARGET_LISTENERS:
        cur = by_name.get(spec["name"])
        if cur:
            if cur.get("bind") != spec["bind"] or cur.get("implicitTls") != spec["implicitTls"]:
                update_nl[cur["id"]] = {
                    "bind": spec["bind"],
                    "protocol": spec["protocol"],
                    "useTls": spec["useTls"],
                    "implicitTls": spec["implicitTls"],
                }
        else:
            create_nl[f"new_{spec['name']}"] = spec

    calls = []
    if create_nl or update_nl:
        arg = {"accountId": "0"}
        if create_nl:
            arg["create"] = create_nl
        if update_nl:
            arg["update"] = update_nl
        calls.append(["x:NetworkListener/set", arg, "nlset"])

    # Ensure tracer writes plain daily logs into /var/log/vmail/core.log
    log_tracer = next((t for t in tracers if t.get("@type") == "Log"), None)
    if log_tracer:
        if log_tracer.get("path") != "/var/log/vmail/" or log_tracer.get("prefix") != "core.log" or log_tracer.get("ansi") is not False:
            calls.append([
                "x:Tracer/set",
                {
                    "accountId": "0",
                    "update": {
                        log_tracer["id"]: {
                            "path": "/var/log/vmail/",
                            "prefix": "core.log",
                            "ansi": False,
                            "enable": True,
                        }
                    },
                },
                "trset",
            ])
    else:
        calls.append([
            "x:Tracer/set",
            {
                "accountId": "0",
                "create": {
                    "main_log": {
                        "@type": "Log",
                        "path": "/var/log/vmail/",
                        "prefix": "core.log",
                        "rotate": "daily",
                        "ansi": False,
                        "enable": True,
                        "level": "info",
                        "lossy": False,
                        "multiline": False,
                        "events": {},
                        "eventsPolicy": "exclude",
                    }
                },
            },
            "trset",
        ])

    # Ensure standard mx and local routes exist
    route_names = {r.get("name"): r for r in routes}
    create_rt = {}
    if "mx" not in route_names:
        create_rt["mx"] = {"@type": "Mx", "name": "mx", "description": "MX direct delivery route", "ipLookupStrategy": "v4ThenV6", "maxMultihomed": 2, "maxMxHosts": 2}
    if "local" not in route_names:
        create_rt["local"] = {"@type": "Local", "name": "local", "description": "Local delivery route"}
    if create_rt:
        calls.append(["x:MtaRoute/set", {"accountId": "0", "create": create_rt}, "rtset"])

    if calls:
        _raw_jmap(base_url, user, password, calls)
        try:
            _raw_jmap(base_url, user, password, [["x:ServerOperation", {"accountId": "0", "action": "ReloadSettings"}, "rel"]])
        except Exception:
            pass
        print("[Bootstrap] Listeners & log tracer synchronized.", flush=True)


def main():
    p = argparse.ArgumentParser(description="Bootstrap Vmail Core Engine settings via JMAP")
    p.add_argument("--domain", required=True)
    p.add_argument("--mail-host", required=True)
    p.add_argument("--boot-user", default="admin")
    p.add_argument("--boot-pass", default="")
    p.add_argument("--admin-user", default="admin")
    p.add_argument("--admin-pass", required=True)
    p.add_argument("--admin-email", required=True)
    p.add_argument("--relay-host", default="")
    p.add_argument("--relay-port", type=int, default=587)
    p.add_argument("--relay-user", default="")
    p.add_argument("--relay-secret", default="")
    p.add_argument("--relay-tls", action="store_true")
    args = p.parse_args()

    init_user = args.boot_user or args.admin_user
    init_pass = args.boot_pass or args.admin_pass
    base_url = _probe_base_url(init_user, init_pass)
    os.environ["VMAIL_CORE_URL"] = base_url

    eng_user, eng_pass, did_bootstrap = _maybe_complete_bootstrap(base_url, init_user, init_pass, args.domain, args.mail_host)

    # Save active engine credentials & point jmap_client to active base URL
    jmap_client.VMAIL_CORE_URL = base_url
    jmap_client._CAP_CACHE = None
    jmap_client.save_engine_creds(eng_user, eng_pass)

    # Ensure isolated ports (8090 frees 8080 for Nginx) & clean /var/log/vmail/core.log
    _ensure_listeners_and_tracer(base_url, eng_user, eng_pass)

    # Re-probe onto 8090 if core switched from 8080 -> 8090
    new_base = _probe_base_url(eng_user, eng_pass)
    jmap_client.VMAIL_CORE_URL = new_base
    jmap_client._CAP_CACHE = None

    # System settings, Vmail ESMTP greeting, bridge Received header, Anti-Spam
    calls = [
        ["x:SystemSettings/set", {"accountId": "0", "update": {"singleton": {
            "defaultHostname": args.mail_host,
        }}}, "sys"],
        ["x:MtaStageConnect/set", {"accountId": "0", "update": {"singleton": {
            "smtpGreeting": {"match": {}, "else": "system('hostname') + ' Vmail ESMTP ready'"},
        }}}, "conn"],
        ["x:MtaStageData/set", {"accountId": "0", "update": {"singleton": {
            "addReceivedHeader": {"match": {"0": {"if": "local_port == 25 || local_port == 2525", "then": "true"}}, "else": "false"},
            "enableSpamFilter": {"match": {}, "else": "is_empty(authenticated_as)"},
            "dkimSignDomain": {
                "match": {"0": {"if": "is_local_domain(sender_domain) && !is_empty(authenticated_as)", "then": "sender_domain"}},
                "else": "false",
            },
        }}}, "data"],
        ["x:SpamSettings/set", {"accountId": "0", "update": {"singleton": {
            "enable": True,
            "trustContacts": True,
            "trustReplies": True,
            "scoreSpam": 5.0,
            "scoreDiscard": 12.0,
            "scoreReject": 16.0,
        }}}, "spam"],
    ]
    res = jmap_client.jmap_call(calls, eng_user, eng_pass)
    print("[Bootstrap] Stage/Spam config:", len(res), "calls applied", flush=True)

    # Ensure primary domain exists
    domains = jmap_client.list_domains()
    dom = next((d for d in domains if d.get("name") == args.domain), None)
    if not dom:
        dom = jmap_client.create_domain(args.domain)
        domains = jmap_client.list_domains()
        dom = next((d for d in domains if d.get("name") == args.domain), dom)
        print(f"[Bootstrap] Created primary domain {args.domain}", flush=True)
    else:
        print(f"[Bootstrap] Domain {args.domain} already present", flush=True)

    # Ensure requested admin mailbox exists with user's chosen password
    admin_email = args.admin_email.strip().lower()
    local_part = admin_email.split("@", 1)[0]
    accounts = jmap_client.list_accounts()
    exists = next((a for a in accounts if (a.get("emailAddress") or "").lower() == admin_email), None)
    if not exists:
        jmap_client.create_account(local_part, dom["id"], args.admin_pass, "Vmail Administrator", "Admin", 0)
        print(f"[Bootstrap] Created admin mailbox {admin_email}", flush=True)
    else:
        jmap_client.update_account(exists["id"], {
            "roles": {"@type": "Admin"},
            "credentials": {"0": {"@type": "Password", "secret": args.admin_pass}},
        })
        print(f"[Bootstrap] Updated admin mailbox credentials for {admin_email}", flush=True)

    # Optional Outbound SMTP Relay
    if args.relay_host:
        jmap_client.upsert_relay(
            route_name="default-relay",
            host=args.relay_host,
            port=args.relay_port,
            implicit_tls=args.relay_tls,
            username=args.relay_user,
            secret=args.relay_secret,
            description="Primary Outbound Relay",
        )
        jmap_client.set_default_outbound_route("default-relay")
        print(f"[Bootstrap] Configured outbound SMTP relay {args.relay_host}:{args.relay_port}", flush=True)

    # Initialize SQLite & save encrypted sender credentials
    db.init_db()
    db.save_sender_cred(admin_email, args.admin_pass)
    jmap_client.reload_settings()

    # Verify admin mailbox works for JMAP control API before switching /etc/vmail/engine.json
    try:
        jmap_client.get_session(admin_email, args.admin_pass)
        jmap_client.save_engine_creds(admin_email, args.admin_pass)
    except Exception:
        jmap_client.save_engine_creds(eng_user, eng_pass)

    print("[Bootstrap] Vmail Core bootstrap completed successfully.", flush=True)


if __name__ == "__main__":
    main()
