# TRACKR HEART RATE – Training App

A desktop training application for the **TRACKR HEART RATE** chest strap. An advanced detection algorithm gives you accurate real-time heart rate data, so you can train more efficiently and perform at your best.

The app connects to the strap over **Bluetooth LE** or **ANT+**. It shows a live heart rate chart coloured by training zone, and records each session with Start, Pause, Resume and Stop controls.

<img width="1320" height="840" alt="image" src="https://github.com/user-attachments/assets/05fd41e4-0a60-4fea-a6e1-11dc6068fce2" />


---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Command-line options](#command-line-options)
- [Using the app](#using-the-app)
- [Detection algorithm](#detection-algorithm)
- [Training metrics and formulas](#training-metrics-and-formulas)
- [Session files](#session-files)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)

---

## Features

| Area | What you get |
|---|---|
| **Connectivity** | Bluetooth LE (standard Heart Rate Service), ANT+ (Heart Rate profile), and a built-in simulator for testing without hardware |
| **Detection algorithm** | Five-stage processing of the raw beat-to-beat (R-R) intervals: physiological gate, adaptive artifact rejection, missed-beat repair, sensor fusion and adaptive smoothing |
| **Session control** | **Start**, **Pause**, **Resume** and **Stop** buttons; the space bar also pauses and resumes |
| **Real-time chart** | Heart rate line coloured by zone, zone bands with bpm ranges, a target-time marker and pause markers |
| **Customizable period** | The chart's time axis equals the training time in minutes (**default 50**), and you can change it at any time, even during a session |
| **Live dashboard** | Heart rate, zone, % of max HR, elapsed and remaining time, average and peak HR, HRV (RMSSD), signal quality, training load (TRIMP), calories and time in zone |
| **Alerts** | Optional high heart rate alarm and a notification when the target training time is reached |
| **Auto-reconnect** | The Bluetooth link reconnects automatically if it drops |
| **Saved config** | **Save Config** stores your name, athlete details, sensor and session settings; they load automatically the next time you open the app |
| **Session report** | Every session is saved as a CSV of all samples and a PNG report with name, age, sex, weight, date and time, session summary, the HR chart and time-in-zone percentages |

---

## Requirements

- **Python 3.9 or newer**
- **Tkinter**, which ships with Python on Windows and macOS (see below for Linux)
- **matplotlib** and **numpy**
- For Bluetooth: **bleak** and a computer with Bluetooth 4.0 or newer
- For ANT+: **openant** and an ANT+ USB stick (for example a Garmin USB-m or USB2 stick)

| Package | Needed for | Required |
|---|---|---|
| `matplotlib`, `numpy` | GUI and chart | Yes |
| `bleak` | Bluetooth LE | Only for Bluetooth |
| `openant` | ANT+ | Only for ANT+ |

---

## Installation

```bash
# 1. (Recommended) create a virtual environment
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate

# 2. Install the required packages
pip install matplotlib numpy

# 3. Install the connectivity you need
pip install bleak        # Bluetooth LE
pip install openant      # ANT+
```

### If `pip install bleak` fails

Current bleak (3.x) needs **Python 3.10 or newer**. Check your version with `python --version` (on Windows: `py --list` shows every installed Python).

| Error message contains | Cause | Fix |
|---|---|---|
| `Microsoft Visual C++ 14.0 or greater is required`, `Failed building wheel for winrt-…`, or `CMake` errors (Windows) | Your Python is too new for bleak's Windows parts. They have no ready-made package for Python 3.15 yet, so pip tries to compile them | Install **Python 3.13 or 3.14 (64-bit)** from python.org, then use it: `py -3.13 -m pip install bleak matplotlib numpy` and run the app with `py -3.13 trackr_hr.py` |
| `externally-managed-environment` (Linux, macOS Homebrew) | The system Python does not allow pip installs | Use a virtual environment (see [Installation](#installation)) and install inside it |
| `Failed building wheel for dbus-fast` or `gcc` / `Python.h` errors (Linux, especially Raspberry Pi with a 32-bit OS) | No ready-made package for your processor, so pip compiles it | `sudo apt install build-essential python3-dev`, then install again; or use a 64-bit OS |
| `No matching distribution found for bleak` | Python older than 3.10, or a very old pip | Install Python 3.10+; run `python -m pip install --upgrade pip` |
| Install succeeds but the app says *"Bluetooth needs the bleak package"* | bleak went into a different Python than the one running the app | Install and run with the same interpreter: `python -m pip install bleak` then `python trackr_hr.py` |

Bluetooth is optional: if bleak won't install, the app still runs with **ANT+** or the **Simulator**.

### Platform notes

**Windows**
- Tkinter is included with the python.org installer.
- Bluetooth works through the built-in Windows Bluetooth stack; no pairing in Windows Settings is needed.
- For ANT+, the stick may need a WinUSB driver. Installing it with [Zadig](https://zadig.akeo.ie/) is the usual way.

**macOS**
- Use the python.org installer or Homebrew (`brew install python-tk`).
- On the first Bluetooth scan, macOS asks for Bluetooth permission for your terminal or IDE; allow it.
- On macOS, devices are identified by a UUID instead of a MAC address. This is normal.

**Linux**
- Install Tkinter: `sudo apt install python3-tk` (Debian/Ubuntu) or `sudo dnf install python3-tkinter` (Fedora).
- Bluetooth uses BlueZ; make sure the `bluetooth` service is running.
- For ANT+ without root, add a udev rule for the stick, for example:
  ```
  SUBSYSTEM=="usb", ATTRS{idVendor}=="0fcf", MODE="0666"
  ```
  Save it as `/etc/udev/rules.d/99-ant.rules`, then run `sudo udevadm control --reload` and replug the stick.

---

## Quick start

```bash
# Try the app without a strap
python trackr_hr.py --source sim

# Use your TRACKR strap over Bluetooth
python trackr_hr.py

# Use your TRACKR strap over ANT+
python trackr_hr.py --source ant
```

1. Moisten the strap's electrodes and put it on. The strap wakes up when it detects your heartbeat.
2. Pick the **Connection** type and enter your name and athlete details.
3. Set the **Training time**; the default is 50 minutes.
4. Press **Save Config** so your settings are there next time (you only need to do this once, or when something changes).
5. Press **Start**.

---

## Command-line options

All options are optional. They preset the fields in the app **for that run only**, overriding the saved config. They are not written to the config unless you press **Save Config**. Defaults apply when neither the saved config nor an option sets a value.

| Option | Default | Description |
|---|---|---|
| `--config PATH` | `~/.trackr_hr/config.json` | Config file to load and save; use one file per athlete if several people share the computer |
| `--name NAME` | empty | Athlete name |
| `--source {ble,ant,sim}` | `ble` | Connection type selected at startup |
| `--address ADDR` | auto | Bluetooth address (Windows/Linux) or UUID (macOS) of the strap |
| `--ant-id N` | `0` | ANT+ device ID; `0` connects to the first strap found |
| `--minutes M` | `50` | Training time and chart display period, in minutes |
| `--age N` | `30` | Age, used to estimate max HR and calories |
| `--rest-hr N` | `60` | Resting heart rate (bpm) |
| `--max-hr N` | auto | Maximum heart rate; if omitted it is estimated from age |
| `--weight KG` | `70` | Body weight, used for calories |
| `--sex {m,f}` | `m` | Used for training load and calories |
| `--alert-hr N` | off | Beep when heart rate goes above this value |

Examples:

```bash
python trackr_hr.py --source ble --minutes 60 --age 35 --rest-hr 55 --max-hr 186 --weight 68 --alert-hr 175
python trackr_hr.py --minutes 30                      # saved settings, but a 30-minute session today
python trackr_hr.py --config ~/athletes/nok.json      # a second athlete with their own config
```

---

## Using the app

### Window layout

| Area | Contents |
|---|---|
| **Header** | Connection status (for example `● Connected (BLE)`) and session state badge: `READY`, `● RECORDING` or `❚❚ PAUSED` |
| **Left panel** | Sensor, athlete and session settings, the Start / Pause / Resume / Stop buttons and **Save Config** |
| **Heart rate card** | Large live heart rate in the current zone colour, with zone name and % of max HR |
| **Metric cards** | Elapsed, Remaining, Avg HR, Peak HR, HRV (RMSSD), Signal, Training load, Calories |
| **Chart** | Real-time heart rate against training time |
| **Time in zone** | Coloured bar and per-zone totals |
| **Status bar** | Connection messages, alerts and save locations |

### 1. Connect the strap

| Connection | How to use |
|---|---|
| **Bluetooth LE** | Press **Scan** to list nearby heart rate straps (a TRACKR strap is listed first), then pick yours. If you leave the device field empty, the app connects to the best strap it finds when you press Start. |
| **ANT+** | Plug in the ANT+ stick. Leave **ANT+ ID** at `0` to use the first strap found, or enter the strap's device ID to always use that one (useful in a gym with several straps). |
| **Simulator** | No hardware needed. Plays a repeating 2-minute workout (warm-up, intervals, cool-down) with injected noise and missed beats, so you can see the algorithm work. |

The connection starts when you press **Start** and ends when you press **Stop**.

### 2. Enter athlete settings

**Name** is printed on the session report and added to the session file names. Age, resting HR, max HR, weight and sex determine your heart rate zones, training load and calories. The zone bands on the chart update as you type. Leave **Max HR** blank to estimate it from your age.

Settings are locked during a session so the numbers stay consistent. Only **Training time** and **HR alert** can be changed while recording.

### Saving your settings (Save Config)

Press **Save Config** to store the current settings. The button briefly shows **✓ Saved**, and the status bar shows where the file was written. The next time you open the app, the saved settings are loaded automatically and the status bar shows `Loaded saved config from …`.

| Saved | Not saved |
|---|---|
| Name, age, resting HR, max HR, weight, sex | Session data (that goes into the session files) |
| Connection type, chosen Bluetooth strap, ANT+ ID | |
| Training time, HR alert | |

- Settings are checked before saving; if a value is out of range, an error explains which one.
- Changes are **not** saved automatically. If you change a setting and close the app without pressing Save Config, the previous saved settings load next time.
- The config is stored in `~/.trackr_hr/config.json` (on Windows `C:\Users\<you>\.trackr_hr\config.json`). It is plain JSON, so you can also edit or back it up. If the file is damaged, the app starts with default settings and says so in the status bar; pressing Save Config writes a fresh file.
- A remembered Bluetooth strap is reconnected directly at Start, without scanning.

Example `config.json`:

```json
{
  "name": "Somchai Jaidee",
  "source": "ble",
  "device": "TRACKR HR (C4:7F:51:2A:90:1B)",
  "device_address": "C4:7F:51:2A:90:1B",
  "ant_id": 0,
  "age": 42,
  "rest_hr": 52,
  "max_hr": null,
  "weight": 64.5,
  "sex": "f",
  "minutes": 50,
  "alert_hr": 170
}
```

### 3. Session controls

| Button | Available when | What it does |
|---|---|---|
| **▶ Start** | Ready | Connects the sensor, clears the previous session and starts recording |
| **❚❚ Pause** | Recording | Stops the clock, training load and calories. The live heart rate keeps updating and a dotted marker is drawn on the chart |
| **▶ Resume** | Paused | Continues recording from where you paused |
| **■ Stop** | Recording or paused | Asks for confirmation, disconnects, saves the session and shows a summary |

**Space bar** toggles Pause and Resume (when the cursor is not in a text field).

Paused time is not counted. Elapsed time, the chart's time axis, time in zone, training load and calories reflect active training only.

Closing the window during a session asks whether to save it first.

### 4. Real-time chart

- **X axis:** training time in minutes, from 0 to the **Training time** you set (default 50).
- **Y axis:** heart rate in bpm. It covers resting HR to max HR and expands if your heart rate goes outside that range.
- **Line colour:** the zone of each moment (grey Z1, blue Z2, green Z3, amber Z4, red Z5).
- **Zone bands:** shaded backgrounds labelled with zone names and bpm ranges.
- **Dashed vertical line:** the target training time.
- **Dotted vertical lines:** places where you paused.

**Changing the display period:** edit **Training time (min)** at any time, and the chart rescales immediately. For example, set 30 for a short interval session or 90 for a long ride. If you train past the target, the app beeps, shows a message, and the chart keeps extending. The Remaining card then shows overtime as `+mm:ss` in green.

### 5. Dashboard metrics

| Metric | Meaning |
|---|---|
| **Heart rate** | Filtered heart rate from the detection algorithm. It turns red when above your HR alert |
| **Zone · % max** | Current zone and heart rate as a percentage of max HR |
| **Elapsed / Remaining** | Active training time and time left to your target |
| **Avg HR / Peak HR** | Average and highest heart rate while recording |
| **HRV (RMSSD)** | Heart rate variability from clean beats only. Higher at rest usually means better recovery; it naturally drops during hard effort |
| **Signal** | Signal quality: green ≥ 70%, amber 40–69%, red < 40%. "No contact" means the strap has lost skin contact |
| **Training load** | Cumulative Banister TRIMP for the session |
| **Calories** | Estimated energy burned (kcal) |
| **Time in zone** | Time and share spent in each zone |

If no data arrives for about 6 seconds, the heart rate shows `--` and the header shows `● No signal`.

---

## Detection algorithm

The strap sends both its own heart rate value and the raw time between heartbeats (R-R intervals). Instead of only showing the strap's number, the app processes every R-R interval in five stages:

1. **Physiological gate:** intervals outside 270–2000 ms (about 30–220 bpm) are rejected as impossible.
2. **Adaptive median gate:** each beat is compared with the median of the last 16 clean beats. Beats more than 20% away are rejected as artifacts (extra or ectopic beats, strap movement, static). During the first few beats, the strap's own heart rate is used as the reference. After three rejections in a row, the tolerance widens and the reference resets, so a real, sudden change such as a sprint start is followed instead of being blocked.
3. **Missed-beat repair:** an interval about 2× or 3× the median means the strap missed one or two beats. The interval is split into equal beats instead of being thrown away. Repaired beats are excluded from HRV.
4. **Sensor fusion:** the heart rate from clean beats is blended with the strap's own value, weighted by a **confidence score** (0–100%). The score combines skin contact, the recent rejection rate and how stable the rhythm is. This is the **Signal** value on the dashboard.
5. **Adaptive smoothing:** a Kalman-style filter smooths the result. It reacts faster when heart rate is changing quickly and smooths harder when the rhythm is steady or the signal is poor.

**Result:** a stable reading that ignores spikes and dropouts but still follows real changes within about 2 seconds. In simulator tests with injected noise spikes and missed beats, the reading started at the correct heart rate, gave realistic HRV values, and followed a jump from 120 to 165 bpm in about 2 seconds.

---

## Training metrics and formulas

### Max heart rate

If **Max HR** is blank, it is estimated with the Tanaka formula:

```
Max HR = 208 − 0.7 × age
```

For the most accurate zones, enter a max HR measured in a field test.

### Heart rate zones (Karvonen / heart rate reserve)

```
HRR fraction = (HR − resting HR) / (max HR − resting HR)
```

| Zone | % of HRR | Purpose | Example (age 30, rest 60, max 187) |
|---|---|---|---|
| Z1 Recovery | 50–60% | Warm-up, recovery | 124–136 bpm |
| Z2 Endurance | 60–70% | Aerobic base, fat burning | 136–149 bpm |
| Z3 Tempo | 70–80% | Aerobic fitness | 149–162 bpm |
| Z4 Threshold | 80–90% | Lactate threshold | 162–174 bpm |
| Z5 VO2max | 90–100% | Maximum effort | 174–187 bpm |

Heart rate below Z1 is shown as **Warm-up** and counted in Z1.

### Training load (Banister TRIMP)

```
TRIMP += minutes × HRR × 0.64 × e^(b × HRR)      b = 1.92 (male), 1.67 (female)
```

As a rough guide, an easy 50-minute session scores about 30–60, and a hard 50-minute interval session can score 100 or more.

### Calories (Keytel et al., 2005)

```
Male:   kcal/min = (−55.0969 + 0.6309·HR + 0.1988·weight + 0.2017·age) / 4.184
Female: kcal/min = (−20.4022 + 0.4472·HR − 0.1263·weight + 0.0740·age) / 4.184
```

### HRV (RMSSD)

The root mean square of successive differences between clean, consecutive R-R intervals, over the last 60 beats. Rejected and repaired beats are excluded.

---

## Session files

When you press **Stop**, the session is saved in a `trackr_sessions` folder inside the folder you started the app from:

```
trackr_sessions/
├── session_20261021_094620_Somchai_Jaidee.csv    # every sample
└── session_20261021_094620_Somchai_Jaidee.png    # session report image
```

The file name is the session start time followed by the athlete's name (spaces become `_`; names in any language, such as Thai, are kept). Without a name, the file is just `session_<date>_<time>`. A summary window shows the athlete, duration, average and peak HR, training load, calories, time in each zone, and where the files were saved. If no heart rate data was recorded, nothing is saved.

### Session report (PNG)

<img width="1690" height="1118" alt="image" src="https://github.com/user-attachments/assets/7f4a6c2f-2325-49ec-be96-f1a797819439" />

The report image is made for sharing with a coach or keeping as a training record. It contains:

| Section | Contents |
|---|---|
| **Header** | TRACKR title, resting HR and max HR used for the zones |
| **Athlete** | Name, age, sex, weight |
| **Date / time** | Session date with start and end time (both dates shown if the session crosses midnight) |
| **Summary** | Active duration, average HR, peak HR, training load (TRIMP), calories |
| **Heart rate chart** | Full session, coloured by zone, with zone bands, target-time line, pause markers and the peak marked |
| **Time in zone** | Coloured bar with the percentage of each zone, plus the per-zone percentage, time and bpm range |

The report is drawn separately from the window, so it always has the same size and layout (about 1690 × 1120 pixels) whatever the window size. If the image cannot be created, the CSV is still saved and a warning is shown.

### CSV columns

One row per sensor packet (about one per second) while recording; paused time is not recorded.

| Column | Description |
|---|---|
| `time` | Date and time of the sample (ISO 8601, milliseconds) |
| `elapsed_s` | Active training time in seconds |
| `hr_filtered` | Heart rate from the detection algorithm (bpm) |
| `hr_device` | Heart rate reported by the strap itself (bpm) |
| `confidence` | Signal confidence, 0–1 |
| `rmssd_ms` | HRV (RMSSD) in ms; empty until enough clean beats |
| `zone` | Zone 1–5 |
| `trimp` | Cumulative training load |
| `kcal` | Cumulative calories |
| `rr_accepted_ms` | Accepted R-R intervals in this packet, space-separated (ms) |
| `rr_rejected` | Number of R-R intervals rejected in this packet |
| `contact` | Skin contact: `True`, `False`, or empty if not reported |

The CSV opens directly in Excel, Google Sheets, or pandas:

```python
import pandas as pd
df = pd.read_csv("trackr_sessions/session_20261020_081737.csv")
print(df[["elapsed_s", "hr_filtered"]].describe())
```

---

## Architecture

Everything is in a single file, `trackr_hr.py`.

```
 TRACKR strap ──BLE──►  ble_source()  ┐
              ──ANT+─►  ant_source()  ├─► SensorWorker (background thread + asyncio)
 Simulator ──────────►  sim_source()  ┘            │  RawSample
                                                   ▼
                                              queue.Queue
                                                   │  polled every 150 ms
                                                   ▼
                    TrackrApp (Tkinter main thread)
                    ├── HeartRateDetector   → filtered HR, confidence, HRV
                    ├── TrainingAnalytics   → zones, TRIMP, calories, time in zone
                    ├── matplotlib chart    → redrawn once per second
                    ├── config              → ~/.trackr_hr/config.json (Save Config / startup)
                    └── session recorder    → CSV + report PNG on Stop
```

| Component | Role |
|---|---|
| `parse_ble_hr_measurement()` | Decodes the Bluetooth Heart Rate Measurement characteristic (0x2A37): heart rate, skin contact, energy expended and R-R intervals |
| `ble_scan()` / `ble_source()` | Bluetooth discovery and streaming with `bleak`, with automatic reconnect |
| `ant_source()` | ANT+ Heart Rate profile with `openant`; derives R-R intervals from beat timing |
| `sim_source()` | Simulated workout with artifacts, for testing |
| `SensorWorker` | Runs the selected source in a background thread so the window never freezes |
| `HeartRateDetector` | The five-stage detection algorithm |
| `TrainingAnalytics` | Zones, TRIMP, calories, averages and time in zone |
| `load_config()` / `write_config()` | Read the saved config at startup and write it (atomically) on Save Config |
| `TrackrApp` | The Tkinter window, session state machine (ready → recording ⇄ paused → ready), chart, config and file saving |
| `TrackrApp._render_report()` | Draws the session report PNG |

**Bluetooth identifiers used:** Heart Rate Service `0x180D`, Heart Rate Measurement `0x2A37`, Battery Level `0x2A19`.

---

## Troubleshooting

| Problem | What to try |
|---|---|
| **Scan finds no strap** | Moisten the electrodes and wear the strap; it sleeps when not on skin. Make sure Bluetooth is on, and close other apps connected to the strap (phone apps, watches), since most straps accept only one Bluetooth connection at a time |
| **"Bluetooth needs the bleak package"** | Run `pip install bleak` in the same Python environment you run the app from |
| **macOS: scan does nothing** | Allow Bluetooth access for your terminal or IDE in System Settings → Privacy & Security → Bluetooth |
| **Linux: Bluetooth errors** | Check `systemctl status bluetooth`; make sure your user can access BlueZ |
| **"ANT+ needs openant…"** | Run `pip install openant` and plug in the ANT+ stick |
| **ANT+ stick not found** | Windows: install the WinUSB driver with Zadig. Linux: add the udev rule from [Installation](#installation). Close Garmin Express or other software using the stick |
| **ANT+ shows HR but HRV stays `--`** | Some `openant` versions name the beat-timing fields differently, so R-R intervals are not received. Heart rate still works; only HRV and the beat-level algorithm stages are affected |
| **Signal is amber or red, or "No contact"** | Wet the electrodes, tighten the strap, and position it just below the chest muscles. Dry skin and loose straps are the most common cause of bad readings, especially in the first minutes |
| **Reading jumps at the start** | Normal for the first few seconds while the algorithm learns your rhythm |
| **"No signal" during a session** | The strap went out of range or lost contact. Bluetooth reconnects automatically; the session keeps running |
| **"Sensor stopped" message** | The connection ended with an error. Press **Stop** to save, then **Start** to reconnect |
| **No module named tkinter** | Linux: `sudo apt install python3-tk`. macOS with Homebrew: `brew install python-tk` |
| **Settings are not remembered** | Press **Save Config** after changing settings; changes are not saved automatically. Check the status bar at startup: it says whether the config was loaded |
| **"Saved config could not be read"** | The config file is damaged. Fix it in a text editor, or just press Save Config to write a new one |
| **Settings differ from what I saved** | A command-line option (for example `--minutes`) overrides the saved value for that run |
| **Name shows as boxes in the report** | The computer has no font for that script. Windows and macOS include Thai fonts; on Linux install one, for example `sudo apt install fonts-thai-tlwg` |
| **Text looks blurry on Windows** | The app turns on high-DPI support automatically; if it still looks blurry, check Windows display scaling settings |

---

## Limitations

- **Not a medical device.** The TRACKR training app is for sport and fitness. Do not use it to diagnose or monitor any medical condition. Consult a doctor before starting a new training programme, especially if you have a heart condition.
- Max HR from age, calories and training load are **estimates**. Entering your measured max HR and resting HR improves the zones.
- The app connects to **one strap at a time**.
- Session files are saved only when you press **Stop** or choose to save when closing the window. If the app is force-quit, the session in progress is lost.
- The ANT+ beat-timing fields depend on the installed `openant` version (see [Troubleshooting](#troubleshooting)).
