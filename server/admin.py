#!/usr/bin/env python3
"""Tenant admin CLI launcher.  python server/admin.py create-project "Team A"  (etc.)"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from dprov_server.admin import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
