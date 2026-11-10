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
* Connects automatically at startup to a saved Bluetooth strap and shows live
  heart rate and sensor battery level before a session starts
* Resizable settings panel (width saved in the config file)
* Every session is saved as CSV + a session report PNG (athlete details,
  date/time, summary, HR chart, time-in-zone %) in ./trackr_sessions
* Save Config button: settings are stored and loaded automatically at startup

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
    python trackr_hr.py --config coach_athlete2.json   # use another config file

Command-line options override the saved config for that run only.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
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
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import numpy as np
import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

# ==========================================================================
# Bluetooth SIG Heart Rate profile identifiers
# ==========================================================================
HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
BATTERY_POLL_S = 60          # read the sensor battery level once a minute

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
    battery: Optional[int] = None                     # sensor battery %, if known


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
class RRValidator:
    """
    Stage 0 - check the strap's R-R data before trusting it.

    * Units: the Bluetooth standard sends R-R in 1/1024 s. Some straps send
      milliseconds instead, which makes every beat look 2.4% shorter (HR about
      3 bpm too high). The R-R-derived HR is compared with the strap's own HR;
      if it matches only when read as milliseconds, R-R is rescaled (BLE only).
    * Beat-to-beat check: real R-R values are almost never identical many times
      in a row. If most consecutive values repeat, the strap is resending a
      value derived from its averaged HR rather than measuring each beat. Then
      the strap's HR is used directly and HRV is not shown.

    status: "checking" (not enough data yet), "ok", "synthetic" (not
    beat-to-beat) or "no_rr" (strap sends no R-R data).
    """
    CAL_PACKETS = 30          # packets with R-R + strap HR needed to decide
    UNIT_TOL = 0.008          # 0.8 % tolerance around the 1.000 / 1.024 ratios
    REPEAT_ON, REPEAT_OFF = 0.50, 0.30   # hysteresis for the repeat test

    def __init__(self):
        self.check_units = True
        self.scale = 1.0
        self.units_decided = False
        self.status = "checking"
        self.pairs: Deque[tuple[float, int]] = deque(maxlen=120)  # (median R-R, strap HR)
        self.recent: Deque[float] = deque(maxlen=60)
        self.hr_packets = 0
        self.packets = 0
        self.messages: List[str] = []

    def _decide_units(self) -> None:
        ratios = sorted((60000.0 / rr) / hr for rr, hr in self.pairs)
        med = ratios[len(ratios) // 2]
        if abs(med - 1.024) < self.UNIT_TOL:
            self.scale, self.units_decided = 1.024, True
            self.messages.append("This strap sends R-R intervals in milliseconds instead of the "
                                 "Bluetooth standard 1/1024 s. Corrected automatically.")
        elif abs(med - 1.0) < self.UNIT_TOL:
            self.scale, self.units_decided = 1.0, True

    def _decide_repeats(self) -> None:
        vals = list(self.recent)
        rep = sum(a == b for a, b in zip(vals, vals[1:])) / max(1, len(vals) - 1)
        if self.status != "synthetic" and rep > self.REPEAT_ON:
            self.status = "synthetic"
            self.messages.append("This strap does not send beat-to-beat R-R data. Heart rate comes "
                                 "directly from the strap; HRV is not available.")
        elif self.status in ("checking", "synthetic") and rep < self.REPEAT_OFF:
            if self.status == "synthetic":
                self.messages.append("Beat-to-beat R-R data detected again; HRV re-enabled.")
            self.status = "ok"

    def feed(self, rr_list: List[float], device_hr: int) -> List[float]:
        """Return the R-R list in correct milliseconds."""
        self.packets += 1
        if device_hr > 0:
            self.hr_packets += 1
        for rr in rr_list:
            self.recent.append(rr)
        if rr_list and device_hr > 0:
            self.pairs.append((statistics.median(rr_list), device_hr))
        if self.packets % 5 == 0:
            if self.check_units and not self.units_decided and len(self.pairs) >= self.CAL_PACKETS:
                self._decide_units()
            if not self.check_units:
                self.units_decided = True
            if len(self.recent) >= 40 and (self.units_decided or len(self.pairs) >= 3 * self.CAL_PACKETS):
                self._decide_repeats()
            if self.status == "checking" and not self.recent and self.hr_packets >= self.CAL_PACKETS:
                self.status = "no_rr"
                self.messages.append("This strap sends no R-R data. Heart rate comes directly from "
                                     "the strap; HRV is not available.")
        return [rr * self.scale for rr in rr_list] if self.scale != 1.0 else list(rr_list)

    @property
    def use_rr(self) -> bool:
        return self.status == "ok"

    def pop_messages(self) -> List[str]:
        m, self.messages = self.messages, []
        return m


class HeartRateDetector:
    """
    Multi-stage beat detection and artifact rejection:

    0. R-R validation       - correct non-standard R-R units and detect straps
       that do not send real beat-to-beat data (see RRValidator). Until the
       R-R data is confirmed, and for such straps, the strap's own HR is used.

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
        self.validator = RRValidator()
        self._source_set = False
        self.last_rr: List[float] = []       # R-R as received (after unit correction)

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

    @property
    def rr_status(self) -> str:
        return self.validator.status

    def rmssd(self) -> Optional[float]:
        if not self.validator.use_rr:
            return None
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
        if not self._source_set:                  # unit check applies to Bluetooth only
            self.validator.check_units = s.source == "BLE"
            self._source_set = True
        rr_ms = self.validator.feed(s.rr_ms, s.device_hr)
        self.last_rr = rr_ms
        if not self.validator.use_rr:
            # strap HR path: already averaged by the strap, so no extra smoothing
            conf = 0.05 if s.contact is False else 0.95 if s.contact else 0.85
            if s.device_hr > 0:
                self.x = float(s.device_hr)
            return (self.x or 0.0), conf, [], 0
        accepted, rejected = self._filter_rr(rr_ms, s.device_hr)
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

    async def read_battery(client) -> Optional[int]:
        try:
            b = await client.read_gatt_char(BATTERY_LEVEL_UUID)
            return int(b[0]) if b and 0 <= b[0] <= 100 else None
        except Exception:
            return None                             # strap has no Battery Service

    batt: Optional[int] = None
    ever_connected = False
    while True:                                    # auto-reconnect loop
        try:
            async with BleakClient(address, timeout=15.0) as client:
                ever_connected = True
                batt = await read_battery(client)
                log("Connected via Bluetooth." + (f" Sensor battery {batt}%." if batt is not None else ""))
                await client.start_notify(HR_MEASUREMENT_UUID, on_notify)
                last_batt = time.monotonic()
                while client.is_connected:
                    if time.monotonic() - last_batt >= BATTERY_POLL_S:
                        last_batt = time.monotonic()
                        batt = await read_battery(client) or batt
                    try:
                        sample = await asyncio.wait_for(q.get(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    sample.battery = batt
                    yield sample
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if ever_connected:
                log(f"Bluetooth link lost ({exc}). Reconnecting in 3 s...")
            else:
                log(f"Waiting for the heart rate strap ({address})... "
                    "Moisten the electrodes and put the strap on to wake it.")
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
        for field_name in ("battery_percentage", "battery_level", "battery"):
            v = getattr(data, field_name, None)
            if isinstance(v, (int, float)) and 0 <= v <= 100:
                state["bat"] = int(v)
                break
        loop.call_soon_threadsafe(q.put_nowait, RawSample(time.time(), hr, rr, None, None, "ANT+",
                                                          state.get("bat")))

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
        battery = max(5, 92 - int(el // 600))      # simulated: -1 % per 10 min
        yield RawSample(time.time(), dev_hr, rr_list, contact, None, "SIM", battery)
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
HR_FONT = 52                       # live heart rate number
CARD_FONT = round(HR_FONT * 0.7)   # other real-time values: 70 % of the heart rate size
CARD_FONT_MIN = 24                 # below this the cards reflow into fewer columns
DEFAULT_MINUTES = 50

# Fonts for the chart / report image, with fallbacks so non-Latin names
# (e.g. Thai) render correctly. Only fonts installed on this computer are used.
_FONT_CANDIDATES = ["DejaVu Sans", "Segoe UI", "Tahoma", "Leelawadee UI", "Thonburi",
                    "Noto Sans Thai", "Noto Sans", "Garuda", "Loma", "Arial Unicode MS"]
_installed = {f.name for f in font_manager.fontManager.ttflist}
matplotlib.rcParams["font.family"] = [f for f in _FONT_CANDIDATES if f in _installed] or ["sans-serif"]

# --------------------------------------------------------------------------
# Config file (saved with the Save Config button, loaded at startup)
# --------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".trackr_hr", "config.json")
DEFAULT_CONFIG = {
    "name": "", "source": "ble", "device": "", "device_address": "", "ant_id": 0,
    "age": 30, "rest_hr": 60, "max_hr": None, "weight": 70.0, "sex": "m",
    "minutes": DEFAULT_MINUTES, "alert_hr": None,
    "panel_width": 300,        # settings panel width in pixels (drag the divider, then Save Config)
    "auto_connect": True,      # connect to the saved Bluetooth strap when the app starts
}
PANEL_MIN, PANEL_MAX = 260, 600


def load_config(path: str) -> tuple[dict, str]:
    """Return (config, status message). Missing or unreadable files give defaults."""
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(path):
        return cfg, "No saved config yet. Enter your settings and press Save Config."
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except Exception as exc:
        return cfg, f"Saved config could not be read ({exc}); using defaults."
    cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    if cfg["source"] not in SOURCES.values():
        cfg["source"] = "ble"
    if cfg["sex"] not in ("m", "f"):
        cfg["sex"] = "m"
    try:
        cfg["panel_width"] = min(PANEL_MAX, max(PANEL_MIN, int(cfg["panel_width"])))
    except (TypeError, ValueError):
        cfg["panel_width"] = DEFAULT_CONFIG["panel_width"]
    cfg["auto_connect"] = bool(cfg["auto_connect"])
    return cfg, f"Loaded saved config from {path}"


def write_config(path: str, cfg: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)                      # atomic: never leaves a half-written file


def safe_filename(text: str) -> str:
    """Make text safe for a file name while keeping non-Latin letters."""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("._")[:40]


def style_hr_axes(ax, a: "TrainingAnalytics", zone_fontsize: float = 8) -> None:
    """Shared look for the live chart and the session report chart."""
    ax.set_facecolor(PANEL)
    for (name, lo, hi), z in zip(a.zone_bounds(), range(1, 6)):
        ax.axhspan(lo, hi, color=ZONE_HEX[z], alpha=0.10, lw=0)
        ax.text(1.01, (lo + hi) / 2, f"{name}\n{lo}–{hi}", transform=ax.get_yaxis_transform(),
                color=ZONE_HEX[z], va="center", fontsize=zone_fontsize, fontweight="bold",
                linespacing=1.1)
    ax.grid(True, color="#2a343f", lw=0.6)
    for sp in ax.spines.values():
        sp.set_color("#2a343f")
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.set_xlabel("Training time (min)", color=MUTED, fontsize=9)
    ax.set_ylabel("Heart rate (bpm)", color=MUTED, fontsize=9)


GAP_BREAK_MIN = 5 / 60      # no line is drawn across a data gap longer than 5 s
GAP_MARK_MIN = 10 / 60      # gaps of 10 s or more are shaded as "no signal"


def hr_segments(xs: List[float], ys: List[float], zs: List[int]):
    """Line segments coloured by the zone of their end point; gaps are left open."""
    if len(xs) < 2:
        return [], []
    pts = np.column_stack([xs, ys])
    keep = np.diff(pts[:, 0]) <= GAP_BREAK_MIN
    segs = np.stack([pts[:-1], pts[1:]], axis=1)[keep]
    return segs, [ZONE_HEX[z] for z, k in zip(zs[1:], keep) if k]


def draw_gap(ax, x0: float, x1: float) -> None:
    """Shade a period without heart rate data."""
    ax.axvspan(x0, x1, color=MUTED, alpha=0.18, lw=0, zorder=0)
    if x1 - x0 >= 0.25:
        ax.text((x0 + x1) / 2, 0.03, "no signal", transform=ax.get_xaxis_transform(),
                color=MUTED, fontsize=7.5, ha="center", va="bottom", rotation=90)


def hr_limits(a: "TrainingAnalytics", xs: List[float], ys: List[float], minutes: float):
    xmax = max(minutes, (xs[-1] if xs else 0))
    ymin = min([max(40, a.rest_hr - 10)] + [y - 5 for y in ys])
    ymax = max([a.max_hr + 10] + [y + 5 for y in ys])
    return xmax, ymin, ymax


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class TrackrApp(tk.Tk):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.config_path = args.config or CONFIG_PATH
        cfg, cfg_msg = load_config(self.config_path)
        # command-line options override the saved config for this run
        for key in ("name", "source", "ant_id", "age", "rest_hr", "max_hr", "weight", "sex",
                    "minutes", "alert_hr"):
            if getattr(args, key, None) is not None:
                cfg[key] = getattr(args, key)
        if args.address:
            cfg["device"] = cfg["device_address"] = args.address
        self.title("TRACKR HEART RATE - Training")
        self.geometry("1320x840")
        self.minsize(1080, 820)
        self.configure(bg=BG)

        self.q: queue.Queue = queue.Queue()
        self.worker: Optional[SensorWorker] = None
        self.state = "idle"                 # idle | running | paused
        self.accum = 0.0                    # active seconds before the current run segment
        self.run_started = 0.0
        self.session_start: Optional[datetime] = None
        self.session_end: Optional[datetime] = None
        self.last_sample_mono = 0.0
        self.connected = False
        self.target_notified = False
        self.last_alert = 0.0
        self.chart_dirty = True
        self.scan_map: dict[str, str] = {}
        self.panel_width = cfg["panel_width"]
        self.auto_connect = cfg["auto_connect"]
        self.battery: Optional[int] = None
        self.battery_warned = False
        self._ready = False
        if cfg["device"] and cfg["device_address"] and cfg["device"] != cfg["device_address"]:
            self.scan_map[cfg["device"]] = cfg["device_address"]   # remembered strap

        self.detector = HeartRateDetector()
        self.analytics = TrainingAnalytics(30, 60, None, 70, True)
        self.t_min: List[float] = []
        self.hr_pts: List[float] = []
        self.z_pts: List[int] = []
        self.rows: List[list] = []
        self.pause_marks: List[float] = []
        self.gap_marks: List[tuple[float, float]] = []

        # ---- settings variables ----
        def txt(v) -> str:
            if v is None:
                return ""
            return f"{v:g}" if isinstance(v, float) else str(v)

        self.v_name = tk.StringVar(value=txt(cfg["name"]))
        self.v_source = tk.StringVar(value={v: k for k, v in SOURCES.items()}[cfg["source"]])
        self.v_device = tk.StringVar(value=txt(cfg["device"]))
        self.v_ant = tk.StringVar(value=txt(cfg["ant_id"]))
        self.v_age = tk.StringVar(value=txt(cfg["age"]))
        self.v_rest = tk.StringVar(value=txt(cfg["rest_hr"]))
        self.v_max = tk.StringVar(value=txt(cfg["max_hr"]))
        self.v_weight = tk.StringVar(value=txt(cfg["weight"]))
        self.v_sex = tk.StringVar(value="Male" if cfg["sex"] == "m" else "Female")
        self.v_minutes = tk.StringVar(value=txt(cfg["minutes"]))
        self.v_alert = tk.StringVar(value=txt(cfg["alert_hr"]))

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
        if self.scan_map:
            self.dev_cb.config(values=list(self.scan_map))
        self._status(cfg_msg)
        for ev in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
            self.dev_cb.bind(ev, lambda e: self._refresh_connection())
        self._ready = True
        if self._auto_connect_wanted():
            self.after(300, self._refresh_connection)

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
        st.configure("Sash", sashthickness=12, gripcount=0, background=BG, lightcolor=BG,
                     darkcolor=BG, bordercolor=BG)
        st.configure("TPanedwindow", background=BG)
        self.option_add("*TCombobox*Listbox.background", PANEL2)
        self.option_add("*TCombobox*Listbox.foreground", FG)

        for name, color in (("Start", "#16a34a"), ("Pause", "#d97706"),
                            ("Resume", "#2563eb"), ("Stop", "#dc2626"), ("Small", "#334155"),
                            ("Config", "#334155")):
            size, pad = {"Small": (9, (6, 3)), "Config": (10, (8, 7))}.get(name, (12, (10, 10)))
            st.configure(f"{name}.TButton", background=color, foreground="white",
                         font=(FONT, size, "bold"), padding=pad, borderwidth=0)
            st.map(f"{name}.TButton",
                   background=[("disabled", "#2a333d"), ("active", color)],
                   foreground=[("disabled", "#5b6773")])

    # ------------------------------------------------------------------ layout
    def _section(self, parent: tk.Widget, title: str) -> tk.Frame:
        tk.Label(parent, text=title.upper(), bg=PANEL, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=14, pady=(10, 3))
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="x", padx=14)
        f.columnconfigure(1, weight=1)
        return f

    def _row(self, f: tk.Frame, r: int, label: str, widget: tk.Widget) -> tk.Widget:
        tk.Label(f, text=label, bg=PANEL, fg=FG, font=(FONT, 10)).grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        widget.grid(row=r, column=1, sticky="ew", pady=3)
        return widget

    def _card(self, parent: tk.Widget, title: str) -> tuple[tk.Frame, tk.Label]:
        f = tk.Frame(parent, bg=PANEL, padx=12, pady=6)
        tk.Label(f, text=title.upper(), bg=PANEL, fg=MUTED, font=(FONT, 8, "bold"),
                 anchor="w").pack(fill="x")
        v = tk.Label(f, text="--", bg=PANEL, fg=FG, font=self.card_font, anchor="w")
        v.pack(fill="x")
        return f, v

    def _layout_cards(self, cols: int) -> None:
        for i, key in enumerate(self.card_order):
            frame = self.cards[key].master
            frame.grid(row=i // cols, column=i % cols, sticky="nsew",
                       padx=(0 if i % cols == 0 else 6, 0), pady=(0 if i < cols else 6, 0))
        for c in range(4):
            self.cards_grid.columnconfigure(c, weight=1 if c < cols else 0,
                                            uniform="c" if c < cols else "")
        for r in range(4):
            self.cards_grid.rowconfigure(r, weight=1 if r < -(-len(self.card_order) // cols) else 0)
        self.card_cols = cols

    def _fit_cards(self, event=None) -> None:
        """Card values use 70 % of the heart rate size. If the cards get too narrow they
        reflow into 3 columns (and shrink as needed) so the chart keeps its height."""
        width = self.cards_grid.winfo_width()
        if width < 50:
            return
        for cols in (4, 3):
            avail = (width - (cols - 1) * 6) / cols - 26   # one card's text width in pixels
            size = CARD_FONT
            while size > 12 and self._measure(size, "00:00") > avail:
                size -= 1
            if size >= CARD_FONT_MIN:
                break
        if cols != getattr(self, "card_cols", 4):
            self._layout_cards(cols)
        self.card_avail = avail
        if size != self.card_font.cget("size"):
            self.card_font.configure(size=size)
        for key, lbl in self.cards.items():               # re-fit long texts at the new size
            self._fit_label(lbl)

    def _measure(self, size: int, text: str) -> int:
        self._probe_font.configure(size=size)
        return self._probe_font.measure(text)

    def _fit_label(self, lbl: tk.Label) -> None:
        text = lbl.cget("text")
        base = self.card_font.cget("size")
        if self._measure(base, text) <= getattr(self, "card_avail", 10 ** 6):
            lbl.config(font=self.card_font)
            return
        size = base
        while size > 9 and self._measure(size, text) > self.card_avail:
            size -= 1
        lbl.config(font=(FONT, size, "bold"))

    def _set_card(self, key: str, text: str, fg: str = FG) -> None:
        lbl = self.cards[key]
        if lbl.cget("text") != text or lbl.cget("fg") != fg:
            lbl.config(text=text, fg=fg)
            self._fit_label(lbl)

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

        # status bar (packed first so it always keeps its place at the bottom)
        self.status_lbl = tk.Label(self, text="Choose a connection and press Start.", bg=PANEL2,
                                   fg=MUTED, anchor="w", padx=12, pady=5, font=(FONT, 9))
        self.status_lbl.pack(fill="x", side="bottom")

        # settings panel | main area, with a draggable divider
        self.paned = ttk.PanedWindow(self, orient="horizontal")
        self.paned.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        # ---------------- left panel: settings + controls
        left = tk.Frame(self.paned, bg=PANEL, width=self.panel_width)
        self.paned.add(left, weight=0)

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
        self.lockable.append(self._row(f, 0, "Name", ttk.Entry(f, textvariable=self.v_name, width=16)))
        for r, (lbl, var, lo, hi) in enumerate((("Age", self.v_age, 10, 100),
                                                 ("Resting HR", self.v_rest, 30, 120)), start=1):
            sp = ttk.Spinbox(f, from_=lo, to=hi, textvariable=var, width=8)
            self.lockable.append(self._row(f, r, lbl, sp))
        self.lockable.append(self._row(f, 3, "Max HR (blank=auto)", ttk.Entry(f, textvariable=self.v_max, width=8)))
        self.lockable.append(self._row(f, 4, "Weight (kg)", ttk.Entry(f, textvariable=self.v_weight, width=8)))
        self.lockable.append(self._row(f, 5, "Sex", ttk.Combobox(f, textvariable=self.v_sex,
                                                                 values=["Male", "Female"],
                                                                 state="readonly", width=8)))

        f = self._section(left, "Session")
        self._row(f, 0, "Training time (min)",
                  ttk.Spinbox(f, from_=1, to=600, increment=5, textvariable=self.v_minutes, width=8))
        self._row(f, 1, "HR alert (bpm)", ttk.Entry(f, textvariable=self.v_alert, width=8))

        ctl = tk.Frame(left, bg=PANEL)
        ctl.pack(fill="x", padx=14, pady=(12, 4))
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

        self.btn_config = ttk.Button(left, text="Save Config", style="Config.TButton",
                                     command=self.save_config)
        self.btn_config.pack(fill="x", padx=14, pady=(10, 2))
        tk.Label(left, text="Saved settings load automatically at startup", bg=PANEL, fg=MUTED,
                 font=(FONT, 8)).pack(anchor="w", padx=14)

        # ---------------- right panel: metrics + chart
        right = tk.Frame(self.paned, bg=BG)
        self.paned.add(right, weight=1)
        self.paned.bind("<B1-Motion>", self._on_sash_drag, add="+")
        self.paned.bind("<ButtonRelease-1>", self._on_sash_release, add="+")
        self._sash_applied = False
        self.paned.bind("<Configure>", self._apply_panel_width, add="+")

        top = tk.Frame(right, bg=BG)
        top.pack(fill="x")

        hrcard = tk.Frame(top, bg=PANEL, padx=18, pady=8, width=280, height=180)
        hrcard.pack(side="left", fill="y")
        hrcard.pack_propagate(False)
        tk.Label(hrcard, text="HEART RATE", bg=PANEL, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        hrline = tk.Frame(hrcard, bg=PANEL)
        hrline.pack(anchor="w")
        self.hr_lbl = tk.Label(hrline, text="--", bg=PANEL, fg=FG, font=(FONT, HR_FONT, "bold"))
        self.hr_lbl.pack(side="left")
        tk.Label(hrline, text="bpm", bg=PANEL, fg=MUTED, font=(FONT, 14)).pack(side="left", anchor="s", pady=(0, 16))
        self.zone_lbl = tk.Label(hrcard, text="—", bg=PANEL, fg=MUTED, font=(FONT, 12, "bold"))
        self.zone_lbl.pack(anchor="w")

        grid = tk.Frame(top, bg=BG)
        grid.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.cards_grid = grid
        self.card_font = tkfont.Font(family=FONT, size=CARD_FONT, weight="bold")
        self._probe_font = tkfont.Font(family=FONT, size=CARD_FONT, weight="bold")
        self.cards = {}
        names = [("battery", "Sensor battery"), ("remaining", "Remaining"), ("avg", "Avg HR"),
                 ("peak", "Peak HR"), ("hrv", "HRV (RMSSD)"), ("signal", "Signal"),
                 ("trimp", "Training load"), ("kcal", "Calories")]
        for key, title in names:
            self.cards[key] = self._card(grid, title)[1]
        self.card_order = [k for k, _ in names]
        self._layout_cards(4)
        grid.bind("<Configure>", self._fit_cards)

        chart_frame = tk.Frame(right, bg=PANEL)
        chart_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.fig = Figure(figsize=(8, 4), dpi=100, facecolor=PANEL)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas.get_tk_widget().configure(bg=PANEL, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)

        zf = tk.Frame(right, bg=PANEL, padx=12, pady=8)
        zf.pack(side="bottom", fill="x", pady=(8, 8), before=chart_frame)   # keep its height; chart flexes
        tk.Label(zf, text="TIME IN ZONE", bg=PANEL, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        self.zone_canvas = tk.Canvas(zf, bg=PANEL, height=38, highlightthickness=0)
        self.zone_canvas.pack(fill="x", pady=(4, 0))
        self.zone_canvas.bind("<Configure>", lambda e: self._draw_zone_bar())

    # ------------------------------------------------------------------ panel width
    def _apply_panel_width(self, event=None) -> None:
        """Set the saved panel width once the window has its real size."""
        if self._sash_applied or self.paned.winfo_width() < self.panel_width + 200:
            return
        self._sash_applied = True
        self.paned.sashpos(0, self.panel_width)

    def _on_sash_drag(self, event=None) -> None:
        try:
            pos = self.paned.sashpos(0)
        except tk.TclError:
            return
        clamped = min(PANEL_MAX, max(PANEL_MIN, pos))
        if clamped != pos:
            self.paned.sashpos(0, clamped)

    def _on_sash_release(self, event=None) -> None:
        self._on_sash_drag()
        try:
            pos = self.paned.sashpos(0)
        except tk.TclError:
            return
        if pos != self.panel_width:
            self.panel_width = pos
            self._status(f"Settings panel width {pos} px. Press Save Config to keep it.")

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
        if self._ready:
            self._refresh_connection()

    def _ble_address(self) -> Optional[str]:
        dev = self.v_device.get().strip()
        return self.scan_map.get(dev, dev) or None

    def _auto_connect_wanted(self) -> bool:
        """A Bluetooth strap is assigned: keep it connected even between sessions."""
        return (self.auto_connect and SOURCES.get(self.v_source.get()) == "ble"
                and self._ble_address() is not None)

    def _connect_sensor(self, ant_id: int = 0) -> None:
        if self.worker is not None:
            return
        src = SOURCES[self.v_source.get()]
        address = self._ble_address() if src == "ble" else None
        self.detector = HeartRateDetector()          # new connection: check R-R data again
        self.worker = SensorWorker(src, address, ant_id, self.q)
        self.worker.start()
        self._status(f"Connecting to {self.v_device.get().strip()}..." if src == "ble" and address
                     else "Connecting to sensor...")

    def _refresh_connection(self) -> None:
        """While idle: (re)connect to the assigned Bluetooth strap, or disconnect if none."""
        if self.state != "idle" or not self._ready:
            return
        w = self.worker
        if w is not None:
            same = (w.source == SOURCES.get(self.v_source.get()) and
                    (w.source != "ble" or w.address == self._ble_address()))
            if same and (self._auto_connect_wanted() or w.source != "ble"):
                return
            self._disconnect()
        if self._auto_connect_wanted():
            self._connect_sensor()

    def _on_minutes_changed(self) -> None:
        self.target_notified = self._elapsed() >= self._minutes() * 60
        self.chart_dirty = True

    # ------------------------------------------------------------------ chart
    def _setup_chart(self) -> None:
        ax, a = self.ax, self.analytics
        ax.clear()
        self.fig.subplots_adjust(left=0.06, right=0.88, top=0.93, bottom=0.12)
        style_hr_axes(ax, a)
        self.chart_title = ax.set_title("", color=FG, fontsize=10, loc="left")
        self.lc = LineCollection([], linewidths=2.4, capstyle="round")
        ax.add_collection(self.lc)
        (self.dot,) = ax.plot([], [], "o", ms=8, color="white", mec=ACCENT, mew=2, zorder=5)
        self.target_line = ax.axvline(self._minutes(), color=FG, ls="--", lw=1, alpha=0.5)
        for m in self.pause_marks:
            ax.axvline(m, color=MUTED, ls=":", lw=1)
        for x0, x1 in self.gap_marks:
            draw_gap(ax, x0, x1)
        self.chart_dirty = True
        self._redraw_chart(schedule=False)

    def _redraw_chart(self, schedule: bool = True) -> None:
        try:
            if self.chart_dirty:
                self.chart_dirty = False
                a, minutes = self.analytics, self._minutes()
                xs, ys = self.t_min, self.hr_pts
                segs, colors = hr_segments(xs, ys, self.z_pts)
                self.lc.set_segments(segs)
                if colors:
                    self.lc.set_colors(colors)
                self.dot.set_data(xs[-1:], ys[-1:])
                xmax, ymin, ymax = hr_limits(a, xs, ys, minutes)
                self.ax.set_xlim(0, xmax)
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

        self.t_min, self.hr_pts, self.z_pts, self.rows, self.pause_marks = [], [], [], [], []
        self.gap_marks = []
        self.accum, self.run_started = 0.0, time.monotonic()
        self.session_start = datetime.now()
        self.target_notified = False
        self.state = "running"
        self._setup_chart()
        self._draw_zone_bar()
        self._lock_settings(True)
        self._set_buttons()

        connected = self.worker is not None
        self._connect_sensor(ant_id)
        self._status("Training started. Good luck!" if connected else
                     "Training started. Connecting to sensor...")

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
        self.session_end = datetime.now()
        self.state = "idle"
        if not self._auto_connect_wanted():
            self._disconnect()                       # assigned BLE strap stays connected
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
        for msg in self.detector.validator.pop_messages():
            self._status(msg)

        if self.state == "running":
            self.analytics.update(raw.timestamp, hr, zone)
            el = self._elapsed()
            if hr > 0:
                if self.t_min and el / 60.0 - self.t_min[-1] >= GAP_MARK_MIN:
                    gap = (self.t_min[-1], el / 60.0)
                    self.gap_marks.append(gap)
                    draw_gap(self.ax, *gap)
                self.t_min.append(el / 60.0)
                self.hr_pts.append(hr)
                self.z_pts.append(zone)
                self.chart_dirty = True
            self.rows.append([
                datetime.fromtimestamp(raw.timestamp).isoformat(timespec="milliseconds"),
                f"{el:.1f}", f"{hr:.1f}", raw.device_hr, conf,
                f"{rmssd:.1f}" if rmssd else "", zone,
                f"{self.analytics.trimp:.2f}", f"{self.analytics.kcal:.2f}",
                " ".join(f"{r:.0f}" for r in acc), rej, raw.contact,
                " ".join(f"{r:.0f}" for r in self.detector.last_rr),
                self.detector.rr_status, self.detector.validator.scale])
            self._draw_zone_bar()

        # live display (also while paused)
        alert = self._alert_hr()
        over = alert is not None and hr >= alert
        self.hr_lbl.config(text=f"{hr:.0f}" if hr > 0 else "--",
                           fg=ACCENT if over else ZONE_HEX[zone])
        self.zone_lbl.config(text=f"{zname}  ·  {100 * hr / self.analytics.max_hr:.0f}% max",
                             fg=ZONE_HEX[zone])
        if raw.contact is False:
            self._set_card("signal", "No contact", ACCENT)
        else:
            col = "#22c55e" if conf >= 0.7 else "#f59e0b" if conf >= 0.4 else ACCENT
            self._set_card("signal", f"{conf * 100:.0f}%", col)
        st = self.detector.rr_status
        self._set_card("hrv", f"{rmssd:.0f} ms" if rmssd else
                       "n/a" if st in ("synthetic", "no_rr") else "--",
                       MUTED if st in ("synthetic", "no_rr") else FG)
        a = self.analytics
        self._set_card("avg", f"{a.avg_hr:.0f}" if a.hr_n else "--")
        self._set_card("peak", f"{a.peak_hr:.0f}" if a.hr_n else "--")
        self._set_card("trimp", f"{a.trimp:.1f}")
        self._set_card("kcal", f"{a.kcal:.0f}")
        if raw.battery is not None:
            self._set_battery(raw.battery)
        if over and time.monotonic() - self.last_alert > 5:
            self.last_alert = time.monotonic()
            self.bell()
            self._status(f"⚠ Heart rate above your {alert} bpm alert!")

    def _set_battery(self, pct: Optional[int]) -> None:
        self.battery = pct
        if pct is None:
            self._set_card("battery", "--", FG)
            return
        col = "#22c55e" if pct > 50 else "#f59e0b" if pct > 20 else ACCENT
        self._set_card("battery", f"{pct}%", col)
        if pct <= 15 and not self.battery_warned:
            self.battery_warned = True
            self._status(f"⚠ Heart rate strap battery is low ({pct}%). Replace or charge it soon.")

    def _alert_hr(self) -> Optional[int]:
        try:
            return self._int(self.v_alert, None, 60, 250)
        except ValueError:
            return None

    def _tick(self) -> None:
        el = self._elapsed()
        total = self._minutes() * 60
        if self.state != "idle":                        # elapsed time shown in the header badge
            badge = ("● RECORDING" if self.state == "running" else "❚❚ PAUSED") + f"   {fmt_time(el)}"
            if self.state_lbl.cget("text") != badge:
                self.state_lbl.config(text=badge)
        rem = total - el
        self._set_card("remaining", fmt_time(rem) if rem > 0 else f"+{fmt_time(-rem)}",
                       FG if rem > 0 else "#22c55e")
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
            self._set_battery(None)

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
        who = safe_filename(self.v_name.get().strip())
        base = os.path.join("trackr_sessions", f"session_{stamp}" + (f"_{who}" if who else ""))
        with open(base + ".csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "elapsed_s", "hr_filtered", "hr_device", "confidence", "rmssd_ms",
                        "zone", "trimp", "kcal", "rr_accepted_ms", "rr_rejected", "contact",
                        "rr_received_ms", "rr_status", "rr_scale"])
            w.writerows(self.rows)
        try:
            self._render_report(base + ".png")
        except Exception as exc:               # never lose the CSV because of the image
            messagebox.showwarning("Session report", f"The CSV was saved, but the report image "
                                   f"could not be created:\n{exc}")

        total = sum(a.zone_time.values()) or 1
        zones = "\n".join(f"   {name:<13} {lo}-{hi} bpm   {fmt_time(a.zone_time[z])}  "
                          f"({100 * a.zone_time[z] / total:.0f}%)"
                          for (name, lo, hi), z in zip(a.zone_bounds(), range(1, 6)))
        name = self.v_name.get().strip()
        summary = ((f"Athlete:   {name}\n" if name else "") +
                   f"Duration:   {fmt_time(el)}  (target {self._minutes():g} min)\n"
                   f"Avg / Peak HR:   {a.avg_hr:.0f} / {a.peak_hr:.0f} bpm\n"
                   f"Training load (TRIMP):   {a.trimp:.1f}\n"
                   f"Calories:   {a.kcal:.0f} kcal\n\nTime in zone:\n{zones}\n\n"
                   f"Saved:\n   {os.path.abspath(base)}.csv\n   {os.path.abspath(base)}.png")
        self._status(f"Session saved to {os.path.abspath(base)}.csv")
        messagebox.showinfo("Training summary", summary)

    def _render_report(self, path: str) -> None:
        """Session report image: athlete, date/time, summary, HR chart and time in zone %."""
        a, minutes = self.analytics, self._minutes()
        start = self.session_start or datetime.now()
        end = self.session_end or datetime.now()
        name = self.v_name.get().strip() or "—"
        if len(name) > 26:
            name = name[:25] + "…"
        sex = self.v_sex.get()
        try:
            weight = f"{float(self.v_weight.get()):g} kg"
        except ValueError:
            weight = "—"
        same_day = start.date() == end.date()
        when = (f"{start:%d %b %Y}  ·  {start:%H:%M}–{end:%H:%M}" if same_day else
                f"{start:%d %b %Y %H:%M} – {end:%d %b %H:%M}")

        fig = Figure(figsize=(13, 8.6), dpi=130, facecolor=BG)
        FigureCanvasAgg(fig)
        gs = fig.add_gridspec(3, 1, height_ratios=[1.55, 4.3, 1.0], hspace=0.30,
                              left=0.065, right=0.865, top=0.93, bottom=0.05)

        # ---- title
        fig.text(0.065, 0.965, "♥ TRACKR", color=ACCENT, fontsize=17, fontweight="bold", va="center")
        fig.text(0.175, 0.965, "HEART RATE  ·  Training Session Report", color=FG, fontsize=14,
                 va="center")
        fig.text(0.865, 0.965, f"Resting HR {a.rest_hr}  ·  Max HR {a.max_hr} bpm", color=MUTED,
                 fontsize=10, va="center", ha="right")

        # ---- athlete + summary cards
        hx = fig.add_subplot(gs[0])
        hx.set_axis_off()
        hx.set_xlim(0, 1)
        hx.set_ylim(0, 1)
        cols = [0.0, 0.31, 0.43, 0.555, 0.695]
        widths = [0.30, 0.11, 0.115, 0.13, 0.305]
        rows = [
            [("NAME", name), ("AGE", self.v_age.get() or "—"), ("SEX", sex), ("WEIGHT", weight),
             ("DATE / TIME", when)],
            [("DURATION", fmt_time(self.accum)), ("AVG HR", f"{a.avg_hr:.0f} bpm"),
             ("PEAK HR", f"{a.peak_hr:.0f} bpm"), ("LOAD (TRIMP)", f"{a.trimp:.1f}"),
             ("CALORIES", f"{a.kcal:.0f} kcal")],
        ]
        from matplotlib.patches import FancyBboxPatch
        for r, row in enumerate(rows):
            y0 = 0.54 if r == 0 else 0.0
            for (label, value), x, w in zip(row, cols, widths):
                hx.add_patch(FancyBboxPatch((x, y0), w - 0.01, 0.44, boxstyle="round,pad=0,rounding_size=0.015",
                                            fc=PANEL, ec="none", transform=hx.transAxes, clip_on=False))
                hx.text(x + 0.012, y0 + 0.33, label, color=MUTED, fontsize=8, fontweight="bold",
                        va="center", transform=hx.transAxes)
                hx.text(x + 0.012, y0 + 0.14, value, color=FG, fontsize=13 if label != "DATE / TIME" else 12,
                        fontweight="bold", va="center", transform=hx.transAxes)

        # ---- heart rate chart
        ax = fig.add_subplot(gs[1])
        style_hr_axes(ax, a, zone_fontsize=8.5)
        ax.set_title(f"Heart rate · training time {minutes:g} min", color=FG, fontsize=10, loc="left")
        segs, colors = hr_segments(self.t_min, self.hr_pts, self.z_pts)
        if len(segs):
            ax.add_collection(LineCollection(segs, colors=colors, linewidths=2.0, capstyle="round"))
        ax.axvline(minutes, color=FG, ls="--", lw=1, alpha=0.5)
        for m in self.pause_marks:
            ax.axvline(m, color=MUTED, ls=":", lw=1)
        for x0, x1 in self.gap_marks:
            draw_gap(ax, x0, x1)
        xmax, ymin, ymax = hr_limits(a, self.t_min, self.hr_pts, minutes)
        ax.set_xlim(0, xmax)
        ax.set_ylim(ymin, ymax)
        if self.hr_pts:
            i = int(np.argmax(self.hr_pts))
            ax.plot([self.t_min[i]], [self.hr_pts[i]], "o", ms=6, color="white", mec=ACCENT, mew=1.5, zorder=5)
            ax.annotate(f"Peak {self.hr_pts[i]:.0f}", (self.t_min[i], self.hr_pts[i]),
                        xytext=(6, 6), textcoords="offset points", color=FG, fontsize=8)

        # ---- time in zone %
        zx = fig.add_subplot(gs[2])
        zx.set_axis_off()
        zx.set_xlim(0, 1)
        zx.set_ylim(0, 1)
        zx.text(0, 1.02, "TIME IN ZONE", color=MUTED, fontsize=9, fontweight="bold", va="bottom",
                transform=zx.transAxes)
        total = sum(a.zone_time.values())
        x = 0.0
        if total <= 0:
            zx.barh(0.72, 1.0, height=0.32, left=0, color=PANEL2)
        for z in range(1, 6):
            t = a.zone_time[z]
            if total > 0 and t > 0:
                w = t / total
                zx.barh(0.72, w, height=0.32, left=x, color=ZONE_HEX[z])
                if w >= 0.06:
                    zx.text(x + w / 2, 0.72, f"{100 * w:.0f}%", color=BG, fontsize=10,
                            fontweight="bold", ha="center", va="center")
                x += w
        for (zname, lo, hi), z in zip(a.zone_bounds(), range(1, 6)):
            t = a.zone_time[z]
            pct = 100 * t / total if total else 0
            cx = (z - 0.5) / 5
            zx.text(cx, 0.30, f"{zname}  {pct:.0f}%", color=ZONE_HEX[z], fontsize=10,
                    fontweight="bold", ha="center", va="center")
            zx.text(cx, 0.06, f"{fmt_time(t)}  ·  {lo}–{hi} bpm", color=MUTED, fontsize=8.5,
                    ha="center", va="center")

        fig.savefig(path, facecolor=BG)

    # ------------------------------------------------------------------ config
    def save_config(self) -> None:
        if self.state == "idle" and not self._apply_athlete(preview=False):
            return
        try:
            ant_id = self._int(self.v_ant, 0, 0, 65535) or 0
            alert = self._int(self.v_alert, None, 60, 250)
            minutes = float(self.v_minutes.get())
            if not 1 <= minutes <= 600:
                raise ValueError
        except ValueError:
            messagebox.showerror("Save Config", "Please check the settings:\n"
                                 "ANT+ ID 0-65535, HR alert 60-250 bpm (or blank), "
                                 "training time 1-600 min.")
            return
        dev = self.v_device.get().strip()
        cfg = {
            "name": self.v_name.get().strip(),
            "source": SOURCES.get(self.v_source.get(), "ble"),
            "device": dev,
            "device_address": self.scan_map.get(dev, dev),
            "ant_id": ant_id,
            "age": self._int(self.v_age, 30, 10, 100),
            "rest_hr": self._int(self.v_rest, 60, 30, 120),
            "max_hr": self._int(self.v_max, None, 120, 230),
            "weight": float(self.v_weight.get() or 70),
            "sex": "m" if self.v_sex.get() == "Male" else "f",
            "minutes": int(minutes) if minutes == int(minutes) else minutes,
            "alert_hr": alert,
            "panel_width": self.panel_width,
            "auto_connect": self.auto_connect,
        }
        try:
            write_config(self.config_path, cfg)
        except OSError as exc:
            messagebox.showerror("Save Config", f"Could not save the config:\n{exc}")
            return
        self._status(f"Config saved to {self.config_path}. It will load automatically next time.")
        self.btn_config.config(text="✓  Saved")
        self.after(1800, lambda: self.btn_config.config(text="Save Config"))

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
    ap = argparse.ArgumentParser(
        description="TRACKR HEART RATE training app (BLE + ANT+). "
                    "Options override the saved config for this run only.")
    ap.add_argument("--config", help=f"config file to load/save (default {CONFIG_PATH})")
    ap.add_argument("--name", help="athlete name")
    ap.add_argument("--source", choices=["ble", "ant", "sim"])
    ap.add_argument("--address", help="BLE address/UUID of the strap (auto-detect if omitted)")
    ap.add_argument("--ant-id", type=int, help="ANT+ device id (0 = any)")
    ap.add_argument("--minutes", type=float,
                    help="training time / chart display period in minutes (default 50)")
    ap.add_argument("--age", type=int)
    ap.add_argument("--rest-hr", type=int)
    ap.add_argument("--max-hr", type=int, help="override estimated max HR")
    ap.add_argument("--weight", type=float, help="kg")
    ap.add_argument("--sex", choices=["m", "f"])
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
