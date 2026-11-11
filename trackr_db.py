#!/usr/bin/env python3
"""
TRACKR HEART RATE - Session database
====================================

Combines the training session CSV files written by trackr_hr.py into one
SQLite database, as the data layer for training performance tracking.

* Imports single files or whole folders (session_*.csv), safely re-runnable:
  files already imported are skipped (detected by content hash)
* Older session files recorded before the R-R data check update are checked
  for the strap's millisecond R-R units / non-beat-to-beat R-R data and
  corrected (heart rate taken from the strap)
* Summary metrics are calculated the same way for every session, from the
  athlete's profile: avg / peak HR, time in zones, TRIMP, calories, signal gaps
* Heart rate recovery (HRR60) with its peak HR, flagged as a valid recovery test
  only after a sustained hard effort followed by easing off, so the trend
  compares like with like
* Every sample is stored too, so metrics can be recalculated later
  (for example after updating resting or max HR)
* Ready-made views for weekly totals and a fitness / fatigue (CTL / ATL / TSB)
  report

Only the Python standard library is needed.

Usage
-----
    python trackr_db.py import trackr_sessions/              # all session CSVs in a folder
    python trackr_db.py import a.csv b.csv --athlete "Somchai Jaidee"
    python trackr_db.py athlete "Theerawat Ramchuen" --age 62 --rest-hr 52 --weight 70 --sex m
    python trackr_db.py recalc --athlete "Theerawat Ramchuen"  # after changing the profile
    python trackr_db.py list
    python trackr_db.py weekly
    python trackr_db.py hrr                                   # heart rate recovery tests
    python trackr_db.py fitness --days 28
    python trackr_db.py remove 12                             # delete session id 12

The database file defaults to trackr.db in the current folder (--db to change).
Athlete profiles are read from the app's saved config (~/.trackr_hr/config.json)
when the name matches, unless set with the "athlete" command.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional

SCHEMA_VERSION = 2
DEFAULT_DB = "trackr.db"
APP_CONFIG = os.path.join(os.path.expanduser("~"), ".trackr_hr", "config.json")

# Same rules as trackr_hr.py so the numbers match the app
ZONE_THRESHOLDS = (0.6, 0.7, 0.8, 0.9)        # heart-rate-reserve fraction for Z2..Z5
MAX_DT_S = 5.0                                # longer gaps count as 5 s (as in the app)
GAP_S = 5.0                                   # a gap longer than this is "no signal"
UNIT_TOL = 0.008                              # R-R unit check tolerance (0.8 %)

# Heart rate recovery (HRR60) test rules
HRR_PEAK_FRAC = 0.8          # peak must reach Zone 4 (80 % of heart rate reserve)
HRR_EFFORT_FRAC = 0.7        # the 2 minutes before the peak must average Zone 3 or more
HRR_EFFORT_S = 120
HRR_DROP30_MIN = 5           # HR must fall >= 5 bpm in the first 30 s, and keep falling to 60 s
HRR_DROP120_MIN = 2          # ... and, if recorded, fall >= 2 bpm more between 60 and 120 s
HRR_LOCAL_S = 60             # a peak is the highest point within +/- 60 s
HRR_PLATEAU_BPM = 2          # recovery is measured from the end of the peak plateau

REQUIRED_COLUMNS = {"time", "elapsed_s", "hr_filtered", "hr_device"}
SAMPLE_COLUMNS = ["time", "elapsed_s", "hr_filtered", "hr_device", "confidence", "rmssd_ms",
                  "zone", "trimp", "kcal", "rr_accepted_ms", "rr_rejected", "contact",
                  "rr_received_ms", "rr_status", "rr_scale"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS athletes (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    age         INTEGER,
    sex         TEXT CHECK (sex IN ('m', 'f')),
    weight_kg   REAL,
    rest_hr     INTEGER,
    max_hr      INTEGER,               -- NULL = estimated from age (Tanaka)
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY,
    athlete_id      INTEGER REFERENCES athletes(id) ON DELETE SET NULL,
    source_file     TEXT NOT NULL,      -- file name as imported
    file_sha256     TEXT NOT NULL UNIQUE,
    start_time      TEXT NOT NULL,      -- local time, ISO 8601
    end_time        TEXT NOT NULL,
    session_date    TEXT NOT NULL,      -- YYYY-MM-DD (local)
    duration_s      REAL NOT NULL,      -- active training time (pauses excluded)
    samples         INTEGER NOT NULL,
    hr_source       TEXT NOT NULL,      -- 'app' (filtered HR) or 'strap' (strap HR used)
    rr_status       TEXT,               -- ok / synthetic / no_rr / checking / unknown
    rr_scale        REAL,               -- 1.0 or 1.024 (strap sent milliseconds)
    corrected       INTEGER NOT NULL DEFAULT 0,  -- 1 = old file corrected on import
    avg_hr          REAL,
    max_hr_reached  REAL,
    min_hr          REAL,
    trimp           REAL,
    kcal            REAL,
    z1_s REAL, z2_s REAL, z3_s REAL, z4_s REAL, z5_s REAL,
    peak_elapsed_s  REAL,               -- time of the peak used for HRR60
    hrr60_bpm       REAL,               -- HR drop 60 s after that peak (NULL if not measurable)
    hrr_peak_hr     REAL,               -- peak HR (10-sample average) the drop is measured from
    hrr_valid       INTEGER,            -- 1 = valid recovery test, 0 = not, NULL = no profile
    hrr_note        TEXT,               -- why it is not a valid test
    gaps            INTEGER,            -- number of data gaps > {GAP_S:g} s
    gap_s           REAL,               -- total seconds without data
    avg_confidence  REAL,
    -- profile used for the metrics above (snapshot)
    profile_rest_hr INTEGER,
    profile_max_hr  INTEGER,
    profile_age     INTEGER,
    profile_weight  REAL,
    profile_sex     TEXT,
    metrics_source  TEXT NOT NULL,      -- 'profile' (recalculated) or 'csv' (as recorded by app)
    notes           TEXT,
    imported_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_athlete_date ON sessions(athlete_id, session_date);

CREATE TABLE IF NOT EXISTS samples (
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    time            TEXT,
    elapsed_s       REAL NOT NULL,
    hr              REAL,               -- heart rate used for all metrics
    hr_filtered     REAL,
    hr_device       INTEGER,
    confidence      REAL,
    rmssd_ms        REAL,               -- NULL when not valid
    zone            INTEGER,            -- zone of hr with the session's profile
    rr_accepted_ms  TEXT,
    rr_rejected     INTEGER,
    contact         INTEGER,            -- 1 / 0 / NULL
    rr_received_ms  TEXT,
    PRIMARY KEY (session_id, seq)
) WITHOUT ROWID;

CREATE VIEW IF NOT EXISTS v_sessions AS
SELECT s.id, a.name AS athlete, s.session_date, s.start_time, s.end_time,
       ROUND(s.duration_s / 60.0, 1) AS minutes, ROUND(s.avg_hr) AS avg_hr,
       ROUND(s.max_hr_reached) AS max_hr, ROUND(s.trimp, 1) AS trimp, ROUND(s.kcal) AS kcal,
       ROUND(100.0 * s.z1_s / NULLIF(s.z1_s + s.z2_s + s.z3_s + s.z4_s + s.z5_s, 0)) AS z1_pct,
       ROUND(100.0 * s.z2_s / NULLIF(s.z1_s + s.z2_s + s.z3_s + s.z4_s + s.z5_s, 0)) AS z2_pct,
       ROUND(100.0 * s.z3_s / NULLIF(s.z1_s + s.z2_s + s.z3_s + s.z4_s + s.z5_s, 0)) AS z3_pct,
       ROUND(100.0 * s.z4_s / NULLIF(s.z1_s + s.z2_s + s.z3_s + s.z4_s + s.z5_s, 0)) AS z4_pct,
       ROUND(100.0 * s.z5_s / NULLIF(s.z1_s + s.z2_s + s.z3_s + s.z4_s + s.z5_s, 0)) AS z5_pct,
       s.hrr60_bpm, ROUND(s.hrr_peak_hr) AS hrr_peak_hr, s.hrr_valid, s.hrr_note,
       s.gaps, ROUND(s.gap_s) AS gap_s, s.hr_source, s.corrected, s.metrics_source,
       s.source_file
FROM sessions s LEFT JOIN athletes a ON a.id = s.athlete_id;

CREATE VIEW IF NOT EXISTS v_weekly AS
SELECT a.name AS athlete,
       strftime('%Y-W%W', s.session_date) AS week,
       MIN(s.session_date) AS first_day,
       COUNT(*) AS sessions,
       ROUND(SUM(s.duration_s) / 60.0) AS minutes,
       ROUND(SUM(s.trimp), 1) AS trimp,
       ROUND(SUM(s.kcal)) AS kcal,
       ROUND(SUM(s.avg_hr * s.duration_s) / SUM(s.duration_s)) AS avg_hr,
       ROUND(MAX(s.max_hr_reached)) AS max_hr,
       ROUND(100.0 * SUM(s.z1_s + s.z2_s) / NULLIF(SUM(s.z1_s + s.z2_s + s.z3_s + s.z4_s + s.z5_s), 0))
           AS easy_pct,                -- Z1 + Z2 share (aim ~80 %)
       ROUND(100.0 * SUM(s.z4_s + s.z5_s) / NULLIF(SUM(s.z1_s + s.z2_s + s.z3_s + s.z4_s + s.z5_s), 0))
           AS hard_pct,
       -- heart rate recovery: valid recovery tests only
       ROUND(AVG(CASE WHEN s.hrr_valid = 1 THEN s.hrr60_bpm END), 1) AS avg_hrr60,
       ROUND(AVG(CASE WHEN s.hrr_valid = 1 THEN s.hrr_peak_hr END)) AS avg_hrr_peak,
       SUM(CASE WHEN s.hrr_valid = 1 THEN 1 ELSE 0 END) AS hrr_tests
FROM sessions s LEFT JOIN athletes a ON a.id = s.athlete_id
GROUP BY a.name, week;
"""


