#!/usr/bin/env python3
"""Checkout wrapper for the e2e decision-path demo.

    python scripts/e2e_demo.py
    python scripts/e2e_demo.py --output-dir /tmp/dpk-e2e --keep
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from examples.e2e_decision_path.run_demo import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
