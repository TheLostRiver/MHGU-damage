"""Tkinter overlay for the MHGU damage meter.

The window is intentionally dependency-free: Tkinter ships with CPython on
Windows.  The chart is drawn on a Canvas so there is no matplotlib/pyqtgraph
requirement.
"""
from __future__ import annotations

import math
import time
import tkinter as tk
from tkinter import font as tkfont

from .meter import DamageMeter, Stats

BG = "#0d0f13"
PANEL = "#151922"
PANEL_2 = "#1b202b"
TEXT = "#e8eaf0"
MUTED = "#7b8496"
GRID = "#242a36"
ACCENT = "#ffb454"
ACCENT_DIM = "#8a5f28"
CYAN = "#5ad1ff"
GREEN = "#57d98a"
RED = "#ff6b6b"

CHART_SECONDS = 60
UPDATE_MS = 100


def fmt_int(n: float) -> str:
    return f"{int(n):,}"


def fmt_dps(n: float) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return f"{n:.1f}"


class MeterWindow:
    def __init__(self, meter: DamageMeter, title: str = "MHGU Damage Meter"):
        self.meter = meter
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg=GRID)
        self.root.geometry("560x372")
        self.root.minsize(520, 340)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.96)
        except tk.TclError:
            pass

        self._drag_x = 0
        self._drag_y = 0

        self._build()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        root = self.root
        shell = tk.Frame(root, bg=BG)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        outer = tk.Frame(shell, bg=BG)
        outer.pack(fill="both", expand=True, padx=13, pady=11)

        # header -----------------------------------------------------------
        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        self._header = header

        tk.Label(
            header, text="MHGU", bg=BG, fg=ACCENT,
            font=("Segoe UI Semibold", 10),
        ).pack(side="left")
        tk.Label(
            header, text=" DAMAGE METER", bg=BG, fg=TEXT,
            font=("Segoe UI Semibold", 10),
        ).pack(side="left")

        close = tk.Button(
            header, text="✕", command=self.close,
            bg=BG, fg=MUTED, activebackground=PANEL_2, activeforeground=TEXT,
            relief="flat", bd=0, padx=7, pady=0, cursor="hand2",
            font=("Segoe UI", 10),
        )
        close.pack(side="right")

        reset = tk.Button(
            header, text="Reset", command=self._reset,
            bg=PANEL_2, fg=TEXT, activebackground=PANEL_2, activeforeground=TEXT,
            relief="flat", bd=0, padx=10, pady=2, cursor="hand2",
            font=("Segoe UI", 8),
        )
        reset.pack(side="right", padx=(0, 8))

        self.status_dot = tk.Canvas(header, width=10, height=10, bg=BG, highlightthickness=0)
        self.status_dot.pack(side="right", padx=(8, 4))
        self._dot = self.status_dot.create_oval(2, 2, 9, 9, fill=MUTED, outline="")
        self.status_label = tk.Label(
            header, text="starting", bg=BG, fg=MUTED, font=("Segoe UI", 8),
        )
        self.status_label.pack(side="right")

        # stats ------------------------------------------------------------
        stats = tk.Frame(outer, bg=PANEL)
        stats.pack(fill="x", pady=(10, 8))
        stats.grid_columnconfigure(0, weight=2)
        stats.grid_columnconfigure(1, weight=1)
        stats.grid_columnconfigure(2, weight=1)
        stats.grid_columnconfigure(3, weight=1)

        self.total_var = tk.StringVar(value="0")
        self.hits_var = tk.StringVar(value="0")
        self.dps_var = tk.StringVar(value="0.0")
        self.time_var = tk.StringVar(value="00:00")

        self._stat(stats, 0, "TOTAL DAMAGE", self.total_var, size=24, color=ACCENT)
        self._stat(stats, 1, "HITS", self.hits_var, size=15)
        self._stat(stats, 2, "DPS · 1s", self.dps_var, size=15)
        self._stat(stats, 3, "ELAPSED", self.time_var, size=15)

        # chart ------------------------------------------------------------
        chart_frame = tk.Frame(outer, bg=PANEL)
        chart_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            chart_frame, bg=PANEL, highlightthickness=0, height=150,
        )
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(8, 2))

        # footer -----------------------------------------------------------
        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", pady=(6, 0))
        self.last_var = tk.StringVar(value="last  —")
        self.volley_var = tk.StringVar(value="volley  —")
        self.peak_var = tk.StringVar(value="peak  —")
        self.session_var = tk.StringVar(value="session  —")
        tk.Label(
            footer, textvariable=self.last_var, bg=BG, fg=MUTED,
            font=("Consolas", 8),
        ).pack(side="left")
        tk.Label(
            footer, textvariable=self.volley_var, bg=BG, fg=ACCENT,
            font=("Consolas", 8, "bold"),
        ).pack(side="left", padx=(14, 0))
        tk.Label(
            footer, textvariable=self.session_var, bg=BG, fg=MUTED,
            font=("Consolas", 8),
        ).pack(side="right")
        tk.Label(
            footer, textvariable=self.peak_var, bg=BG, fg=MUTED,
            font=("Consolas", 8),
        ).pack(side="right", padx=(0, 18))

        # drag window by the header / empty areas
        for widget in (header, self.status_label):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _e: self.close())

    def _stat(self, parent, col: int, label: str, var: tk.StringVar, size: int, color: str = TEXT):
        frame = tk.Frame(parent, bg=PANEL)
        frame.grid(row=0, column=col, sticky="nsew", padx=12, pady=9)
        tk.Label(
            frame, text=label, bg=PANEL, fg=MUTED,
            font=("Segoe UI Semibold", 7),
        ).pack(anchor="w")
        tk.Label(
            frame, textvariable=var, bg=PANEL, fg=color,
            font=("Consolas", size, "bold"),
        ).pack(anchor="w")

    # ------------------------------------------------------------------ events
    def _drag_start(self, event):
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_move(self, event):
        self.root.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _reset(self):
        self.meter.reset()

    def close(self):
        self.meter.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------- chart
    def _draw_chart(self):
        c = self.canvas
        c.delete("all")
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        left, right, top, bottom = 38, 8, 8, 18
        pw = w - left - right
        ph = h - top - bottom

        buckets = self.meter.buckets(CHART_SECONDS)
        # 5 second moving average for the secondary line
        smooth = []
        for i in range(len(buckets)):
            lo = max(0, i - 4)
            chunk = buckets[lo:i + 1]
            smooth.append(sum(chunk) / len(chunk))

        ymax = max(10.0, max(buckets) * 1.18, max(smooth) * 1.1)
        ymax = _nice_max(ymax)

        # grid + y labels
        for i in range(5):
            y = top + ph * i / 4
            c.create_line(left, y, w - right, y, fill=GRID)
            val = ymax * (4 - i) / 4
            c.create_text(
                left - 6, y, text=_short_num(val), anchor="e",
                fill=MUTED, font=("Consolas", 7),
            )
        # x labels (seconds ago)
        for sec in (60, 45, 30, 15, 0):
            x = left + pw * (1 - sec / CHART_SECONDS)
            c.create_text(
                x, h - 8, text=("now" if sec == 0 else f"-{sec}s"),
                fill=MUTED, font=("Consolas", 7),
            )

        if not any(buckets):
            c.create_text(
                left + pw / 2, top + ph / 2,
                text="waiting for damage...", fill=MUTED,
                font=("Segoe UI", 9),
            )
            return

        def points(values):
            pts = []
            for i, v in enumerate(values):
                x = left + pw * i / (len(values) - 1)
                y = top + ph * (1 - min(v / ymax, 1.0))
                pts.extend((x, y))
            return pts

        # filled area under the DPS curve
        pts = points(buckets)
        poly = [left, top + ph] + pts + [left + pw, top + ph]
        c.create_polygon(poly, fill=ACCENT_DIM, outline="", stipple="gray25")

        # 5s average (behind the main line)
        c.create_line(*points(smooth), fill=CYAN, width=1, smooth=True)

        # 1s DPS curve
        c.create_line(*pts, fill=ACCENT, width=2, smooth=True)

        # hit ticks at the bottom
        now = time.monotonic()
        for hit in self.meter.recent_hits(400):
            age = now - hit.time
            if age < 0 or age >= CHART_SECONDS:
                continue
            x = left + pw * (1 - age / CHART_SECONDS)
            length = 3 + min(9, math.log2(max(2, hit.damage)) * 1.5)
            c.create_line(x, top + ph, x, top + ph - length, fill="#c98a3c")

    # ------------------------------------------------------------------ update
    def _update(self):
        st: Stats = self.meter.snapshot()
        self.total_var.set(fmt_int(st.total))
        self.hits_var.set(fmt_int(st.hits))
        self.dps_var.set(fmt_dps(st.dps_1s))
        minutes, seconds = divmod(int(st.elapsed), 60)
        self.time_var.set(f"{minutes:02d}:{seconds:02d}")

        self.last_var.set(f"last  {st.last_hit:>6,}")
        self.volley_var.set(f"volley  {st.volley_hits} hit · {st.volley_damage:,}")
        self.peak_var.set(f"peak  {fmt_dps(st.peak_dps):>6}/s")
        self.session_var.set(f"session  {fmt_dps(st.dps_session):>6}/s")

        color = GREEN if st.connected else (RED if "not found" in st.status or "disconnected" in st.status else MUTED)
        self.status_dot.itemconfigure(self._dot, fill=color)
        # keep the status label short
        text = st.status
        if len(text) > 34:
            text = text[:31] + "..."
        self.status_label.configure(text=text)

        self._draw_chart()
        self.root.after(UPDATE_MS, self._update)

    def run(self):
        self.root.after(50, self._update)
        self.root.mainloop()


def _nice_max(v: float) -> float:
    """Round a chart maximum up to a value that splits cleanly into 4."""
    if v <= 12:
        return 12
    mag = 10 ** math.floor(math.log10(v))
    for step in (1, 2, 4, 5, 8, 10):
        if v <= step * mag:
            return step * mag
    return 10 * mag


def _short_num(v: float) -> str:
    if v >= 1000:
        return f"{v / 1000:.1f}k"
    if v >= 10:
        return f"{v:.0f}"
    return f"{v:.1f}"


def run_ui(meter: DamageMeter, title: str = "MHGU Damage Meter") -> None:
    MeterWindow(meter, title).run()