# ==========================================================================
# Athlete profile and metrics (same formulas as trackr_hr.py)
# ==========================================================================
@dataclass
class Profile:
    age: Optional[int] = None
    sex: Optional[str] = None
    weight_kg: Optional[float] = None
    rest_hr: Optional[int] = None
    max_hr: Optional[int] = None

    @property
    def complete(self) -> bool:
        return None not in (self.age, self.sex, self.weight_kg, self.rest_hr)

    @property
    def effective_max(self) -> int:
        return self.max_hr or round(208 - 0.7 * (self.age or 30))   # Tanaka

    def describe(self) -> str:
        if not self.complete:
            return "incomplete profile"
        mx = f"{self.max_hr}" if self.max_hr else f"{self.effective_max} (est.)"
        return (f"age {self.age}, {'male' if self.sex == 'm' else 'female'}, {self.weight_kg:g} kg, "
                f"rest HR {self.rest_hr}, max HR {mx}")


def zone_of(hr: float, p: Profile) -> int:
    frac = (hr - p.rest_hr) / max(1, p.effective_max - p.rest_hr)
    return 1 + sum(frac >= t for t in ZONE_THRESHOLDS)


def compute_metrics(elapsed: List[float], hr: List[float], p: Optional[Profile]) -> dict:
    """Summary metrics from a heart rate series. Zones/TRIMP/kcal need a complete profile."""
    valid = [h for h in hr if h and h > 0]
    m = dict(avg_hr=statistics.mean(valid) if valid else None,
             max_hr_reached=max(valid) if valid else None,
             min_hr=min(valid) if valid else None)

    gaps, gap_s = 0, 0.0
    for a, b in zip(elapsed, elapsed[1:]):
        if b - a > GAP_S:
            gaps += 1
            gap_s += b - a
    m.update(gaps=gaps, gap_s=gap_s)

    has_zones = p is not None and p.rest_hr is not None and (p.max_hr or p.age)
    m.update(compute_hrr(elapsed, hr, p.rest_hr if has_zones else None,
                         p.effective_max if has_zones else None))

    if p and p.complete:
        zt = [0.0] * 5
        trimp = kcal = 0.0
        b = 1.92 if p.sex == "m" else 1.67
        for i in range(1, len(elapsed)):
            h = hr[i]
            dt = min(MAX_DT_S, elapsed[i] - elapsed[i - 1])
            if not h or h <= 0 or dt <= 0:
                continue
            zt[zone_of(h, p) - 1] += dt
            frac = max(0.0, (h - p.rest_hr) / max(1, p.effective_max - p.rest_hr))
            trimp += dt / 60 * frac * 0.64 * math.exp(b * frac)
            if p.sex == "m":       # Keytel et al. (2005)
                kpm = (-55.0969 + 0.6309 * h + 0.1988 * p.weight_kg + 0.2017 * p.age) / 4.184
            else:
                kpm = (-20.4022 + 0.4472 * h - 0.1263 * p.weight_kg + 0.074 * p.age) / 4.184
            kcal += max(0.0, kpm) * dt / 60
        m.update(trimp=trimp, kcal=kcal, **{f"z{i + 1}_s": zt[i] for i in range(5)},
                 metrics_source="profile")
    return m


