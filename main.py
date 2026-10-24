#!/usr/bin/env python3
"""MHGU player damage meter.

Usage:
    python main.py                  # overlay, waits for Eden's GDB stub
    python main.py --demo           # synthetic data, for UI development
    python main.py --no-gui         # console output
    python main.py --probe          # log a few raw damage events
"""
from __future__ import annotations

import argparse
import sys
import time

from mhgu_damage.gdb import RspClient
from mhgu_damage.meter import DEFAULT_BREAKPOINT, DamageMeter, DemoMeter
from mhgu_damage.ui import run_ui


def run_console(meter: DamageMeter) -> None:
    meter.start()
    try:
        while True:
            time.sleep(1.0)
            st = meter.snapshot()
            print(
                f"total={st.total:>8,}  hits={st.hits:>5,}  "
                f"dps1s={st.dps_1s:>8.1f}  session={st.dps_session:>8.1f}  "
                f"status={st.status}",
                flush=True,
            )
    except KeyboardInterrupt:
        pass
    finally:
        meter.stop()


def run_probe(host: str, port: int, bp_offset: int, limit: int = 20) -> None:
    rsp = RspClient(host, port).connect()
    base = rsp.main_base()
    bp = base + bp_offset
    print(f"main={base:#x} breakpoint={bp:#x}")

    reason = rsp.stop_reason()
    if reason[:1] not in (b"T", b"S", b"W", b"X"):
        rsp.interrupt()
    rsp.set_breakpoint(bp)
    rsp.continue_()
    print("capturing; play the game and hit something...")

    seen = 0
    try:
        while seen < limit:
            stop = rsp.wait_stop(timeout=60.0)
            if stop is None:
                continue
            regs = rsp.registers()
            r = [regs[i] for i in range(9)]
            print(
                f"stop pc={regs[15]:#010x} r0={r[0]:#010x} r4={r[4]:#010x} "
                f"r7={r[7]:#010x} r8={r[8]:#x}"
            )
            try:
                entry = rsp.read(regs[4] + 0xD74, 0x10)
                print(f"  entry+0xd74: {entry.hex(' ')}")
            except Exception as exc:
                print(f"  entry read failed: {exc}")
            seen += 1
            rsp.continue_()
    finally:
        try:
            rsp.clear_breakpoint(bp)
        except Exception:
            pass
        rsp.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6543, help="Eden GDB stub port (default 6543)")
    ap.add_argument("--demo", action="store_true", help="use synthetic data (no emulator)")
    ap.add_argument("--no-gui", action="store_true", help="print stats instead of an overlay")
    ap.add_argument("--probe", action="store_true", help="log raw hits and exit")
    ap.add_argument(
        "--bp", type=lambda s: int(s, 0), default=DEFAULT_BREAKPOINT,
        help="main-relative breakpoint offset (default 0x%X)" % DEFAULT_BREAKPOINT,
    )
    args = ap.parse_args(argv)

    if args.probe:
        run_probe(args.host, args.port, args.bp)
        return 0

    meter = DemoMeter() if args.demo else DamageMeter(args.host, args.port, args.bp)
    if args.no_gui:
        run_console(meter)
        return 0

    meter.start()
    try:
        run_ui(meter, "MHGU Damage Meter" + (" · demo" if args.demo else ""))
    finally:
        meter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
