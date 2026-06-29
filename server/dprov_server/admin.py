"""Tenant administration CLI.

    python -m dprov_server.admin create-project "Team A"
    python -m dprov_server.admin create-key --project proj_xxxx --name ci
    python -m dprov_server.admin list-projects
    python -m dprov_server.admin list-keys --project proj_xxxx
    python -m dprov_server.admin revoke dpk_xxxx

Operates on the same tenancy database the server uses (``$DPROV_TENANTS_DB`` or
``$DPROV_DATA_DIR/tenants.sqlite``).
"""

from __future__ import annotations

import argparse
import sys

from .tenancy import Tenancy


def main(argv=None) -> int:
    t = Tenancy.default()
    ap = argparse.ArgumentParser(prog="dprov_server.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-projects")
    p = sub.add_parser("create-project")
    p.add_argument("name")
    p = sub.add_parser("create-key")
    p.add_argument("--project", required=True)
    p.add_argument("--name", default=None)
    p = sub.add_parser("list-keys")
    p.add_argument("--project", required=True)
    p = sub.add_parser("revoke")
    p.add_argument("key")
    args = ap.parse_args(argv)

    if args.cmd == "create-project":
        print(t.create_project(args.name))
    elif args.cmd == "create-key":
        try:
            print(t.create_api_key(args.project, args.name))
        except KeyError as e:
            print(e, file=sys.stderr)
            return 1
        print("  ^ save this now — only its hash is stored.", file=sys.stderr)
    elif args.cmd == "list-projects":
        for r in t.list_projects():
            print(f"{r['id']}\t{r['name']}")
    elif args.cmd == "list-keys":
        for r in t.list_keys(args.project):
            print(f"{r['key_hash'][:16]}…\t{r['name'] or '-'}\t{'revoked' if r['revoked'] else 'active'}")
    elif args.cmd == "revoke":
        print("revoked" if t.revoke(args.key) else "no such key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