def compute_hrr(elapsed: List[float], hr: List[Optional[float]],
                rest_hr: Optional[float], max_hr: Optional[float]) -> dict:
    """
    Heart rate recovery: HR drop in the 60 s after a peak (peaks use a 10-sample average
    so single spikes are ignored). When HR holds flat at the top of an effort, the drop is
    measured from the end of that plateau (within 2 bpm of the peak), where recovery starts.

    A peak counts as a valid recovery test when
      1. it reaches Zone 4 (80 % of heart rate reserve),
      2. the 2 minutes before it average Zone 3 or more (a sustained hard effort), and
      3. HR falls at least 5 bpm in the first 30 s and keeps falling to 60 s, and (if the
         second minute was recorded) still falls between 60 and 120 s - a real cool-down,
    with no signal gap in between. Every peak in the session is checked, and the highest
    valid one is used, so a hard final effort with a cool-down counts even when the
    session's overall maximum came earlier. If none is valid, the session's highest peak
    is reported with hrr_valid = 0 and the reason in hrr_note.
    """
    out = dict(peak_elapsed_s=None, hrr60_bpm=None, hrr_peak_hr=None, hrr_valid=None, hrr_note=None)
    sm = rolling_mean(hr, 10)
    n = len(sm)
    if n < 2 or max(sm) <= 0:
        return out

    def idx_at(t: float, start: int) -> Optional[int]:
        k = start
        while k < n and elapsed[k] < t:
            k += 1
        return k if k < n and elapsed[k] - t <= 3 else None

    def no_gap(i: int, j: int) -> bool:
        return all(elapsed[k + 1] - elapsed[k] <= GAP_S for k in range(i, j))

    def effort_end(ip: int) -> int:
        """Move from a peak to the end of its plateau (HR within 2 bpm of the peak), where
        recovery really starts - HR often holds flat during a 2-3 minute hard effort."""
        k = ip
        while k + 1 < n and sm[k + 1] >= sm[ip] - HRR_PLATEAU_BPM and \
                elapsed[k + 1] - elapsed[ip] <= 180 and elapsed[k + 1] - elapsed[k] <= GAP_S:
            k += 1
        return k

    def evaluate(ip: int) -> Optional[tuple[float, Optional[int], Optional[str]]]:
        """(drop, valid, reason) or None if the 60 s after the peak were not recorded."""
        j60 = idx_at(elapsed[ip] + 60, ip)
        if j60 is None or not no_gap(ip, j60):
            return None
        drop = round(sm[ip] - sm[j60], 1)
        if rest_hr is None or max_hr is None:
            return drop, None, "no athlete profile, so the test rules cannot be checked"
        reserve = max(1.0, max_hr - rest_hr)
        if sm[ip] < rest_hr + HRR_PEAK_FRAC * reserve:
            return drop, 0, f"peak {sm[ip]:.0f} bpm is below Zone 4 ({rest_hr + HRR_PEAK_FRAC * reserve:.0f})"
        i0 = idx_at(elapsed[ip] - HRR_EFFORT_S, 0) if elapsed[ip] >= HRR_EFFORT_S else None
        effort = [h for h in hr[i0:ip + 1] if h and h > 0] if i0 is not None else []
        if i0 is None or not no_gap(i0, ip) or not effort or \
                statistics.mean(effort) < rest_hr + HRR_EFFORT_FRAC * reserve:
            return drop, 0, "no sustained hard effort (2 min in Zone 3+) before the peak"
        j30 = idx_at(elapsed[ip] + 30, ip)
        if j30 is None or sm[ip] - sm[j30] < HRR_DROP30_MIN or sm[j60] >= sm[j30]:
            return drop, 0, "heart rate did not keep falling after the peak (still exercising)"
        # if the second minute was recorded, HR must still be falling (not settling at a tempo)
        j120 = idx_at(elapsed[ip] + 120, j60)
        if j120 is not None and no_gap(j60, j120) and sm[j120] > sm[j60] - HRR_DROP120_MIN:
            return drop, 0, "heart rate levelled off after 1 minute (eased to a steady pace, not a cool-down)"
        return drop, 1, None

    # candidate peaks: highest point within +/- 60 s
    cands = []
    lo = hi = 0
    for i in range(n):
        while elapsed[lo] < elapsed[i] - HRR_LOCAL_S:
            lo += 1
        while hi + 1 < n and elapsed[hi + 1] <= elapsed[i] + HRR_LOCAL_S:
            hi += 1
        if sm[i] > 0 and sm[i] >= max(sm[lo:hi + 1]) and (not cands or elapsed[i] - elapsed[cands[-1]] > HRR_LOCAL_S):
            cands.append(i)

    cands = sorted({effort_end(i) for i in cands})
    best = None                               # highest valid test
    for ip in cands:
        r = evaluate(ip)
        if r and r[1] == 1 and (best is None or sm[ip] >= sm[best[0]]):
            best = (ip, r)
    if best is None:                          # report the overall peak, with the reason
        ip = effort_end(max(range(n), key=lambda i: sm[i]))
        r = evaluate(ip)
        if r is None:
            r = (None, 0 if rest_hr is not None else None,
                 "the 60 s after the peak were not recorded (session ended or signal lost)")
        best = (ip, r)
    ip, (drop, valid, note) = best
    out.update(peak_elapsed_s=elapsed[ip], hrr60_bpm=drop, hrr_peak_hr=round(sm[ip], 1),
               hrr_valid=valid, hrr_note=note)
    return out


