"""Minimal GDB Remote Serial Protocol client used by the damage meter.

Only the handful of packets Eden's GDB stub needs are implemented:

    qOffsets, qXfer:libraries:read, ?, g, p, m, M, Z0, z0, c, and Ctrl-C.

The client is deliberately dependency-free so the meter runs on a stock
Python installation.
"""
from __future__ import annotations

import re
import socket
import time


class RspError(RuntimeError):
    pass


class RspClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 6543, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b""

    # ------------------------------------------------------------------ socket
    def connect(self) -> "RspClient":
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self._buf = b""
        return self

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self) -> "RspClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # --------------------------------------------------------------- low level
    def _send(self, payload: bytes | str) -> None:
        if self.sock is None:
            raise RspError("not connected")
        if isinstance(payload, str):
            payload = payload.encode()
        checksum = sum(payload) & 0xFF
        packet = b"$" + payload + b"#" + f"{checksum:02x}".encode()
        self.sock.sendall(packet)

    def _recv_byte(self) -> bytes:
        if self.sock is None:
            raise RspError("not connected")
        chunk = self.sock.recv(1)
        if not chunk:
            raise RspError("connection closed")
        return chunk

    def _recv_packet(self) -> bytes:
        """Read one RSP packet, ignoring acknowledgements and O packets."""
        while True:
            b = self._recv_byte()
            if b == b"$":
                break
            # '+' / '-' / stray bytes are ignored
        data = bytearray()
        while True:
            b = self._recv_byte()
            if b == b"#":
                break
            data += b
        csum = self._recv_byte() + self._recv_byte()
        try:
            expected = int(csum, 16)
        except ValueError:
            expected = None
        if expected is not None and (sum(data) & 0xFF) != expected:
            raise RspError("bad packet checksum")
        payload = bytes(data)
        if payload.startswith(b"O"):
            # console output; skip and read the next packet
            return self._recv_packet()
        return payload

    def cmd(self, payload: bytes | str, timeout: float | None = None) -> bytes:
        if self.sock is None:
            raise RspError("not connected")
        old = self.sock.gettimeout()
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            self._send(payload)
            # consume the ack (if any)
            try:
                ack = self.sock.recv(1)
                if ack == b"-":
                    self._send(payload)
            except socket.timeout:
                pass
            return self._recv_packet()
        finally:
            if timeout is not None:
                self.sock.settimeout(old)

    # ------------------------------------------------------------------ memory
    def read(self, addr: int, size: int) -> bytes:
        out = bytearray()
        while size > 0:
            n = min(size, 0x800)
            r = self.cmd(f"m{addr:x},{n:x}")
            if not r or r.startswith(b"E"):
                raise RspError(f"memory read failed at {addr:#x}: {r!r}")
            out += bytes.fromhex(r.decode())
            addr += n
            size -= n
        return bytes(out)

    def read_u32(self, addr: int) -> int:
        return int.from_bytes(self.read(addr, 4), "little")

    def read_u64(self, addr: int) -> int:
        return int.from_bytes(self.read(addr, 8), "little")

    def write(self, addr: int, data: bytes) -> None:
        r = self.cmd(f"M{addr:x},{len(data):x}:{data.hex()}")
        if r != b"OK":
            raise RspError(f"memory write failed at {addr:#x}: {r!r}")

    def write_u32(self, addr: int, value: int) -> None:
        self.write(addr, (value & 0xFFFFFFFF).to_bytes(4, "little"))

    # --------------------------------------------------------------- registers
    def registers(self) -> list[int]:
        """Return r0..r15 from the ARM32 ``g`` packet."""
        r = self.cmd("g")
        # 16 general purpose registers, 32 bit each, little endian, 8 hex
        # characters per register.  Eden's A32 target XML places the FPA
        # registers after r15, so the first 128 hex characters are the GPRs.
        if len(r) < 16 * 8:
            raise RspError(f"short register packet ({len(r)} hex chars)")
        return [int(r[i * 8:(i + 1) * 8], 16) for i in range(16)]

    def register(self, index: int) -> int:
        r = self.cmd(f"p{index:x}")
        if not r or r.startswith(b"E"):
            raise RspError(f"register read failed: {r!r}")
        return int(r, 16)

    # ------------------------------------------------------------- breakpoints
    def set_breakpoint(self, addr: int, kind: int = 0, size: int = 4) -> None:
        r = self.cmd(f"Z{kind},{addr:x},{size:x}")
        if r != b"OK":
            raise RspError(f"breakpoint insert failed at {addr:#x}: {r!r}")

    def clear_breakpoint(self, addr: int, kind: int = 0, size: int = 4) -> None:
        try:
            self.cmd(f"z{kind},{addr:x},{size:x}", timeout=2.0)
        except Exception:
            pass

    # ---------------------------------------------------------------- execution
    def stop_reason(self) -> bytes:
        return self.cmd("?", timeout=2.0)

    def continue_(self) -> None:
        self._send("c")

    def interrupt(self) -> bytes:
        if self.sock is None:
            raise RspError("not connected")
        self.sock.sendall(b"\x03")
        return self._recv_packet()

    def wait_stop(self, timeout: float = 1.0) -> bytes | None:
        """Wait for a T/S/W/X stop packet.  Returns None on timeout."""
        if self.sock is None:
            raise RspError("not connected")
        old = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            while True:
                p = self._recv_packet()
                if p[:1] in (b"T", b"S", b"W", b"X"):
                    return p
        except socket.timeout:
            return None
        finally:
            self.sock.settimeout(old)

    # ------------------------------------------------------------------- info
    def main_base(self) -> int:
        r = self.cmd("qOffsets").decode(errors="replace")
        m = re.search(r"TextSeg=([0-9a-fA-F]+)", r)
        if m:
            return int(m.group(1), 16)
        # fall back to the first module segment
        libs = self.cmd("qXfer:libraries:read::").decode(errors="replace")
        m = re.search(r'address="(0x[0-9a-fA-F]+)"', libs)
        if m:
            return int(m.group(1), 16)
        raise RspError("could not determine main module base")

    def modules(self) -> list[tuple[int, str]]:
        libs = self.cmd("qXfer:libraries:read::").decode(errors="replace")
        return [(int(a, 16), n) for a, n in re.findall(r'<library name="([^"]*)"><segment address="(0x[0-9a-fA-F]+)"', libs)]


def wait_for_port(host: str, port: int, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False
