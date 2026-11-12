#!/usr/bin/env python3
"""
Vmail Core JMAP Bootstrapper
Configures listeners (SMTP 2525, SMTPS 4650, IMAPS 9930, ManageSieve 4190, HTTP/JMAP 8090),
Vmail ESMTP greeting, DKIM automatic signing, Anti-Spam rules, initial domain & admin account,
and optional outbound SMTP relay cleanly via local JMAP API.
"""
import argparse
import os
import sys

sys.path.insert(0, os.environ.get("VMAIL_APP_DIR", "/opt/vmail/app"))
import db
import jmap_client


def main():
    p = argparse.ArgumentParser(description="Bootstrap Vmail Core Engine settings via JMAP")
    p.add_argument("--domain", required=True)
    p.add_argument("--mail-host", required=True)
    p.add_argument("--admin-user", default="admin")
    p.add_argument("--admin-pass", required=True)
    p.add_argument("--admin-email", required=True)
    p.add_argument("--relay-host", default="")
    p.add_argument("--relay-port", type=int, default=587)
    p.add_argument("--relay-user", default="")
    p.add_argument("--relay-secret", default="")
    p.add_argument("--relay-tls", action="store_true")
    args = p.parse_args()

    jmap_client.save_engine_creds(args.admin_user, args.admin_pass)

    # 1. System settings & Vmail ESMTP greeting & bridge Received header rule
    calls = [
        ["x:SystemSettings/set", {"accountId": "0", "update": {"singleton": {
            "defaultHostname": args.mail_host,
        }}}, "sys"],
        ["x:MtaStageConnect/set", {"accountId": "0", "update": {"singleton": {
            "smtpGreeting": {"match": {}, "else": "system('hostname') + ' Vmail ESMTP ready'"},
        }}}, "conn"],
        ["x:MtaStageData/set", {"accountId": "0", "update": {"singleton": {
            "addReceivedHeader": {"match": {"0": {"if": "local_port == 25", "then": "true"}}, "else": "false"},
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
    res = jmap_client.jmap_call(calls, args.admin_user, args.admin_pass)
    print("[Bootstrap] Stage/Spam config:", len(res), "calls applied")

    # 2. Ensure Domain exists
    domains = jmap_client.list_domains()
    dom = next((d for d in domains if d.get("name") == args.domain), None)
    if not dom:
        dom = jmap_client.create_domain(args.domain)
        domains = jmap_client.list_domains()
        dom = next((d for d in domains if d.get("name") == args.domain), dom)
        print(f"[Bootstrap] Created primary domain {args.domain}")
    else:
        print(f"[Bootstrap] Domain {args.domain} already present")

    # 3. Ensure primary mailbox account exists
    local_part = args.admin_email.split("@", 1)[0].strip().lower()
    accounts = jmap_client.list_accounts()
    exists = next((a for a in accounts if (a.get("emailAddress") or "").lower() == args.admin_email.lower()), None)
    if not exists:
        jmap_client.create_account(local_part, dom["id"], args.admin_pass, "Vmail Administrator", "Admin", 0)
        print(f"[Bootstrap] Created admin mailbox {args.admin_email}")
    else:
        print(f"[Bootstrap] Mailbox {args.admin_email} already present")

    # 4. Optional SMTP Relay
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
        print(f"[Bootstrap] Configured outbound SMTP relay {args.relay_host}:{args.relay_port}")

    # 5. Initialize SQLite & store encrypted sender credentials
    db.init_db()
    db.save_sender_cred(args.admin_email, args.admin_pass)
    jmap_client.reload_settings()
    print("[Bootstrap] Vmail Core bootstrap completed successfully.")


if __name__ == "__main__":
    main()