def rolling_mean(xs: List[Optional[float]], n: int) -> List[float]:
    """Trailing average over the last n valid values (0 where there is none yet)."""
    out: List[float] = []
    window: List[Optional[float]] = []
    for x in xs:
        window.append(x if x and x > 0 else None)
        if len(window) > n:
            window.pop(0)
        good = [v for v in window if v is not None]
        out.append(sum(good) / len(good) if good else 0.0)
    return out


# ==========================================================================
# CSV reading and R-R data check for old files
# ==========================================================================
def fnum(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def check_old_rr(rows: List[dict]) -> tuple[float, str]:
    """For files recorded before the app's R-R check: returns (rr_scale, rr_status)."""
    ratios, vals = [], []
    for r in rows:
        rr = [float(x) for x in (r.get("rr_accepted_ms") or "").split() if x]
        dev = fnum(r.get("hr_device"))
        vals.extend(rr)
        if rr and dev and dev > 0:
            ratios.append((60000.0 / statistics.median(rr)) / dev)
    if len(ratios) < 30:
        return 1.0, "no_rr" if not vals else "unknown"
    med = statistics.median(ratios)
    scale = 1.024 if abs(med - 1.024) < UNIT_TOL else 1.0
    repeat = sum(a == b for a, b in zip(vals, vals[1:])) / max(1, len(vals) - 1)
    return scale, "synthetic" if repeat > 0.5 else "ok"


def parse_name(path: str) -> tuple[Optional[datetime], str]:
    m = re.match(r"session_(\d{8}_\d{6})(?:_(.+))?$", os.path.splitext(os.path.basename(path))[0])
    if not m:
        return None, ""
    try:
        start = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        start = None
    return start, (m.group(2) or "").replace("_", " ").strip()


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ==========================================================================
# Database
# ==========================================================================
def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    if ver > SCHEMA_VERSION:
        sys.exit(f"{path} was created by a newer version of trackr_db.py (schema {ver}).")
    upgrade = ver < 2 and con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'").fetchone()
    if upgrade:                               # schema 1 -> 2: HRR test columns
        have = {r["name"] for r in con.execute("PRAGMA table_info(sessions)")}
        for col, typ in (("hrr_peak_hr", "REAL"), ("hrr_valid", "INTEGER"), ("hrr_note", "TEXT")):
            if col not in have:
                con.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typ}")
        con.execute("DROP VIEW IF EXISTS v_sessions")
        con.execute("DROP VIEW IF EXISTS v_weekly")
    con.executescript(SCHEMA)
    if upgrade:
        done = refresh_hrr(con)
        print(f"Database upgraded to schema {SCHEMA_VERSION}: heart rate recovery re-checked "
              f"for {done} session(s).")
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()
    return con


def refresh_hrr(con: sqlite3.Connection) -> int:
    """Re-evaluate HRR60 for all sessions from their stored samples."""
    done = 0
    for s in con.execute("SELECT s.id, s.profile_rest_hr, s.profile_max_hr, a.age, a.rest_hr, a.max_hr "
                         "FROM sessions s LEFT JOIN athletes a ON a.id = s.athlete_id").fetchall():
        pts = con.execute("SELECT elapsed_s, hr FROM samples WHERE session_id = ? ORDER BY seq",
                          (s["id"],)).fetchall()
        if not pts:
            continue
        rest, mx = s["profile_rest_hr"], s["profile_max_hr"]
        if rest is None and s["rest_hr"] is not None and (s["max_hr"] or s["age"]):
            rest = s["rest_hr"]
            mx = s["max_hr"] or round(208 - 0.7 * s["age"])
        h = compute_hrr([r["elapsed_s"] for r in pts], [r["hr"] for r in pts], rest, mx)
        con.execute("UPDATE sessions SET peak_elapsed_s=?, hrr60_bpm=?, hrr_peak_hr=?, hrr_valid=?, "
                    "hrr_note=? WHERE id=?", (h["peak_elapsed_s"], h["hrr60_bpm"], h["hrr_peak_hr"],
                                              h["hrr_valid"], h["hrr_note"], s["id"]))
        done += 1
    return done


