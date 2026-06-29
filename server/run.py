#!/usr/bin/env python3
"""Launch the DProvenanceKit hosted backend.

    python server/run.py            # http://127.0.0.1:8787  (dashboard at /)
    PORT=9000 DPROV_API_KEYS="k1:teamA,k2:teamB" python server/run.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))  # make dprov_server importable
from dprov_server.http_app import main  # noqa: E402

if __name__ == "__main__":
    main()
