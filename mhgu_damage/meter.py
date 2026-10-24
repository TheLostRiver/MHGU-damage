"""Damage capture and statistics for MHGU 1.4.0 (Eden).

The meter attaches to Eden's GDB stub, puts a software breakpoint on the
instruction that stores a damage number into the game's damage-number ring
buffer, and reads the damage value out of r8 on every hit.

Retail MHGU 1.4.0 (main build id FB08F1D20FD1204FE7070672777F869B):

    0xC02074    str r8, [r4, #0xd74]     ; r8 = damage, r4 = ring entry
    0xC02030    ldr r7, [r1, r0]!        ; r7 = ring index

The offset is relative to the main module base, so it survives ASLR.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from .gdb import RspClient, RspError

#: main-relative address of ``str r8, [r4, #0xd74]`` in MHGU 1.4.0
DEFAULT_BREAKPOINT = 0xC02074

#: sanity bounds for a damage number
MIN_DAMAGE = 1
MAX_DAMAGE = 1_000_000


@dataclass(slots=True)
class Hit:
    time: float
    damage: int
    index: int
    entry: int


@dataclass(slots=True)
class Stats:
    total: int = 0
    hits: int = 0
    elapsed: float = 0.0
    dps_session: float = 0.0
    dps_1s: float = 0.0
    dps_5s: float = 0.0
    last_hit: int = 0
    peak_dps: float = 0.0
    status: str = "starting"
    connected: bool = False


class DamageMeter:
    """Background capture thread + lock-protected event history."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6543,
        breakpoint_offset: int = DEFAULT_BREAKPOINT,
        connect_timeout: float = 60.0,
    ):
        self.host = host
        self.port = port
        self.breakpoint_offset = breakpoint_offset
        self.connect_timeout = connect_timeout

        self._lock = threading.Lock()
        self._events: deque[Hit] = deque(maxlen=20000)
        self._start = time.monotonic()
        self._status = "starting"
        self._connected = False
        self._last_hit = 0
        self._last_index: int | None = None
        self._peak_dps = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._base: int | None = None
        self._hits_seen = 0

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="damage-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._start = time.monotonic()
            self._last_hit = 0
            self._peak_dps = 0.0
            self._hits_seen = 0
            self._last_index = None

    def add_hit(self, damage: int, index: int = 0, entry: int = 0, when: float | None = None) -> None:
        if not (MIN_DAMAGE <= damage <= MAX_DAMAGE):
            return
        t = time.monotonic() if when is None else when
        with self._lock:
            self._events.append(Hit(t, damage, index, entry))
            self._last_hit = damage
            self._hits_seen += 1

    def snapshot(self) -> Stats:
        now = time.monotonic()
        with self._lock:
            events = list(self._events)
            start = self._start
            last_hit = self._last_hit
            status = self._status
            connected = self._connected
            base = self._base

        total = sum(h.damage for h in events)
        elapsed = max(0.0, now - start)
        dps_session = total / elapsed if elapsed > 0 else 0.0

        def window(seconds: float) -> float:
            cutoff = now - seconds
            return sum(h.damage for h in events if h.time >= cutoff) / seconds

        dps_1s = window(1.0)
        dps_5s = window(5.0)
        peak = self._peak_dps
        if dps_1s > peak:
            peak = dps_1s
            self._peak_dps = peak

        return Stats(
            total=total,
            hits=len(events),
            elapsed=elapsed,
            dps_session=dps_session,
            dps_1s=dps_1s,
            dps_5s=dps_5s,
            last_hit=last_hit,
            peak_dps=peak,
            status=status,
            connected=connected,
        )

    def buckets(self, seconds: int = 60) -> list[float]:
        """Damage per one-second bucket, oldest first."""
        now = time.monotonic()
        out = [0.0] * seconds
        with self._lock:
            events = list(self._events)
        for h in events:
            age = now - h.time
            if 0.0 <= age < seconds:
                idx = seconds - 1 - int(age)
                if 0 <= idx < seconds:
                    out[idx] += h.damage
        return out

    def recent_hits(self, limit: int = 8) -> list[Hit]:
        with self._lock:
            return list(self._events)[-limit:]

    # --------------------------------------------------------------- internals
    def _set_status(self, text: str, connected: bool | None = None) -> None:
        with self._lock:
            self._status = text
            if connected is not None:
                self._connected = connected

    def _run(self) -> None:
        deadline = time.monotonic() + self.connect_timeout
        while self._running:
            try:
                self._set_status("waiting for Eden GDB stub...", False)
                rsp = RspClient(self.host, self.port, timeout=10.0)
                while self._running:
                    try:
                        rsp.connect()
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            self._set_status("GDB stub not found (enable Debug > GDB stub)", False)
                        time.sleep(1.0)
                if not self._running:
                    rsp.close()
                    return
                self._run_session(rsp)
            except Exception as exc:  # keep the meter alive across game restarts
                self._set_status(f"disconnected: {exc}", False)
                time.sleep(2.0)

    def _run_session(self, rsp: RspClient) -> None:
        try:
            base = rsp.main_base()
            self._base = base
            bp = base + self.breakpoint_offset
            self._set_status(f"connected, main={base:#x}", True)

            # Make sure the guest is stopped before inserting the breakpoint.
            reason = b""
            try:
                reason = rsp.stop_reason()
            except RspError:
                pass
            if reason[:1] not in (b"T", b"S", b"W", b"X"):
                try:
                    rsp.interrupt()
                except Exception:
                    pass

            rsp.set_breakpoint(bp)
            self._set_status("capturing", True)
            self._last_index = None
            rsp.continue_()

            while self._running:
                stop = rsp.wait_stop(timeout=0.5)
                if stop is None:
                    continue
                try:
                    regs = rsp.registers()
                    damage = regs[8] & 0xFFFFFFFF
                    index = regs[7] & 0xFFFFFFFF
                    entry = regs[4] & 0xFFFFFFFF
                    if MIN_DAMAGE <= damage <= MAX_DAMAGE and index != self._last_index:
                        self.add_hit(damage, index, entry)
                        self._last_index = index
                finally:
                    if self._running:
                        rsp.continue_()
        finally:
            try:
                rsp.clear_breakpoint(self._base + self.breakpoint_offset if self._base else 0)
            except Exception:
                pass
            rsp.close()
            self._set_status("disconnected", False)


class DemoMeter(DamageMeter):
    """Synthetic data source used for UI development and screenshots."""

    def __init__(self):
        super().__init__()
        self._demo_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._demo_thread is not None:
            return
        self._running = True
        self._demo_thread = threading.Thread(target=self._demo_loop, daemon=True)
        self._demo_thread.start()

    def _demo_loop(self) -> None:
        import math
        import random

        self._set_status("demo mode", True)
        t0 = time.monotonic()
        while self._running:
            t = time.monotonic() - t0
            # bursts of pierce-like hits, with quieter gaps
            burst = max(0.0, math.sin(t * 0.55)) ** 2
            time.sleep(random.uniform(0.05, 0.18) / (0.4 + burst))
            if random.random() < 0.15:
                time.sleep(random.uniform(0.4, 1.1))
            damage = int(random.gauss(28 + 90 * burst, 9 + 20 * burst))
            self.add_hit(max(1, damage))