def app_config_profile(name: str, path: str = APP_CONFIG) -> Optional[Profile]:
    """Profile from the TRACKR app's saved config, if it belongs to this athlete."""
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return None
    if not isinstance(cfg, dict) or (cfg.get("name") or "").strip().lower() != name.strip().lower():
        return None
    return Profile(cfg.get("age"), cfg.get("sex"), cfg.get("weight"), cfg.get("rest_hr"), cfg.get("max_hr"))


def get_athlete(con: sqlite3.Connection, name: str, create: bool = True) -> Optional[sqlite3.Row]:
    row = con.execute("SELECT * FROM athletes WHERE name = ?", (name,)).fetchone()
    if row is None and create:
        con.execute("INSERT INTO athletes(name, updated_at) VALUES (?, ?)", (name, now()))
        row = con.execute("SELECT * FROM athletes WHERE name = ?", (name,)).fetchone()
    return row


def athlete_profile(con: sqlite3.Connection, ath: sqlite3.Row) -> Profile:
    p = Profile(ath["age"], ath["sex"], ath["weight_kg"], ath["rest_hr"], ath["max_hr"])
    if not p.complete:                         # fill from the app's config if it is the same athlete
        cfg = app_config_profile(ath["name"])
        if cfg and cfg.complete:
            set_profile(con, ath["id"], cfg)
            print(f"  profile for {ath['name']} taken from the app's saved config: {cfg.describe()}")
            p = cfg
    return p


