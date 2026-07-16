#!/usr/bin/env python3
"""Checkout wrapper for the installed DProvenanceKit end-to-end demo.

Run from the repository root without installing first:

    python examples/end_to_end_demo.py

Artifacts are written to the caller's current working directory by default. Set
``DPROV_DEMO_OUT`` or use the installed ``dprovenancekit demo --output-dir`` command
to choose another directory.
"""

from __future__ import annotations

import os
import sys

# Make the repository root importable when this wrapper is run from a checkout.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dprovenancekit.demo import main  # noqa: E402


if __name__ == "__main__":
    main()
