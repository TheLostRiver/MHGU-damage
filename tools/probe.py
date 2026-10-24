#!/usr/bin/env python3
"""Raw damage-event probe.

Run this while the game is running, hit something, and it prints the registers
at the damage-number store, the ring index, and the four words at the ring
entry.  Useful if the meter shows no damage or the wrong value.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import run_probe  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6543)
    ap.add_argument("--bp", type=lambda s: int(s, 0), default=0xC02074)
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    run_probe(args.host, args.port, args.bp, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
