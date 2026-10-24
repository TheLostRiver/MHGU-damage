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

import re
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

#: hits separated by less than this are treated as one volley
VOLLEY_GAP = 0.8


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
    volley_hits: int = 0
    volley_damage: int = 0
    volley_count: int = 0
    status: str = "starting"
    connected: bool = False


class DamageMeter:
    """Background capture thread + lock-protected event history."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 12345,
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
        self._volley_hits = 0
        self._volley_damage = 0
        self._last_volley_hits = 0
        self._last_volley_damage = 0
        self._volley_count = 0
        self._last_hit_time = 0.0

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
            self._volley_hits = 0
            self._volley_damage = 0
            self._last_volley_hits = 0
            self._last_volley_damage = 0
            self._volley_count = 0
            self._last_hit_time = 0.0

    def add_hit(self, damage: int, index: int = 0, entry: int = 0, when: float | None = None) -> None:
        if not (MIN_DAMAGE <= damage <= MAX_DAMAGE):
            return
        t = time.monotonic() if when is None else when
        with self._lock:
            # A pierce volley is a burst of hits; a gap longer than VOLLEY_GAP
            # starts a new volley.  This is the metric that matters for the
            # high-FPS pierce test: hits per shot, not just total damage.
            if self._volley_hits and (t - self._last_hit_time) > VOLLEY_GAP:
                self._last_volley_hits = self._volley_hits
                self._last_volley_damage = self._volley_damage
                self._volley_count += 1
                self._volley_hits = 0
                self._volley_damage = 0
            self._events.append(Hit(t, damage, index, entry))
            self._last_hit = damage
            self._hits_seen += 1
            self._volley_hits += 1
            self._volley_damage += damage
            self._last_hit_time = t

    def snapshot(self) -> Stats:
        now = time.monotonic()
        with self._lock:
            events = list(self._events)
            start = self._start
            last_hit = self._last_hit
            status = self._status
            connected = self._connected
            base = self._base
            vh = self._volley_hits
            vd = self._volley_damage
            lvh = self._last_volley_hits
            lvd = self._last_volley_damage
            vc = self._volley_count
            last_hit_time = self._last_hit_time

        if vh and (now - last_hit_time) <= VOLLEY_GAP:
            volley_hits, volley_damage, volley_count = vh, vd, vc + 1
        else:
            volley_hits, volley_damage, volley_count = lvh, lvd, vc

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
            volley_hits=volley_hits,
            volley_damage=volley_damage,
            volley_count=volley_count,
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
            # Give Eden's debugger a moment to settle after the TCP connect.
            # This mirrors a normal GDB attach: ask for the current stop, set
            # the breakpoint while stopped, then continue.
            time.sleep(1.0)
            base = rsp.main_base()
            self._base = base
            bp = base + self.breakpoint_offset
            self._set_status(f"connected, main={base:#x}", True)

            reason = b""
            try:
                reason = rsp.stop_reason()
            except Exception:
                reason = b""
            if reason[:1] not in (b"T", b"S", b"W", b"X"):
                self._set_status("stopping guest...", True)
                try:
                    rsp.interrupt()
                except Exception:
                    pass

            self._set_status("setting breakpoint...", True)
            rsp.set_breakpoint(bp)
            self._set_status("capturing", True)
            self._last_index = None
            rsp.continue_()

            while self._running:
                stop = rsp.wait_stop(timeout=0.5)
                if stop is None:
                    continue
                try:
                    m = re.search(rb"0f:([0-9a-fA-F]{8})", stop)
                    pc = int.from_bytes(bytes.fromhex(m.group(1).decode()), "little") if m else 0
                    # Only consume stops that are our damage-number store.
                    if pc not in (bp, bp + 4):
                        continue
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