def set_profile(con: sqlite3.Connection, athlete_id: int, p: Profile) -> None:
    con.execute("UPDATE athletes SET age=?, sex=?, weight_kg=?, rest_hr=?, max_hr=?, updated_at=? "
                "WHERE id=?", (p.age, p.sex, p.weight_kg, p.rest_hr, p.max_hr, now(), athlete_id))


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def import_file(con: sqlite3.Connection, path: str, athlete_name: Optional[str],
                force: bool, keep_samples: bool) -> str:
    """Import one CSV. Returns a one-line result."""
    name = os.path.basename(path)
    digest = sha256(path)
    old = con.execute("SELECT id FROM sessions WHERE file_sha256 = ?", (digest,)).fetchone()
    if old and not force:
        return f"skip   {name}: already imported (session {old['id']})"

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        cols = set(reader.fieldnames or [])
        if not REQUIRED_COLUMNS <= cols:
            return f"error  {name}: not a TRACKR session file (missing {', '.join(sorted(REQUIRED_COLUMNS - cols))})"
        rows = [r for r in reader if fnum(r.get("elapsed_s")) is not None]
    if len(rows) < 2:
        return f"error  {name}: no heart rate data"

    file_start, file_athlete = parse_name(path)
    athlete = (athlete_name or file_athlete or "Unknown").strip()
    if not old and file_start:
        # a changed copy of a session already in the database replaces it
        old = con.execute("SELECT id FROM sessions WHERE source_file = ? AND start_time = ?",
                          (name, file_start.isoformat(timespec="seconds"))).fetchone()

    # --- which heart rate to trust
    new_format = "rr_status" in cols
    notes = []
    if new_format:
        statuses = [r["rr_status"] for r in rows if r.get("rr_status")]
        rr_status = statuses[-1] if statuses else "unknown"
        rr_scale = fnum(rows[-1].get("rr_scale")) or 1.0
        hr_source, corrected = "app", 0
        hr = [fnum(r["hr_filtered"]) for r in rows]
    else:
        rr_scale, rr_status = check_old_rr(rows)
        if rr_scale != 1.0 or rr_status == "synthetic":
            hr_source, corrected = "strap", 1
            hr = [fnum(r["hr_device"]) for r in rows]
            why = []
            if rr_scale != 1.0:
                why.append("R-R sent in milliseconds")
            if rr_status == "synthetic":
                why.append("R-R not beat-to-beat")
            notes.append("Recorded before the app's R-R check (" + ", ".join(why) +
                         "); heart rate taken from the strap.")
        else:
            hr_source, corrected = "app", 0
            hr = [fnum(r["hr_filtered"]) for r in rows]
    elapsed = [fnum(r["elapsed_s"]) for r in rows]

    # --- times
    def ts(r):
        try:
            return datetime.fromisoformat(r["time"])
        except (ValueError, TypeError):
            return None
    t_first, t_last = ts(rows[0]), ts(rows[-1])
    start = file_start or t_first
    end = t_last or (start + timedelta(seconds=elapsed[-1]) if start else None)
    if start is None or end is None:
        return f"error  {name}: no valid timestamps"

    # --- metrics
    con.execute("SAVEPOINT imp")
    try:
        ath = get_athlete(con, athlete)
        prof = athlete_profile(con, ath)
        m = compute_metrics(elapsed, hr, prof if prof.complete else None)
        if "trimp" not in m:                  # no profile: keep what the app recorded
            m.update(metrics_from_csv(rows, elapsed))
            if corrected:
                notes.append("Zones/TRIMP/kcal are the app's original values (HR ~3 bpm high); "
                             "set the athlete profile and run recalc.")
        conf = [fnum(r.get("confidence")) for r in rows if fnum(r.get("confidence")) is not None]
        if old:
            con.execute("DELETE FROM sessions WHERE id = ?", (old["id"],))
        cur = con.execute(
            """INSERT INTO sessions (id, athlete_id, source_file, file_sha256, start_time, end_time,
               session_date, duration_s, samples, hr_source, rr_status, rr_scale, corrected,
               avg_hr, max_hr_reached, min_hr, trimp, kcal, z1_s, z2_s, z3_s, z4_s, z5_s,
               peak_elapsed_s, hrr60_bpm, hrr_peak_hr, hrr_valid, hrr_note, gaps, gap_s,
               avg_confidence, profile_rest_hr, profile_max_hr, profile_age, profile_weight,
               profile_sex, metrics_source, notes, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (old["id"] if old else None, ath["id"], name, digest, start.isoformat(timespec="seconds"),
             end.isoformat(timespec="seconds"), start.date().isoformat(), elapsed[-1], len(rows),
             hr_source, rr_status, rr_scale, corrected, m["avg_hr"], m["max_hr_reached"], m["min_hr"],
             m.get("trimp"), m.get("kcal"), *[m.get(f"z{i}_s") for i in range(1, 6)],
             m["peak_elapsed_s"], m["hrr60_bpm"], m["hrr_peak_hr"], m["hrr_valid"], m["hrr_note"],
             m["gaps"], m["gap_s"],
             statistics.mean(conf) if conf else None,
             prof.rest_hr if m["metrics_source"] == "profile" else None,
             prof.effective_max if m["metrics_source"] == "profile" else None,
             prof.age if m["metrics_source"] == "profile" else None,
             prof.weight_kg if m["metrics_source"] == "profile" else None,
             prof.sex if m["metrics_source"] == "profile" else None,
             m["metrics_source"], " ".join(notes) or None, now()))
        sid = cur.lastrowid
        if keep_samples:
            con.executemany(
                """INSERT INTO samples (session_id, seq, time, elapsed_s, hr, hr_filtered, hr_device,
                   confidence, rmssd_ms, zone, rr_accepted_ms, rr_rejected, contact, rr_received_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ((sid, i, r.get("time"), elapsed[i], hr[i], fnum(r.get("hr_filtered")),
                  int(fnum(r["hr_device"]) or 0), fnum(r.get("confidence")),
                  None if corrected else fnum(r.get("rmssd_ms")),
                  zone_of(hr[i], prof) if prof.complete and hr[i] else int(fnum(r.get("zone")) or 0) or None,
                  r.get("rr_accepted_ms") or None, int(fnum(r.get("rr_rejected")) or 0),
                  {"True": 1, "False": 0}.get(r.get("contact") or ""), r.get("rr_received_ms") or None)
                 for i, r in enumerate(rows)))
        con.execute("RELEASE imp")
    except Exception:
        con.execute("ROLLBACK TO imp")
        con.execute("RELEASE imp")
        raise
    flag = " [corrected: HR from strap]" if corrected else ""
    tr = f"TRIMP {m['trimp']:.1f}" if m.get("trimp") is not None else "TRIMP -"
    if m["hrr_valid"] == 1:
        tr += f", HRR60 {m['hrr60_bpm']:.0f} from {m['hrr_peak_hr']:.0f}"
    return (f"{'update' if old else 'import'} {name}: session {sid}, {athlete}, "
            f"{elapsed[-1] / 60:.0f} min, avg {m['avg_hr']:.0f} / peak {m['max_hr_reached']:.0f} bpm, "
            f"{tr}{flag}")


def metrics_from_csv(rows: List[dict], elapsed: List[float]) -> dict:
    """Zones/TRIMP/kcal as recorded by the app (used when no profile is known)."""
    zt = [0.0] * 5
    for i in range(1, len(rows)):
        z = int(fnum(rows[i].get("zone")) or 0)
        if 1 <= z <= 5:
            zt[z - 1] += min(MAX_DT_S, elapsed[i] - elapsed[i - 1])
    return dict(trimp=fnum(rows[-1].get("trimp")), kcal=fnum(rows[-1].get("kcal")),
                **{f"z{i + 1}_s": zt[i] for i in range(5)}, metrics_source="csv")


def find_csvs(paths: Iterable[str]) -> List[str]:
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(glob.glob(os.path.join(p, "**", "session_*.csv"), recursive=True))
        elif any(ch in p for ch in "*?["):
            out += sorted(glob.glob(p))
        else:
            out.append(p)
    return out


def recalc(con: sqlite3.Connection, athlete: Optional[str]) -> None:
    q = "SELECT s.id, s.source_file, a.id AS aid FROM sessions s JOIN athletes a ON a.id = s.athlete_id"
    args: tuple = ()
    if athlete:
        q += " WHERE a.name = ?"
        args = (athlete,)
    done = 0
    for s in con.execute(q, args).fetchall():
        ath = con.execute("SELECT * FROM athletes WHERE id = ?", (s["aid"],)).fetchone()
        p = athlete_profile(con, ath)
        if not p.complete:
            print(f"  skip session {s['id']} ({ath['name']}): profile incomplete")
            continue
        pts = con.execute("SELECT seq, elapsed_s, hr FROM samples WHERE session_id = ? ORDER BY seq",
                          (s["id"],)).fetchall()
        if not pts:
            print(f"  skip session {s['id']}: imported without samples")
            continue
        el, hr = [r["elapsed_s"] for r in pts], [r["hr"] for r in pts]
        m = compute_metrics(el, hr, p)
        con.execute("""UPDATE sessions SET trimp=?, kcal=?, z1_s=?, z2_s=?, z3_s=?, z4_s=?, z5_s=?,
                       peak_elapsed_s=?, hrr60_bpm=?, hrr_peak_hr=?, hrr_valid=?, hrr_note=?,
                       profile_rest_hr=?, profile_max_hr=?, profile_age=?, profile_weight=?,
                       profile_sex=?, metrics_source='profile' WHERE id=?""",
                    (m["trimp"], m["kcal"], *[m[f"z{i}_s"] for i in range(1, 6)],
                     m["peak_elapsed_s"], m["hrr60_bpm"], m["hrr_peak_hr"], m["hrr_valid"], m["hrr_note"],
                     p.rest_hr, p.effective_max, p.age, p.weight_kg, p.sex, s["id"]))
        con.executemany("UPDATE samples SET zone=? WHERE session_id=? AND seq=?",
                        ((zone_of(r["hr"], p) if r["hr"] else None, s["id"], r["seq"]) for r in pts))
        done += 1
    print(f"Recalculated {done} session(s).")


# ==========================================================================
# Reports
# ==========================================================================
def print_table(rows: List[sqlite3.Row], cols: List[tuple[str, str]]) -> None:
    if not rows:
        print("(no sessions)")
        return
    data = [[("" if r[k] is None else str(r[k])) for k, _ in cols] for r in rows]
    w = [max(len(h), *(len(d[i]) for d in data)) for i, (_, h) in enumerate(cols)]
    print("  ".join(h.ljust(w[i]) for i, (_, h) in enumerate(cols)))
    print("  ".join("-" * x for x in w))
    for d in data:
        print("  ".join(v.rjust(w[i]) if v.replace(".", "").replace("-", "").isdigit() else v.ljust(w[i])
                        for i, v in enumerate(d)))


def where_athlete(athlete: Optional[str]) -> tuple[str, tuple]:
    return (" WHERE athlete = ?", (athlete,)) if athlete else ("", ())


def hrr_text(r) -> str:
    """HRR60 for tables: the value for a valid test, '-' otherwise."""
    return f"{r['hrr60_bpm']:.0f}" if r["hrr_valid"] == 1 and r["hrr60_bpm"] is not None else "-"


def cmd_list(con, a) -> None:
    w, args = where_athlete(a.athlete)
    rows = [dict(r, hrr=hrr_text(r)) for r in
            con.execute(f"SELECT * FROM v_sessions{w} ORDER BY start_time", args).fetchall()]
    print_table(rows, [("id", "id"), ("athlete", "athlete"), ("start_time", "start"), ("minutes", "min"),
                       ("avg_hr", "avg"), ("max_hr", "max"), ("trimp", "TRIMP"), ("kcal", "kcal"),
                       ("z1_pct", "Z1%"), ("z2_pct", "Z2%"), ("z3_pct", "Z3%"), ("z4_pct", "Z4%"),
                       ("z5_pct", "Z5%"), ("hrr", "HRR60"), ("gaps", "gaps"),
                       ("corrected", "corr"), ("metrics_source", "metrics")])
    print("\nHRR60 is shown only for valid recovery tests; run 'hrr' for details.")


def cmd_hrr(con, a) -> None:
    """Every session's heart rate recovery with its peak and whether it is a valid test."""
    w, args = where_athlete(a.athlete)
    rows = []
    for r in con.execute(f"SELECT * FROM v_sessions{w} ORDER BY start_time", args).fetchall():
        if a.valid and r["hrr_valid"] != 1:
            continue
        rows.append(dict(r, test={1: "yes", 0: "no"}.get(r["hrr_valid"], "?"),
                         peak="" if r["hrr_peak_hr"] is None else f"{r['hrr_peak_hr']:.0f}",
                         drop="" if r["hrr60_bpm"] is None else f"{r['hrr60_bpm']:.0f}",
                         after="" if r["hrr60_bpm"] is None or r["hrr_peak_hr"] is None
                         else f"{r['hrr_peak_hr'] - r['hrr60_bpm']:.0f}",
                         why=r["hrr_note"] or ""))
    print_table(rows, [("id", "id"), ("athlete", "athlete"), ("start_time", "start"), ("test", "test"),
                       ("peak", "peak HR"), ("after", "HR +60s"), ("drop", "HRR60"), ("why", "note")])
    valid = [r for r in rows if r["test"] == "yes"]
    if valid:
        print(f"\n{len(valid)} valid recovery test(s). Compare HRR60 between tests with a similar peak HR: "
              "at the same peak, a larger drop means better recovery.")
    else:
        print("\nNo valid recovery tests yet. To make one: finish with 2-3 minutes of hard effort "
              "(Zone 4+), then walk slowly or stand for at least 1 minute while still recording.")


def cmd_weekly(con, a) -> None:
    w, args = where_athlete(a.athlete)
    rows = con.execute(f"SELECT * FROM v_weekly{w} ORDER BY athlete, week", args).fetchall()
    print_table(rows, [("athlete", "athlete"), ("week", "week"), ("sessions", "sessions"),
                       ("minutes", "min"), ("trimp", "TRIMP"), ("kcal", "kcal"), ("avg_hr", "avg HR"),
                       ("max_hr", "max HR"), ("easy_pct", "Z1-2 %"), ("hard_pct", "Z4-5 %"),
                       ("hrr_tests", "HRR tests"), ("avg_hrr_peak", "HRR peak"), ("avg_hrr60", "HRR60")])
    print("\nHRR60 = average of valid recovery tests only (HRR peak = their average peak HR).")


def fitness_series(con, athlete_id: int, until: date) -> List[dict]:
    """Banister fitness/fatigue: CTL (42-day) and ATL (7-day) exponential averages of daily TRIMP."""
    rows = con.execute("SELECT session_date, SUM(trimp) AS t FROM sessions WHERE athlete_id = ? "
                       "AND trimp IS NOT NULL GROUP BY session_date ORDER BY session_date",
                       (athlete_id,)).fetchall()
    if not rows:
        return []
    daily = {r["session_date"]: r["t"] for r in rows}
    d = date.fromisoformat(rows[0]["session_date"])
    ctl = atl = 0.0
    out = []
    while d <= until:
        t = daily.get(d.isoformat(), 0.0)
        tsb = ctl - atl                         # form = yesterday's fitness - fatigue
        ctl += (t - ctl) / 42
        atl += (t - atl) / 7
        out.append(dict(day=d.isoformat(), trimp=t, ctl=ctl, atl=atl, tsb=tsb))
        d += timedelta(days=1)
    return out


def cmd_fitness(con, a) -> None:
    athletes = con.execute("SELECT * FROM athletes" + (" WHERE name = ?" if a.athlete else "") +
                           " ORDER BY name", (a.athlete,) if a.athlete else ()).fetchall()
    for ath in athletes:
        s = fitness_series(con, ath["id"], date.today())
        if not s:
            continue
        print(f"\n{ath['name']}  (CTL = fitness, 42-day; ATL = fatigue, 7-day; TSB = form)")
        print("day          TRIMP    CTL    ATL    TSB")
        for r in s[-a.days:]:
            print(f"{r['day']}  {r['trimp']:6.1f} {r['ctl']:6.1f} {r['atl']:6.1f} {r['tsb']:+6.1f}")
        if len(s) < 42:
            print(f"  note: only {len(s)} days of history; CTL becomes meaningful after ~6 weeks.")


def cmd_athlete(con, a) -> None:
    ath = get_athlete(con, a.name)
    p = Profile(ath["age"], ath["sex"], ath["weight_kg"], ath["rest_hr"], ath["max_hr"])
    changed = False
    for field, val in (("age", a.age), ("sex", a.sex), ("weight_kg", a.weight), ("rest_hr", a.rest_hr),
                       ("max_hr", a.max_hr)):
        if val is not None:
            setattr(p, field, val)
            changed = True
    if a.auto_max:
        p.max_hr, changed = None, True
    if changed:
        set_profile(con, ath["id"], p)
        print(f"Saved profile for {a.name}: {p.describe()}")
        print("Run 'recalc' to update this athlete's existing sessions.")
    else:
        print(f"{a.name}: {p.describe()}")


def cmd_remove(con, a) -> None:
    for sid in a.ids:
        n = con.execute("DELETE FROM sessions WHERE id = ?", (sid,)).rowcount
        print(f"session {sid}: {'removed' if n else 'not found'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Combine TRACKR session CSV files into an SQLite database.")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"database file (default {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="import session CSV files or folders")
    p.add_argument("paths", nargs="+", help="CSV files, folders (searched recursively) or wildcards")
    p.add_argument("--athlete", help="athlete name (default: from the file name)")
    p.add_argument("--force", action="store_true", help="re-import files that were already imported")
    p.add_argument("--no-samples", action="store_true", help="store session summaries only")

    p = sub.add_parser("athlete", help="show or set an athlete profile")
    p.add_argument("name")
    p.add_argument("--age", type=int)
    p.add_argument("--sex", choices=["m", "f"])
    p.add_argument("--weight", type=float, help="kg")
    p.add_argument("--rest-hr", type=int)
    p.add_argument("--max-hr", type=int)
    p.add_argument("--auto-max", action="store_true", help="estimate max HR from age again")

    p = sub.add_parser("recalc", help="recalculate metrics from stored samples with current profiles")
    p.add_argument("--athlete")
    for name, hlp in (("list", "list sessions"), ("weekly", "weekly totals")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--athlete")
    p = sub.add_parser("hrr", help="heart rate recovery (HRR60) per session, with the test check")
    p.add_argument("--athlete")
    p.add_argument("--valid", action="store_true", help="show valid recovery tests only")
    p = sub.add_parser("fitness", help="fitness / fatigue / form (CTL / ATL / TSB) by day")
    p.add_argument("--athlete")
    p.add_argument("--days", type=int, default=14)
    p = sub.add_parser("remove", help="delete sessions by id")
    p.add_argument("ids", nargs="+", type=int)
    a = ap.parse_args()

    con = connect(a.db)
    try:
        if a.cmd == "import":
            files = find_csvs(a.paths)
            if not files:
                print("No session CSV files found.")
            counts = {"import": 0, "update": 0, "skip": 0, "error": 0}
            for f in files:
                try:
                    line = import_file(con, f, a.athlete, a.force, not a.no_samples)
                except (OSError, UnicodeDecodeError, csv.Error) as exc:
                    line = f"error  {os.path.basename(f)}: {exc}"
                counts[line.split()[0]] += 1
                print(line)
                con.commit()
            print(f"\nDone: {counts['import']} imported, {counts['update']} updated, "
                  f"{counts['skip']} already in the database, {counts['error']} failed. "
                  f"Database: {os.path.abspath(a.db)}")
        elif a.cmd == "athlete":
            cmd_athlete(con, a)
        elif a.cmd == "recalc":
            recalc(con, a.athlete)
        elif a.cmd == "list":
            cmd_list(con, a)
        elif a.cmd == "weekly":
            cmd_weekly(con, a)
        elif a.cmd == "hrr":
            cmd_hrr(con, a)
        elif a.cmd == "fitness":
            cmd_fitness(con, a)
        elif a.cmd == "remove":
            cmd_remove(con, a)
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    main()
