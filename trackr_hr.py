#!/usr/bin/env python3
"""
TRACKR HEART RATE - Training App (GUI)
======================================

Desktop training application for the TRACKR HEART RATE chest strap.

* Connects over Bluetooth LE or ANT+ (or a built-in simulator for testing)
* Advanced beat detection: artifact rejection, missed-beat repair,
  sensor fusion and adaptive smoothing on the raw R-R intervals
* Real-time heart rate chart coloured by training zone, with a
  customizable display period = training time in minutes (default 50)
* Start / Pause / Resume / Stop session controls
* Live HR, zone, elapsed / remaining time, HRV (RMSSD), signal quality,
  average / peak HR, training load (TRIMP), calories, time in zone
* Every session is saved as CSV + chart PNG in ./trackr_sessions

Install
-------
    pip install matplotlib numpy     # required (Tkinter ships with Python)
    pip install bleak                # Bluetooth LE
    pip install openant              # ANT+ (needs an ANT+ USB stick)

Run
---
    python trackr_hr.py                         # open the app
    python trackr_hr.py --source sim            # start with the simulator selected
    python trackr_hr.py --minutes 60 --age 35 --rest-hr 55
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import math
import os
import queue
import random
import re
import statistics
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Callable, Deque, List, Optional

import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np
import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

# ==========================================================================
# Bluetooth SIG Heart Rate profile identifiers
# ==========================================================================
HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

Log = Callable[[str], None]


# ==========================================================================
# Data model
# ==========================================================================
@dataclass
class RawSample:
    """One packet from the sensor, normalised across BLE / ANT+ / simulator."""
    timestamp: float
    device_hr: int                                    # HR computed by strap firmware
    rr_ms: List[float] = field(default_factory=list)  # beat-to-beat intervals
    contact: Optional[bool] = None                    # skin contact, if reported
    energy_kj: Optional[int] = None
    source: str = ""


# ==========================================================================
# Sensor packet decoding
# ==========================================================================
def parse_ble_hr_measurement(data: bytes) -> RawSample:
    """Decode a Heart Rate Measurement characteristic (0x2A37) packet."""
    flags = data[0]
    idx = 1
    if flags & 0x01:                       # HR value is uint16
        hr = int.from_bytes(data[idx:idx + 2], "little")
        idx += 2
    else:                                  # HR value is uint8
        hr = data[idx]
        idx += 1

    contact = None
    if flags & 0x04:                       # contact status supported
        contact = bool(flags & 0x02)

    energy = None
    if flags & 0x08:                       # energy expended present
        energy = int.from_bytes(data[idx:idx + 2], "little")
        idx += 2

    rr: List[float] = []
    if flags & 0x10:                       # RR intervals present, 1/1024 s units
        while idx + 1 < len(data):
            raw = int.from_bytes(data[idx:idx + 2], "little")
            rr.append(raw * 1000.0 / 1024.0)
            idx += 2

    return RawSample(time.time(), hr, rr, contact, energy, "BLE")


# ==========================================================================
# Advanced detection algorithm
# ==========================================================================
class HeartRateDetector:
    """
    Multi-stage beat detection and artifact rejection:

    1. Physiological gate   - drop R-R intervals outside 270..2000 ms (30..220 bpm).
    2. Adaptive median gate - drop beats deviating > tolerance from the rolling
       median (ectopic beats, strap-motion spikes). During warm-up the strap's
       own HR is the reference. Tolerance widens after repeated rejections so a
       genuine fast change (sprint start) is re-acquired instead of locked out.
    3. Missed-beat repair   - an interval ~2x or ~3x the median is split into
       equal beats instead of being thrown away (excluded from HRV).
    4. Sensor fusion        - beat-derived HR is blended with the strap's HR,
       weighted by a confidence score (contact, rejection rate, RR stability).
    5. Adaptive smoothing   - Kalman-style filter that reacts faster during rapid
       HR change and smooths harder during steady state or poor signal.
    """

    def __init__(self, window: int = 16, tolerance: float = 0.20):
        self.base_tolerance = tolerance
        self.tolerance = tolerance
        self.rr_hist: Deque[float] = deque(maxlen=window)
        self.hrv_hist: Deque[Optional[float]] = deque(maxlen=60)
        self.quality_hist: Deque[int] = deque(maxlen=30)
        self.consecutive_rejects = 0
        self.x: Optional[float] = None       # Kalman state
        self.p = 25.0

    def _filter_rr(self, rr_list: List[float], device_hr: int = 0) -> tuple[List[float], int]:
        accepted, rejected = [], 0
        for rr in rr_list:
            if not 270.0 <= rr <= 2000.0:
                rejected += 1
                self.quality_hist.append(0)
                self.hrv_hist.append(None)
                continue

            if len(self.rr_hist) >= 4:
                med = statistics.median(self.rr_hist)
            elif device_hr > 0:
                med = 60000.0 / device_hr
            else:
                accepted.append(rr)
                self.rr_hist.append(rr)
                self.quality_hist.append(1)
                self.hrv_hist.append(rr)
                continue
            ratio = rr / med

            for n in (2, 3):                                  # missed-beat repair
                if abs(ratio - n) < 0.15:
                    split = rr / n
                    accepted.extend([split] * n)
                    self.rr_hist.extend([split] * n)
                    self.quality_hist.append(1)
                    self.hrv_hist.append(None)
                    self.consecutive_rejects = 0
                    break
            else:
                if abs(ratio - 1.0) <= self.tolerance:
                    accepted.append(rr)
                    self.rr_hist.append(rr)
                    self.quality_hist.append(1)
                    self.hrv_hist.append(rr)
                    self.consecutive_rejects = 0
                    self.tolerance = max(self.base_tolerance, self.tolerance * 0.9)
                else:
                    rejected += 1
                    self.quality_hist.append(0)
                    self.hrv_hist.append(None)
                    self.consecutive_rejects += 1
                    if self.consecutive_rejects >= 3:          # re-acquire
                        self.tolerance = min(0.6, self.tolerance + 0.1)
                        self.rr_hist.clear()
                        self.rr_hist.append(rr)
                        self.consecutive_rejects = 0
        return accepted, rejected

    def _confidence(self, contact: Optional[bool], has_rr: bool) -> float:
        if contact is False:
            return 0.05
        q = (sum(self.quality_hist) / len(self.quality_hist)) if self.quality_hist else 0.5
        stability = 1.0
        if len(self.rr_hist) >= 4:
            cv = statistics.pstdev(self.rr_hist) / statistics.mean(self.rr_hist)
            stability = max(0.0, 1.0 - cv * 4)
        conf = 0.6 * q + 0.4 * stability
        if not has_rr:
            conf *= 0.8
        return round(min(1.0, max(0.0, conf)), 2)

    def rmssd(self) -> Optional[float]:
        rr = list(self.hrv_hist)
        diffs = [(b - a) ** 2 for a, b in zip(rr, rr[1:]) if a is not None and b is not None]
        if len(diffs) < 10:
            return None
        return math.sqrt(sum(diffs) / len(diffs))

    def _kalman(self, z: float, conf: float) -> float:
        if self.x is None:
            self.x = z
            return z
        innovation = abs(z - self.x)
        q = 0.5 + min(innovation, 30) * 0.3
        r = 4.0 + (1.0 - conf) * 40.0
        self.p += q
        k = self.p / (self.p + r)
        self.x += k * (z - self.x)
        self.p *= (1 - k)
        return self.x

    def process(self, s: RawSample) -> tuple[float, float, List[float], int]:
        accepted, rejected = self._filter_rr(s.rr_ms, s.device_hr)
        conf = self._confidence(s.contact, bool(s.rr_ms))
        if self.rr_hist:
            rr_hr = 60000.0 / statistics.median(self.rr_hist)
            fused = conf * rr_hr + (1 - conf) * s.device_hr if s.device_hr else rr_hr
        else:
            fused = float(s.device_hr)
        if fused <= 0:
            return (self.x or 0.0), conf, accepted, rejected
        return self._kalman(fused, conf), conf, accepted, rejected


# ==========================================================================
# Training analytics: zones, TRIMP, calories
# ==========================================================================
ZONES = [
    (0.50, 1, "Z1 Recovery"),
    (0.60, 2, "Z2 Endurance"),
    (0.70, 3, "Z3 Tempo"),
    (0.80, 4, "Z4 Threshold"),
    (0.90, 5, "Z5 VO2max"),
]


class TrainingAnalytics:
    """Karvonen (heart-rate reserve) zones, Banister TRIMP and kcal estimate."""

    def __init__(self, age: int, rest_hr: int, max_hr: Optional[int],
                 weight_kg: float, male: bool):
        self.rest_hr = rest_hr
        self.max_hr = max_hr or round(208 - 0.7 * age)   # Tanaka formula
        self.age, self.weight, self.male = age, weight_kg, male
        self.trimp = 0.0
        self.kcal = 0.0
        self.last_t: Optional[float] = None
        self.zone_time = {z: 0.0 for z in range(1, 6)}
        self.peak_hr = 0.0
        self.hr_sum = 0.0
        self.hr_n = 0

    @property
    def avg_hr(self) -> float:
        return self.hr_sum / self.hr_n if self.hr_n else 0.0

    def hrr_fraction(self, hr: float) -> float:
        return (hr - self.rest_hr) / max(1, self.max_hr - self.rest_hr)

    def zone(self, hr: float) -> tuple[int, str]:
        frac = self.hrr_fraction(hr)
        zone, name = 1, ZONES[0][2]
        for thr, z, n in ZONES:
            if frac >= thr:
                zone, name = z, n
        if frac < ZONES[0][0]:
            name = "Warm-up"
        return zone, name

    def zone_bounds(self) -> List[tuple[str, int, int]]:
        out = []
        for i, (thr, _, name) in enumerate(ZONES):
            hi = ZONES[i + 1][0] if i + 1 < len(ZONES) else 1.0
            lo_bpm = round(self.rest_hr + thr * (self.max_hr - self.rest_hr))
            hi_bpm = round(self.rest_hr + hi * (self.max_hr - self.rest_hr))
            out.append((name, lo_bpm, hi_bpm))
        return out

    def update(self, t: float, hr: float, zone: int) -> None:
        if hr > 0:
            self.peak_hr = max(self.peak_hr, hr)
            self.hr_sum += hr
            self.hr_n += 1
        if self.last_t is None:
            self.last_t = t
            return
        dt_s = min(5.0, t - self.last_t)
        self.last_t = t
        if hr <= 0:
            return
        self.zone_time[zone] += dt_s
        frac = max(0.0, self.hrr_fraction(hr))
        b = 1.92 if self.male else 1.67
        self.trimp += (dt_s / 60.0) * frac * 0.64 * math.exp(b * frac)
        if self.male:   # Keytel et al. (2005)
            kcal_min = (-55.0969 + 0.6309 * hr + 0.1988 * self.weight + 0.2017 * self.age) / 4.184
        else:
            kcal_min = (-20.4022 + 0.4472 * hr - 0.1263 * self.weight + 0.074 * self.age) / 4.184
        self.kcal += max(0.0, kcal_min) * dt_s / 60.0


# ==========================================================================
# Sensor sources (run inside a background asyncio thread)
# ==========================================================================
async def ble_scan(log: Log, timeout: float = 8.0) -> List[tuple[str, str, int]]:
    try:
        from bleak import BleakScanner
    except ImportError:
        raise RuntimeError("Bluetooth needs the bleak package:  pip install bleak")
    log(f"Scanning for heart rate straps ({timeout:.0f} s)...")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    hits = []
    for dev, adv in found.values():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        name = dev.name or adv.local_name or "Unknown"
        if HR_SERVICE_UUID in uuids or "trackr" in name.lower():
            hits.append((name, dev.address, adv.rssi or -999))
    # TRACKR first, then strongest signal
    hits.sort(key=lambda h: ("trackr" not in h[0].lower(), -h[2]))
    log(f"Found {len(hits)} heart rate strap(s)." if hits else
        "No straps found. Moisten the electrodes and wear the strap to wake it.")
    return hits


async def ble_source(address: Optional[str], log: Log) -> AsyncIterator[RawSample]:
    try:
        from bleak import BleakClient
    except ImportError:
        raise RuntimeError("Bluetooth needs the bleak package:  pip install bleak")

    if not address:
        hits = await ble_scan(log)
        if not hits:
            return
        address = hits[0][1]
        log(f"Connecting to {hits[0][0]} ({address})...")

    q: asyncio.Queue[RawSample] = asyncio.Queue()

    def on_notify(_handle, data: bytearray):
        q.put_nowait(parse_ble_hr_measurement(bytes(data)))

    while True:                                    # auto-reconnect loop
        try:
            async with BleakClient(address, timeout=15.0) as client:
                try:
                    batt = await client.read_gatt_char(BATTERY_LEVEL_UUID)
                    log(f"Connected via Bluetooth. Battery {batt[0]}%")
                except Exception:
                    log("Connected via Bluetooth.")
                await client.start_notify(HR_MEASUREMENT_UUID, on_notify)
                while client.is_connected:
                    try:
                        yield await asyncio.wait_for(q.get(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"Bluetooth link lost ({exc}). Reconnecting in 3 s...")
        await asyncio.sleep(3)


async def ant_source(device_id: int, log: Log) -> AsyncIterator[RawSample]:
    """ANT+ Heart Rate profile (device type 120) via the openant library."""
    try:
        from openant.easy.node import Node
        from openant.devices import ANTPLUS_NETWORK_KEY
        from openant.devices.heart_rate import HeartRate
    except ImportError:
        raise RuntimeError("ANT+ needs openant and an ANT+ USB stick:  pip install openant")

    loop = asyncio.get_running_loop()
    q: asyncio.Queue[RawSample] = asyncio.Queue()
    state = {"t": None, "n": None}

    node = Node()
    node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
    device = HeartRate(node, device_id=device_id)

    def on_found():
        loop.call_soon_threadsafe(log, f"ANT+ strap found: {device}")

    def on_data(_page, _page_name, data):
        hr = int(getattr(data, "heart_rate", 0) or 0)
        bt = getattr(data, "beat_time", None)      # field names vary by openant version
        bc = getattr(data, "beat_count", None)
        rr: List[float] = []
        if bt is not None and bc is not None:
            if state["t"] is not None and bc != state["n"]:
                dn = (bc - state["n"]) % 256
                dt = bt - state["t"]
                if isinstance(bt, int):            # raw 1/1024 s ticks
                    dt = (dt % 65536) / 1024.0
                elif dt < 0:
                    dt += 64.0
                if dn == 1 and dt > 0:
                    rr.append(dt * 1000.0)
            state["t"], state["n"] = bt, bc
        loop.call_soon_threadsafe(q.put_nowait, RawSample(time.time(), hr, rr, None, None, "ANT+"))

    device.on_found = on_found
    device.on_device_data = on_data
    threading.Thread(target=node.start, daemon=True).start()
    log("Listening for ANT+ heart rate strap...")
    try:
        while True:
            yield await q.get()
    finally:
        for fn in (device.close_channel, node.stop):
            try:
                fn()
            except Exception:
                pass


async def sim_source(log: Log) -> AsyncIterator[RawSample]:
    """Simulated workout: warm-up, intervals, cool-down, with injected artifacts."""
    t0 = time.time()
    rr_acc = 0.0
    log("Simulator connected (warm-up -> intervals -> cool-down, 2-min cycle).")
    while True:
        el = time.time() - t0
        phase = el % 120
        if phase < 20:
            target = 75 + phase * 2.5
        elif phase < 80:
            target = 165 if int((phase - 20) // 15) % 2 == 0 else 140
        else:
            target = 140 - (phase - 80) * 1.5
        target += 3 * math.sin(el / 4)

        rr_list = []
        while rr_acc < 1000:
            rr = 60000.0 / target + random.gauss(0, 12)
            r = random.random()
            if r < 0.03:
                rr *= 0.55          # ectopic / noise spike
            elif r < 0.05:
                rr *= 2.0           # missed beat
            rr_list.append(rr)
            rr_acc += rr
        rr_acc -= 1000
        contact = random.random() > 0.01
        dev_hr = int(target + random.gauss(0, 2)) if contact else 0
        yield RawSample(time.time(), dev_hr, rr_list, contact, None, "SIM")
        await asyncio.sleep(1.0)


class SensorWorker:
    """Runs a sensor source in its own thread + asyncio loop, posting to a queue."""

    def __init__(self, source: str, address: Optional[str], ant_id: int, out: queue.Queue):
        self.source, self.address, self.ant_id, self.out = source, address, ant_id, out
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.task: Optional[asyncio.Task] = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        if self.loop and self.task and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.task.cancel)

    def _log(self, msg: str) -> None:
        self.out.put(("status", msg))

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(self._main())
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log(f"Sensor error: {exc}")
        finally:
            self.loop.close()
            self.out.put(("disconnected", self))

    async def _main(self) -> None:
        if self.source == "ble":
            gen = ble_source(self.address, self._log)
        elif self.source == "ant":
            gen = ant_source(self.ant_id, self._log)
        else:
            gen = sim_source(self._log)
        try:
            async for raw in gen:
                self.out.put(("sample", raw))
        finally:
            await gen.aclose()


# ==========================================================================
# GUI
# ==========================================================================
BG, PANEL, PANEL2 = "#0f1419", "#182028", "#1f2933"
FG, MUTED, ACCENT = "#e6edf3", "#8b98a5", "#ff4d5e"
ZONE_HEX = {1: "#9aa5b1", 2: "#3b82f6", 3: "#22c55e", 4: "#f59e0b", 5: "#ef4444"}
SOURCES = {"Bluetooth LE": "ble", "ANT+": "ant", "Simulator": "sim"}
FONT = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
DEFAULT_MINUTES = 50


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class TrackrApp(tk.Tk):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.title("TRACKR HEART RATE - Training")
        self.geometry("1320x840")
        self.minsize(1080, 700)
        self.configure(bg=BG)

        self.q: queue.Queue = queue.Queue()
        self.worker: Optional[SensorWorker] = None
        self.state = "idle"                 # idle | running | paused
        self.accum = 0.0                    # active seconds before the current run segment
        self.run_started = 0.0
        self.session_start: Optional[datetime] = None
        self.last_sample_mono = 0.0
        self.connected = False
        self.target_notified = False
        self.last_alert = 0.0
        self.chart_dirty = True
        self.scan_map: dict[str, str] = {}

        self.detector = HeartRateDetector()
        self.analytics = TrainingAnalytics(30, 60, None, 70, True)
        self.t_min: List[float] = []
        self.hr_pts: List[float] = []
        self.z_pts: List[int] = []
        self.rows: List[list] = []
        self.pause_marks: List[float] = []

        # ---- settings variables ----
        self.v_source = tk.StringVar(value={v: k for k, v in SOURCES.items()}[args.source])
        self.v_device = tk.StringVar(value=args.address or "")
        self.v_ant = tk.StringVar(value=str(args.ant_id))
        self.v_age = tk.StringVar(value=str(args.age))
        self.v_rest = tk.StringVar(value=str(args.rest_hr))
        self.v_max = tk.StringVar(value=str(args.max_hr) if args.max_hr else "")
        self.v_weight = tk.StringVar(value=str(args.weight))
        self.v_sex = tk.StringVar(value="Male" if args.sex == "m" else "Female")
        self.v_minutes = tk.StringVar(value=str(args.minutes))
        self.v_alert = tk.StringVar(value=str(args.alert_hr) if args.alert_hr else "")

        self._style()
        self._build()
        self._apply_athlete(preview=True)
        self._setup_chart()
        self._set_buttons()

        for v in (self.v_age, self.v_rest, self.v_max):
            v.trace_add("write", lambda *_: self._apply_athlete(preview=True))
        self.v_minutes.trace_add("write", lambda *_: self._on_minutes_changed())
        self.v_source.trace_add("write", lambda *_: self._on_source_changed())
        self._on_source_changed()

        self.bind("<space>", self._space_toggle)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll)
        self.after(1000, self._redraw_chart)

    # ------------------------------------------------------------------ style
    def _style(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=PANEL, foreground=FG, fieldbackground=PANEL2,
                     bordercolor=PANEL2, lightcolor=PANEL2, darkcolor=PANEL2,
                     insertcolor=FG, font=(FONT, 10))
        st.configure("TLabel", background=PANEL, foreground=FG)
        st.configure("TEntry", fieldbackground=PANEL2, foreground=FG, padding=4)
        st.configure("TSpinbox", fieldbackground=PANEL2, foreground=FG, arrowcolor=FG,
                     background=PANEL2, padding=3)
        st.configure("TCombobox", fieldbackground=PANEL2, foreground=FG, arrowcolor=FG,
                     background=PANEL2, padding=3)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL2), ("disabled", PANEL)],
               foreground=[("disabled", MUTED)])
        st.map("TEntry", foreground=[("disabled", MUTED)], fieldbackground=[("disabled", PANEL)])
        st.map("TSpinbox", foreground=[("disabled", MUTED)], fieldbackground=[("disabled", PANEL)])
        self.option_add("*TCombobox*Listbox.background", PANEL2)
        self.option_add("*TCombobox*Listbox.foreground", FG)

        for name, color in (("Start", "#16a34a"), ("Pause", "#d97706"),
                            ("Resume", "#2563eb"), ("Stop", "#dc2626"), ("Small", "#334155")):
            st.configure(f"{name}.TButton", background=color, foreground="white",
                         font=(FONT, 12 if name != "Small" else 9, "bold"),
                         padding=(10, 10) if name != "Small" else (6, 3), borderwidth=0)
            st.map(f"{name}.TButton",
                   background=[("disabled", "#2a333d"), ("active", color)],
                   foreground=[("disabled", "#5b6773")])

    # ------------------------------------------------------------------ layout
    def _section(self, parent: tk.Widget, title: str) -> tk.Frame:
        tk.Label(parent, text=title.upper(), bg=PANEL, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="x", padx=14)
        f.columnconfigure(1, weight=1)
        return f

    def _row(self, f: tk.Frame, r: int, label: str, widget: tk.Widget) -> tk.Widget:
        tk.Label(f, text=label, bg=PANEL, fg=FG, font=(FONT, 10)).grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        widget.grid(row=r, column=1, sticky="ew", pady=3)
        return widget

    def _card(self, parent: tk.Widget, title: str, size: int = 18) -> tuple[tk.Frame, tk.Label]:
        f = tk.Frame(parent, bg=PANEL, padx=12, pady=8)
        tk.Label(f, text=title.upper(), bg=PANEL, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        v = tk.Label(f, text="--", bg=PANEL, fg=FG, font=(FONT, size, "bold"))
        v.pack(anchor="w")
        return f, v

    def _build(self) -> None:
        # header
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=16, pady=(12, 6))
        tk.Label(hdr, text="♥ TRACKR", bg=BG, fg=ACCENT, font=(FONT, 20, "bold")).pack(side="left")
        tk.Label(hdr, text=" HEART RATE  ·  Training", bg=BG, fg=FG,
                 font=(FONT, 16)).pack(side="left", pady=(4, 0))
        self.state_lbl = tk.Label(hdr, text="READY", bg=PANEL2, fg=FG,
                                  font=(FONT, 10, "bold"), padx=10, pady=3)
        self.state_lbl.pack(side="right")
        self.conn_lbl = tk.Label(hdr, text="● Not connected", bg=BG, fg=MUTED, font=(FONT, 10))
        self.conn_lbl.pack(side="right", padx=12)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16)

        # ---------------- left panel: settings + controls
        left = tk.Frame(body, bg=PANEL, width=300)
        left.pack(side="left", fill="y", pady=(0, 8))
        left.pack_propagate(False)

        self.lockable: List[tk.Widget] = []

        f = self._section(left, "Sensor")
        cb = ttk.Combobox(f, textvariable=self.v_source, values=list(SOURCES), state="readonly", width=16)
        self.lockable.append(self._row(f, 0, "Connection", cb))
        dev = tk.Frame(f, bg=PANEL)
        self.dev_cb = ttk.Combobox(dev, textvariable=self.v_device, width=14)
        self.dev_cb.pack(side="left", fill="x", expand=True)
        self.scan_btn = ttk.Button(dev, text="Scan", style="Small.TButton", command=self._scan)
        self.scan_btn.pack(side="left", padx=(4, 0))
        self._row(f, 1, "BLE device", dev)
        self.lockable.append(self.dev_cb)
        self.ant_entry = ttk.Entry(f, textvariable=self.v_ant, width=10)
        self.lockable.append(self._row(f, 2, "ANT+ ID (0=any)", self.ant_entry))

        f = self._section(left, "Athlete")
        for r, (lbl, var, lo, hi) in enumerate((("Age", self.v_age, 10, 100),
                                                 ("Resting HR", self.v_rest, 30, 120))):
            sp = ttk.Spinbox(f, from_=lo, to=hi, textvariable=var, width=8)
            self.lockable.append(self._row(f, r, lbl, sp))
        self.lockable.append(self._row(f, 2, "Max HR (blank=auto)", ttk.Entry(f, textvariable=self.v_max, width=8)))
        self.lockable.append(self._row(f, 3, "Weight (kg)", ttk.Entry(f, textvariable=self.v_weight, width=8)))
        self.lockable.append(self._row(f, 4, "Sex", ttk.Combobox(f, textvariable=self.v_sex,
                                                                 values=["Male", "Female"],
                                                                 state="readonly", width=8)))

        f = self._section(left, "Session")
        self._row(f, 0, "Training time (min)",
                  ttk.Spinbox(f, from_=1, to=600, increment=5, textvariable=self.v_minutes, width=8))
        self._row(f, 1, "HR alert (bpm)", ttk.Entry(f, textvariable=self.v_alert, width=8))

        ctl = tk.Frame(left, bg=PANEL)
        ctl.pack(fill="x", padx=14, pady=(18, 6))
        ctl.columnconfigure((0, 1), weight=1, uniform="b")
        self.btn_start = ttk.Button(ctl, text="▶  Start", style="Start.TButton", command=self.start)
        self.btn_pause = ttk.Button(ctl, text="❚❚  Pause", style="Pause.TButton", command=self.pause)
        self.btn_resume = ttk.Button(ctl, text="▶  Resume", style="Resume.TButton", command=self.resume)
        self.btn_stop = ttk.Button(ctl, text="■  Stop", style="Stop.TButton", command=self.stop)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=4)
        self.btn_pause.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=4)
        self.btn_resume.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=4)
        self.btn_stop.grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=4)
        tk.Label(left, text="Space = pause / resume", bg=PANEL, fg=MUTED,
                 font=(FONT, 8)).pack(anchor="w", padx=14)

        # ---------------- right panel: metrics + chart
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        top = tk.Frame(right, bg=BG)
        top.pack(fill="x")

        hrcard = tk.Frame(top, bg=PANEL, padx=18, pady=8, width=280, height=172)
        hrcard.pack(side="left", fill="y")
        hrcard.pack_propagate(False)
        tk.Label(hrcard, text="HEART RATE", bg=PANEL, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        hrline = tk.Frame(hrcard, bg=PANEL)
        hrline.pack(anchor="w")
        self.hr_lbl = tk.Label(hrline, text="--", bg=PANEL, fg=FG, font=(FONT, 52, "bold"))
        self.hr_lbl.pack(side="left")
        tk.Label(hrline, text="bpm", bg=PANEL, fg=MUTED, font=(FONT, 14)).pack(side="left", anchor="s", pady=(0, 16))
        self.zone_lbl = tk.Label(hrcard, text="—", bg=PANEL, fg=MUTED, font=(FONT, 12, "bold"))
        self.zone_lbl.pack(anchor="w")

        grid = tk.Frame(top, bg=BG)
        grid.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.cards = {}
        names = [("elapsed", "Elapsed"), ("remaining", "Remaining"), ("avg", "Avg HR"), ("peak", "Peak HR"),
                 ("hrv", "HRV (RMSSD)"), ("signal", "Signal"), ("trimp", "Training load"), ("kcal", "Calories")]
        for i, (key, title) in enumerate(names):
            card, val = self._card(grid, title)
            card.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0 if i % 4 == 0 else 6, 0),
                      pady=(0 if i < 4 else 6, 0))
            self.cards[key] = val
        for c in range(4):
            grid.columnconfigure(c, weight=1, uniform="c")
        grid.rowconfigure((0, 1), weight=1)

        chart_frame = tk.Frame(right, bg=PANEL)
        chart_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.fig = Figure(figsize=(8, 4), dpi=100, facecolor=PANEL)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas.get_tk_widget().configure(bg=PANEL, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)

        zf = tk.Frame(right, bg=PANEL, padx=12, pady=8)
        zf.pack(fill="x", pady=(8, 8))
        tk.Label(zf, text="TIME IN ZONE", bg=PANEL, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        self.zone_canvas = tk.Canvas(zf, bg=PANEL, height=38, highlightthickness=0)
        self.zone_canvas.pack(fill="x", pady=(4, 0))
        self.zone_canvas.bind("<Configure>", lambda e: self._draw_zone_bar())

        # status bar
        self.status_lbl = tk.Label(self, text="Choose a connection and press Start.", bg=PANEL2,
                                   fg=MUTED, anchor="w", padx=12, pady=5, font=(FONT, 9))
        self.status_lbl.pack(fill="x", side="bottom")

    # ------------------------------------------------------------------ settings
    def _int(self, var: tk.StringVar, default: Optional[int], lo: int, hi: int) -> Optional[int]:
        s = var.get().strip()
        if not s:
            return default
        v = int(float(s))
        if not lo <= v <= hi:
            raise ValueError
        return v

    def _minutes(self) -> float:
        try:
            m = float(self.v_minutes.get())
            return m if 1 <= m <= 600 else DEFAULT_MINUTES
        except ValueError:
            return DEFAULT_MINUTES

    def _apply_athlete(self, preview: bool = False) -> bool:
        try:
            age = self._int(self.v_age, 30, 10, 100)
            rest = self._int(self.v_rest, 60, 30, 120)
            mx = self._int(self.v_max, None, 120, 230)
            weight = float(self.v_weight.get() or 70)
            if not 30 <= weight <= 250:
                raise ValueError
        except ValueError:
            if not preview:
                messagebox.showerror("Settings", "Please check the athlete settings "
                                     "(age 10-100, resting HR 30-120, max HR 120-230, weight 30-250 kg).")
            return False
        a = TrainingAnalytics(age, rest, mx, weight, self.v_sex.get() == "Male")
        if not preview or self.state == "idle":
            self.analytics = a
            if preview and hasattr(self, "ax"):
                self._setup_chart()
        return True

    def _lock_settings(self, locked: bool) -> None:
        for w in self.lockable:
            if isinstance(w, ttk.Combobox) and w is not self.dev_cb:
                w.config(state="disabled" if locked else "readonly")
            else:
                w.config(state="disabled" if locked else "normal")
        self.scan_btn.config(state="disabled" if locked else "normal")
        if not locked:
            self._on_source_changed()

    def _on_source_changed(self) -> None:
        if self.state != "idle":
            return
        src = SOURCES.get(self.v_source.get(), "ble")
        self.dev_cb.config(state="normal" if src == "ble" else "disabled")
        self.scan_btn.config(state="normal" if src == "ble" else "disabled")
        self.ant_entry.config(state="normal" if src == "ant" else "disabled")

    def _on_minutes_changed(self) -> None:
        self.target_notified = self._elapsed() >= self._minutes() * 60
        self.chart_dirty = True

    # ------------------------------------------------------------------ chart
    def _setup_chart(self) -> None:
        ax, a = self.ax, self.analytics
        ax.clear()
        ax.set_facecolor(PANEL)
        self.fig.subplots_adjust(left=0.06, right=0.88, top=0.93, bottom=0.12)
        for (name, lo, hi), z in zip(a.zone_bounds(), range(1, 6)):
            ax.axhspan(lo, hi, color=ZONE_HEX[z], alpha=0.10, lw=0)
            ax.text(1.01, (lo + hi) / 2, f"{name}\n{lo}–{hi}", transform=ax.get_yaxis_transform(),
                    color=ZONE_HEX[z], va="center", fontsize=8, fontweight="bold", linespacing=1.1)
        ax.grid(True, color="#2a343f", lw=0.6)
        for s in ax.spines.values():
            s.set_color("#2a343f")
        ax.tick_params(colors=MUTED, labelsize=9)
        ax.set_xlabel("Training time (min)", color=MUTED, fontsize=9)
        ax.set_ylabel("Heart rate (bpm)", color=MUTED, fontsize=9)
        self.chart_title = ax.set_title("", color=FG, fontsize=10, loc="left")
        self.lc = LineCollection([], linewidths=2.4, capstyle="round")
        ax.add_collection(self.lc)
        (self.dot,) = ax.plot([], [], "o", ms=8, color="white", mec=ACCENT, mew=2, zorder=5)
        self.target_line = ax.axvline(self._minutes(), color=FG, ls="--", lw=1, alpha=0.5)
        for m in self.pause_marks:
            ax.axvline(m, color=MUTED, ls=":", lw=1)
        self.chart_dirty = True
        self._redraw_chart(schedule=False)

    def _redraw_chart(self, schedule: bool = True) -> None:
        try:
            if self.chart_dirty:
                self.chart_dirty = False
                a, minutes = self.analytics, self._minutes()
                xs, ys = self.t_min, self.hr_pts
                if len(xs) >= 2:
                    pts = np.column_stack([xs, ys])
                    self.lc.set_segments(np.stack([pts[:-1], pts[1:]], axis=1))
                    self.lc.set_colors([ZONE_HEX[z] for z in self.z_pts[1:]])
                else:
                    self.lc.set_segments([])
                self.dot.set_data(xs[-1:], ys[-1:])
                xmax = max(minutes, (xs[-1] if xs else 0))
                self.ax.set_xlim(0, xmax)
                ymin = min([max(40, a.rest_hr - 10)] + [y - 5 for y in ys[-3000:]])
                ymax = max([a.max_hr + 10] + [y + 5 for y in ys[-3000:]])
                self.ax.set_ylim(ymin, ymax)
                self.target_line.set_xdata([minutes, minutes])
                self.chart_title.set_text(f"Real-time heart rate · display period {minutes:g} min")
                self.canvas.draw_idle()
        finally:
            if schedule:
                self.after(1000, self._redraw_chart)

    def _draw_zone_bar(self) -> None:
        c = self.zone_canvas
        c.delete("all")
        w = max(c.winfo_width(), 100)
        total = sum(self.analytics.zone_time.values())
        x = 0.0
        if total <= 0:
            c.create_rectangle(0, 2, w, 18, fill=PANEL2, width=0)
        for z in range(1, 6):
            t = self.analytics.zone_time[z]
            if total > 0 and t > 0:
                seg = w * t / total
                c.create_rectangle(x, 2, x + seg, 18, fill=ZONE_HEX[z], width=0)
                x += seg
        for z in range(1, 6):
            t = self.analytics.zone_time[z]
            pct = 100 * t / total if total else 0
            cx = (z - 0.5) * w / 5
            c.create_text(cx, 30, text=f"Z{z}  {fmt_time(t)}  ({pct:.0f}%)",
                          fill=ZONE_HEX[z], font=(FONT, 9, "bold"))

    # ------------------------------------------------------------------ session control
    def _elapsed(self) -> float:
        if self.state == "running":
            return self.accum + (time.monotonic() - self.run_started)
        return self.accum

    def _set_buttons(self) -> None:
        st = self.state
        self.btn_start.state(["!disabled"] if st == "idle" else ["disabled"])
        self.btn_pause.state(["!disabled"] if st == "running" else ["disabled"])
        self.btn_resume.state(["!disabled"] if st == "paused" else ["disabled"])
        self.btn_stop.state(["!disabled"] if st in ("running", "paused") else ["disabled"])
        text, color = {"idle": ("READY", PANEL2), "running": ("● RECORDING", "#16a34a"),
                       "paused": ("❚❚ PAUSED", "#d97706")}[st]
        self.state_lbl.config(text=text, bg=color)

    def start(self) -> None:
        if self.state != "idle" or not self._apply_athlete(preview=False):
            return
        try:
            ant_id = self._int(self.v_ant, 0, 0, 65535) or 0
        except ValueError:
            messagebox.showerror("Settings", "ANT+ ID must be a number between 0 and 65535.")
            return

        self.detector = HeartRateDetector()
        self.t_min, self.hr_pts, self.z_pts, self.rows, self.pause_marks = [], [], [], [], []
        self.accum, self.run_started = 0.0, time.monotonic()
        self.session_start = datetime.now()
        self.target_notified = False
        self.state = "running"
        self._setup_chart()
        self._draw_zone_bar()
        self._lock_settings(True)
        self._set_buttons()

        if self.worker is None:
            src = SOURCES[self.v_source.get()]
            dev = self.v_device.get().strip()
            address = self.scan_map.get(dev, dev) or None
            self._status("Connecting to sensor...")
            self.worker = SensorWorker(src, address, ant_id, self.q)
            self.worker.start()
        self._status("Training started. Good luck!")

    def pause(self) -> None:
        if self.state != "running":
            return
        self.accum += time.monotonic() - self.run_started
        self.state = "paused"
        self.analytics.last_t = None            # don't count paused time
        mark = self.accum / 60
        self.pause_marks.append(mark)
        self.ax.axvline(mark, color=MUTED, ls=":", lw=1)
        self.chart_dirty = True
        self._set_buttons()
        self._status("Paused. The sensor stays connected; press Resume to continue.")

    def resume(self) -> None:
        if self.state != "paused":
            return
        self.run_started = time.monotonic()
        self.state = "running"
        self._set_buttons()
        self._status("Resumed.")

    def stop(self, ask: bool = True) -> None:
        if self.state == "idle":
            return
        if ask and not messagebox.askyesno("Stop training", "Stop this training session and save it?"):
            return
        if self.state == "running":
            self.accum += time.monotonic() - self.run_started
        self.state = "idle"
        self._disconnect()
        self._lock_settings(False)
        self._set_buttons()
        self.chart_dirty = True
        self._redraw_chart(schedule=False)
        self._save_and_summarize()

    def _space_toggle(self, event: tk.Event) -> None:
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox)):
            return
        if self.state == "running":
            self.pause()
        elif self.state == "paused":
            self.resume()

    def _disconnect(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker = None
        self._set_conn(False)

    # ------------------------------------------------------------------ data flow
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "sample":
                    if self.worker is not None:
                        self._on_sample(payload)
                elif kind == "status":
                    self._status(payload)
                elif kind == "scan":
                    self._on_scan(payload)
                elif kind == "disconnected":
                    if payload is self.worker:
                        self.worker = None
                        self._set_conn(False)
                        if self.state != "idle":
                            self._status("Sensor stopped. Check the connection, then Stop and Start again.")
        except queue.Empty:
            pass
        self._tick()
        self.after(150, self._poll)

    def _on_sample(self, raw: RawSample) -> None:
        self.last_sample_mono = time.monotonic()
        self._set_conn(True, raw.source)
        hr, conf, acc, rej = self.detector.process(raw)
        zone, zname = self.analytics.zone(hr)
        rmssd = self.detector.rmssd()

        if self.state == "running":
            self.analytics.update(raw.timestamp, hr, zone)
            el = self._elapsed()
            if hr > 0:
                self.t_min.append(el / 60.0)
                self.hr_pts.append(hr)
                self.z_pts.append(zone)
                self.chart_dirty = True
            self.rows.append([
                datetime.fromtimestamp(raw.timestamp).isoformat(timespec="milliseconds"),
                f"{el:.1f}", f"{hr:.1f}", raw.device_hr, conf,
                f"{rmssd:.1f}" if rmssd else "", zone,
                f"{self.analytics.trimp:.2f}", f"{self.analytics.kcal:.2f}",
                " ".join(f"{r:.0f}" for r in acc), rej, raw.contact])
            self._draw_zone_bar()

        # live display (also while paused)
        alert = self._alert_hr()
        over = alert is not None and hr >= alert
        self.hr_lbl.config(text=f"{hr:.0f}" if hr > 0 else "--",
                           fg=ACCENT if over else ZONE_HEX[zone])
        self.zone_lbl.config(text=f"{zname}  ·  {100 * hr / self.analytics.max_hr:.0f}% max",
                             fg=ZONE_HEX[zone])
        if raw.contact is False:
            self.cards["signal"].config(text="No contact", fg=ACCENT)
        else:
            col = "#22c55e" if conf >= 0.7 else "#f59e0b" if conf >= 0.4 else ACCENT
            self.cards["signal"].config(text=f"{conf * 100:.0f}%", fg=col)
        self.cards["hrv"].config(text=f"{rmssd:.0f} ms" if rmssd else "--")
        a = self.analytics
        self.cards["avg"].config(text=f"{a.avg_hr:.0f}" if a.hr_n else "--")
        self.cards["peak"].config(text=f"{a.peak_hr:.0f}" if a.hr_n else "--")
        self.cards["trimp"].config(text=f"{a.trimp:.1f}")
        self.cards["kcal"].config(text=f"{a.kcal:.0f}")
        if over and time.monotonic() - self.last_alert > 5:
            self.last_alert = time.monotonic()
            self.bell()
            self._status(f"⚠ Heart rate above your {alert} bpm alert!")

    def _alert_hr(self) -> Optional[int]:
        try:
            return self._int(self.v_alert, None, 60, 250)
        except ValueError:
            return None

    def _tick(self) -> None:
        el = self._elapsed()
        total = self._minutes() * 60
        self.cards["elapsed"].config(text=fmt_time(el))
        rem = total - el
        self.cards["remaining"].config(text=fmt_time(rem) if rem > 0 else f"+{fmt_time(-rem)}",
                                       fg=FG if rem > 0 else "#22c55e")
        if self.state == "running" and rem <= 0 and not self.target_notified:
            self.target_notified = True
            self.bell()
            self._status(f"🎯 Target training time of {self._minutes():g} min reached! "
                         "Keep going or press Stop to finish.")
        if self.connected and time.monotonic() - self.last_sample_mono > 6:
            self.hr_lbl.config(text="--", fg=MUTED)
            self.conn_lbl.config(text="● No signal", fg="#f59e0b")

    def _set_conn(self, on: bool, src: str = "") -> None:
        self.connected = on
        if on:
            self.conn_lbl.config(text=f"● Connected ({src})", fg="#22c55e")
        else:
            self.conn_lbl.config(text="● Not connected", fg=MUTED)
            self.hr_lbl.config(text="--", fg=FG)
            self.zone_lbl.config(text="—", fg=MUTED)

    def _status(self, msg: str) -> None:
        self.status_lbl.config(text=msg)

    # ------------------------------------------------------------------ BLE scan
    def _scan(self) -> None:
        self.scan_btn.config(state="disabled")

        def job():
            try:
                hits = asyncio.run(ble_scan(lambda m: self.q.put(("status", m))))
            except Exception as exc:
                self.q.put(("status", f"Scan failed: {exc}"))
                hits = []
            self.q.put(("scan", hits))

        threading.Thread(target=job, daemon=True).start()

    def _on_scan(self, hits: List[tuple[str, str, int]]) -> None:
        if self.state == "idle":
            self.scan_btn.config(state="normal")
        self.scan_map = {f"{n} ({addr})": addr for n, addr, _ in hits}
        self.dev_cb.config(values=list(self.scan_map))
        if hits:
            self.v_device.set(next(iter(self.scan_map)))

    # ------------------------------------------------------------------ save
    def _save_and_summarize(self) -> None:
        a, el = self.analytics, self.accum
        if not self.rows:
            self._status("Session stopped. No heart rate data was recorded, nothing saved.")
            return
        os.makedirs("trackr_sessions", exist_ok=True)
        stamp = (self.session_start or datetime.now()).strftime("%Y%m%d_%H%M%S")
        base = os.path.join("trackr_sessions", f"session_{stamp}")
        with open(base + ".csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "elapsed_s", "hr_filtered", "hr_device", "confidence", "rmssd_ms",
                        "zone", "trimp", "kcal", "rr_accepted_ms", "rr_rejected", "contact"])
            w.writerows(self.rows)
        self.fig.savefig(base + ".png", facecolor=self.fig.get_facecolor(), dpi=130)

        total = sum(a.zone_time.values()) or 1
        zones = "\n".join(f"   {name:<13} {lo}-{hi} bpm   {fmt_time(a.zone_time[z])}  "
                          f"({100 * a.zone_time[z] / total:.0f}%)"
                          for (name, lo, hi), z in zip(a.zone_bounds(), range(1, 6)))
        summary = (f"Duration:   {fmt_time(el)}  (target {self._minutes():g} min)\n"
                   f"Avg / Peak HR:   {a.avg_hr:.0f} / {a.peak_hr:.0f} bpm\n"
                   f"Training load (TRIMP):   {a.trimp:.1f}\n"
                   f"Calories:   {a.kcal:.0f} kcal\n\nTime in zone:\n{zones}\n\n"
                   f"Saved:\n   {os.path.abspath(base)}.csv\n   {os.path.abspath(base)}.png")
        self._status(f"Session saved to {os.path.abspath(base)}.csv")
        messagebox.showinfo("Training summary", summary)

    def _on_close(self) -> None:
        if self.state != "idle":
            ans = messagebox.askyesnocancel("Quit", "A training session is in progress.\n"
                                            "Save it before quitting?")
            if ans is None:
                return
            if ans:
                self.stop(ask=False)
        self._disconnect()
        self.destroy()


# ==========================================================================
# Entry point
# ==========================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="TRACKR HEART RATE training app (BLE + ANT+)")
    ap.add_argument("--source", choices=["ble", "ant", "sim"], default="ble")
    ap.add_argument("--address", help="BLE address/UUID of the strap (auto-detect if omitted)")
    ap.add_argument("--ant-id", type=int, default=0, help="ANT+ device id (0 = any)")
    ap.add_argument("--minutes", type=float, default=DEFAULT_MINUTES,
                    help="training time / chart display period in minutes (default 50)")
    ap.add_argument("--age", type=int, default=30)
    ap.add_argument("--rest-hr", type=int, default=60)
    ap.add_argument("--max-hr", type=int, help="override estimated max HR")
    ap.add_argument("--weight", type=float, default=70.0, help="kg")
    ap.add_argument("--sex", choices=["m", "f"], default="m")
    ap.add_argument("--alert-hr", type=int, help="alert when HR exceeds this value")
    args = ap.parse_args()

    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on HiDPI screens
        except Exception:
            pass

    TrackrApp(args).mainloop()


if __name__ == "__main__":
    main()
