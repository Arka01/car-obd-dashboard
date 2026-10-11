"""
Live OBD-II race-style dashboard for a Vgate vLinker MC (Bluetooth) on Windows.
Reads live stats from your car and shows what the car's own dashboard doesn't: temperatures,
engine internals, sprint times and more. Tuned for a 2023 Tata Tiago 1.2 petrol AMT
(see CAR SETTINGS below); works on any OBD-II car.

This file is the logic: which stats to read, talking to the adapter, the trip computer,
recording/replay and the main loop. How things look lives in dashboard_design.py.

One-time setup
--------------
1. pip install obd rich
2. Plug the vLinker MC into the car's OBD port, turn ignition ON (engine on or off).
3. Windows Settings -> Bluetooth & devices -> Add device -> Bluetooth.
   If "vLinker MC" doesn't show up, press the button on the adapter. Pairing PIN: 1234
4. Windows creates a COM port for it. Find it with:
       python car_dashboard.py --list-ports
   (look for "Standard Serial over Bluetooth link")

Run
---
    python car_dashboard.py                 # auto-find the vLinker's Bluetooth COM port
    python car_dashboard.py --port COM6     # use a specific port
    python car_dashboard.py --protocol 6    # skip protocol auto-detect (6 = CAN 11 bit, 500 kbaud)
    python car_dashboard.py --all           # every stat your car supports (as a list)
    python car_dashboard.py --demo          # fake data, no car needed
    python car_dashboard.py --replay recordings/drive_20261009_1915.csv   # play a recorded drive back
    python car_dashboard.py --debug         # log adapter traffic (to obd.log once the dashboard starts)

Every connected run records its readings to recordings/drive_<date>_<time>.csv and saves what the
car supports to recordings/car_profile.json, so drives can be replayed at home without the car.
Ctrl+C quits.
"""

import argparse
import csv
import itertools
import json
import logging
import math
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from rich.live import Live

from dashboard_design import (
    CRITICAL,
    NOT_SUPPORTED,
    WARNING,
    as_number,
    console,
    gear_name,
    minutes,
    render_all,
    render_dashboard,
)

HERE = os.path.dirname(os.path.abspath(__file__))
RECORD_DIR = os.path.join(HERE, "recordings")
LOG_PATH = os.path.join(HERE, "obd.log")
ATTEMPTS = 2  # Bluetooth links often fail on the first try and work on the next

# ---------------------------------------------------------------- CAR SETTINGS (assumed; tweak for your car)

SHIFT_RPM, REDLINE_RPM = 3500, 4000  # RPM sweep turns amber / red
# Gear estimate. Tata doesn't publish the Tiago's gear ratios; these come from an engineering
# exercise that assumed them (LinkedIn, Suriaa). Tyre size 175/60 R15 is from CarWale/CarDekho.
GEAR_RATIOS = [3.73, 2.05, 1.32, 0.94, 0.74]
FINAL_DRIVE = 4.06
TYRE_WIDTH_MM, TYRE_PROFILE_PCT, RIM_INCH = 175, 60, 15
TYRE_CIRCUMFERENCE_M = math.pi * (RIM_INCH * 25.4 + 2 * TYRE_WIDTH_MM * TYRE_PROFILE_PCT / 100) / 1000
# Engine rpm per 1 km/h in each gear: wheel turns per minute at 1 km/h x gearbox ratio x final drive.
GEAR_RPM_PER_KMH = [r * FINAL_DRIVE * (1000 / 60) / TYRE_CIRCUMFERENCE_M for r in GEAR_RATIOS]
# Calibrate: drive steadily in each gear, note "RPM per km/h" (in a recording or with --all), and
# replace the list above with your measured numbers, e.g. GEAR_RPM_PER_KMH = [136, 75, 48, 34, 27].


# ---------------------------------------------------------------- the stats


@dataclass
class Stat:
    label: str
    command: str  # python-OBD command name; None = calculated by the trip computer
    unit: str  # unit python-OBD converts to (or the display unit for calculated stats)
    low: float  # gauge range
    high: float
    # (test, level, text) checked in order; first match wins, otherwise OK.
    # Thresholds are typical petrol-car values; tweak them for your car.
    alerts: list = field(default_factory=list)
    every: int = 1  # read every Nth update; slow-changing stats less often keeps fast ones snappy
    center: float = None  # bars grow up/down from this value, e.g. 0 for fuel trims
    fmt: object = None  # custom value -> text, instead of the number + unit
    group: str = "car"  # where it's drawn: "cockpit", "temps", "engine", "trip" or "car"
    decimals: int = 1
    short: str = ""  # short name for tight spots (engine meters, footer line)
    empty: str = "waiting..."  # shown while there is no value yet


STATS = [
    # --- cockpit: RPM sweep, gear and speed in the middle of the dash
    Stat("Speed", "SPEED", "km/h", 0, 200, group="cockpit"),
    Stat("RPM", "RPM", "rpm", 0, REDLINE_RPM + 1000, [  # scale ends 1000 above redline
        (lambda v: v > REDLINE_RPM, CRITICAL, "REDLINE"),
        (lambda v: v > SHIFT_RPM, WARNING, "SHIFT"),
    ], group="cockpit"),
    Stat("Gear (estimated)", None, "", 0, 5, fmt=gear_name, group="cockpit"),

    # --- temperatures: left column, coolant + oil in big digits
    Stat("Coolant", "COOLANT_TEMP", "degC", 0, 130, [
        (lambda v: v > 115, CRITICAL, "OVERHEATING"),
        (lambda v: v > 105, WARNING, "HOT"),
        (lambda v: v < 60, WARNING, "WARMING UP"),
    ], every=10, group="temps", decimals=0),
    Stat("Oil", "OIL_TEMP", "degC", 0, 150, [
        (lambda v: v > 135, CRITICAL, "OVERHEATING"),
        (lambda v: v > 120, WARNING, "HOT"),
        (lambda v: v < 60, WARNING, "COLD"),  # go easy until oil is warm
    ], every=10, group="temps", decimals=0),
    Stat("Intake air", "INTAKE_TEMP", "degC", -20, 80, [
        (lambda v: v > 60, WARNING, "HOT AIR"),  # heat soak: less power
    ], every=10, group="temps", decimals=0),
    Stat("Outside", "AMBIANT_AIR_TEMP", "degC", -20, 50, every=10, group="temps", decimals=0),
    Stat("Catalyst", "CATALYST_TEMP_B1S1", "degC", 0, 1000, [
        (lambda v: v > 900, CRITICAL, "TOO HOT"),
        (lambda v: v < 300, WARNING, "COLD"),  # cleans exhaust only once hot
    ], every=10, group="temps", decimals=0),

    # --- engine: vertical bar meters below the dash
    Stat("Throttle", "THROTTLE_POS", "percent", 0, 100, group="engine", short="THROTTLE", decimals=0),
    Stat("Accelerator pedal", "ACCELERATOR_POS_D", "percent", 0, 100, group="engine", short="PEDAL", decimals=0),
    Stat("Engine load", "ENGINE_LOAD", "percent", 0, 100, group="engine", short="LOAD", decimals=0),
    Stat("Manifold pressure", "INTAKE_PRESSURE", "kilopascal", 0, 120, every=2, group="engine", short="MANIFOLD",
         decimals=0),
    Stat("Air flow (MAF)", "MAF", "gps", 0, 60, every=2, group="engine", short="AIR FLOW"),
    Stat("Timing advance", "TIMING_ADVANCE", "degree", -10, 50, every=2, center=0, group="engine", short="TIMING",
         decimals=0),
    Stat("Lambda (commanded)", "COMMANDED_EQUIV_RATIO", "ratio", 0.7, 1.3, every=2, center=1.0, group="engine",
         short="LAMBDA", decimals=2),  # <1 = rich (more fuel, e.g. full throttle), >1 = lean
    Stat("Short fuel trim", "SHORT_FUEL_TRIM_1", "percent", -25, 25, every=2, center=0, group="engine",
         short="TRIM NOW"),
    Stat("Long fuel trim", "LONG_FUEL_TRIM_1", "percent", -25, 25, [
        (lambda v: abs(v) > 20, CRITICAL, "CHECK"),  # engine constantly correcting the mix
        (lambda v: abs(v) > 10, WARNING, "OFF"),
    ], every=10, center=0, group="engine", short="TRIM LEARN"),
    Stat("EGR (commanded)", "COMMANDED_EGR", "percent", 0, 100, every=2, group="engine", short="EGR", decimals=0),

    # --- trip (calculated, right column) and car (slow-changing, footer line)
    Stat("Acceleration", None, "g", -0.6, 0.6, center=0, decimals=2, group="trip"),
    Stat("Trip distance", None, "km", 0, 100, decimals=2, group="trip"),
    Stat("Trip avg speed", None, "km/h", 0, 120, group="trip"),
    Stat("0-60 km/h best", None, "s", 0, 20, empty="stop, then floor it", group="trip"),
    Stat("0-100 km/h best", None, "s", 0, 30, empty="stop, then floor it", group="trip"),
    Stat("RPM per km/h", None, "", 0, 150, empty="drive > 5 km/h", group="trip"),  # for gear calibration
    Stat("Engine run time", "RUN_TIME", "second", 0, 3600, every=10, fmt=minutes, short="RUN"),
    Stat("Battery", "CONTROL_MODULE_VOLTAGE", "volt", 10, 16, [
        (lambda v: v < 11.8, CRITICAL, "VERY LOW"),
        (lambda v: v < 12.2, WARNING, "LOW"),
        (lambda v: v > 15.0, WARNING, "OVERCHARGING"),
    ], every=10, short="BATTERY"),
    Stat("Barometric pressure", "BAROMETRIC_PRESSURE", "kilopascal", 70, 105, every=10, short="BARO", decimals=0),
    Stat("Since codes cleared", "DISTANCE_SINCE_DTC_CLEAR", "kilometer", 0, 10000, every=10, decimals=0,
         short="CODES CLEARED"),
    Stat("Driven w/ check-engine", "DISTANCE_W_MIL", "kilometer", 0, 1000, [
        (lambda v: v > 0, WARNING, "LIGHT WAS ON"),
    ], every=10, decimals=0, short="KM W/ CHECK-ENGINE"),
]


# ---------------------------------------------------------------- connecting to the adapter


def bluetooth_address(hwid):
    """Address of the remote device from a Bluetooth COM port's hardware id.

    Windows hwids look like BTHENUM\\{...}_LOCALMFG&0002\\7&2D1E&0&001DA5F1C2B3_C00000000.
    Outgoing ports (the ones that talk to the adapter) carry the device's address;
    incoming ports have 000000000000 and never answer.
    """
    match = re.search(r"&([0-9A-F]{12})_", hwid.upper())
    return match.group(1) if match else None


def bluetooth_ports():
    """Outgoing COM ports Windows created for paired Bluetooth serial devices."""
    from serial.tools import list_ports  # installed together with `obd`

    ports = [p for p in list_ports.comports() if "bluetooth" in p.description.lower()]
    outgoing = [p.device for p in ports if bluetooth_address(p.hwid) not in (None, "0" * 12)]
    return outgoing or [p.device for p in ports]  # fall back if the hwid format is unexpected


def print_ports():
    from serial.tools import list_ports

    ports = list_ports.comports()
    if not ports:
        print("No COM ports found. Is the vLinker paired in Windows Bluetooth settings?")
    for p in ports:
        address = bluetooth_address(p.hwid)
        if address == "0" * 12:
            kind = "incoming - not usable"
        elif address:
            kind = "outgoing to device " + ":".join(address[i:i + 2] for i in range(0, 12, 2))
        else:
            kind = ""
        print(f"{p.device:<8}{p.description:<45}{kind}")


def connect(port, protocol=None):
    """Open a connection to the adapter. Tries every Bluetooth COM port if none is given.

    protocol: ELM327 protocol id (e.g. "6" = ISO 15765-4 CAN 11 bit 500 kbaud); None = auto-detect.
    """
    import obd

    # Windows usually creates two COM ports per Bluetooth device (incoming + outgoing);
    # only one of them talks to the adapter, so try each.
    candidates = [port] if port else bluetooth_ports()
    if not candidates:
        print("No Bluetooth COM port found. Pair the vLinker MC first, or pass --port COMx.")
        return None
    if not port:
        print(f"Bluetooth COM ports found: {', '.join(candidates)}")

    for candidate in candidates:
        for attempt in range(1, ATTEMPTS + 1):
            # Opening the port makes Windows connect to the adapter over Bluetooth;
            # if the adapter is asleep or busy this hangs until Windows gives up (~30s).
            print(f"Trying {candidate}, attempt {attempt}/{ATTEMPTS} (can take up to ~30s) ...")
            # Bluetooth COM ports ignore baud rate; fixing it skips flaky auto-detection.
            # fast=True: after the first reply, tell the adapter how many replies to expect so it
            # answers immediately instead of waiting for other ECUs (big latency win).
            # A longer timeout suits Bluetooth.
            connection = obd.OBD(candidate, baudrate=38400, protocol=protocol, fast=True, timeout=2)
            if connection.is_connected():
                return connection
            status = connection.status()
            print(f"  {candidate}: {status}")
            connection.close()
            # Only a failed Bluetooth link is worth retrying; if the adapter answered, it won't change.
            if status != obd.OBDStatus.NOT_CONNECTED:
                break

    print("\nCould not connect. Check:")
    print("  - adapter LED is on (press its button to wake it from battery-saver sleep)")
    print("  - your phone's Bluetooth is off, so no other app is holding the adapter")
    print("  - ignition ON; if still failing, toggle the laptop's Bluetooth or re-plug the adapter")
    return None


def car_is_answering(connection):
    """The adapter can answer while the car's engine computer stays silent ("NO DATA"),
    e.g. with the ignition off or only in accessory mode. Then no live stats are listed."""
    return any(cmd.mode == 1 and not cmd.name.startswith("PIDS_") for cmd in connection.supported_commands)


def trouble_codes(connection):
    """Stored Diagnostic Trouble Codes (the 'check engine light' codes) as text."""
    import obd

    response = connection.query(obd.commands.GET_DTC)
    codes = [] if response.is_null() else response.value
    return ", ".join(f"{c} ({d})" for c, d in codes) or "none"


def log_to_file():
    """Move python-OBD's log lines from the screen to obd.log, so they don't scribble over the dashboard."""
    import obd

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
    for handler in list(obd.logger.handlers):
        obd.logger.removeHandler(handler)
    obd.logger.addHandler(file_handler)


# ---------------------------------------------------------------- reading values


def read_stats(connection, stats):
    """{label: number}, or None (no data) / NOT_SUPPORTED (car doesn't report it)."""
    import obd

    values = {}
    for stat in stats:
        cmd = obd.commands[stat.command]
        if not connection.supports(cmd):
            values[stat.label] = NOT_SUPPORTED
            continue
        response = connection.query(cmd)
        values[stat.label] = None if response.is_null() else response.value.to(stat.unit).magnitude
    return values


def read_everything(connection):
    """Every live-data (mode 01) command this car says it supports, as {description: text}."""
    commands = [
        cmd for cmd in connection.supported_commands
        if cmd.mode == 1 and not cmd.name.startswith("PIDS_")  # PIDS_* only list what's supported
    ]
    values = {}
    for cmd in sorted(commands, key=lambda c: c.pid):
        response = connection.query(cmd)
        text = "no data" if response.is_null() else describe(response.value)
        if text is not None:
            values[cmd.desc] = text
    return values


def describe(value):
    """Turn a python-OBD value into short text; None for values too complex to show."""
    if hasattr(value, "magnitude"):  # a number with a unit, e.g. 40 degC
        return f"{value:.1f~}"
    if isinstance(value, str):
        return value
    if hasattr(value, "MIL"):  # engine status: check-engine light + number of codes
        return f"check-engine light {'ON' if value.MIL else 'off'}, {value.DTC_count} codes"
    if isinstance(value, tuple) and all(isinstance(v, str) for v in value):
        return ", ".join(v for v in value if v)
    return None


def fake_values(t):
    """Believable fake values so the dashboard can be tried without a car.

    Repeats a 40 s cycle: accelerate from a stop to 110 km/h through the gears, then brake to a stop.
    """
    x = t % 40
    accelerating = x < 20
    speed = 110 * (x / 20) ** 0.8 if accelerating else 110 * (1 - (x - 20) / 20)
    gear = sum(speed > s for s in (0.5, 20, 35, 55, 75))  # 0 = stopped
    rpm = 800 if gear == 0 else max(900, speed * GEAR_RPM_PER_KMH[gear - 1]) + random.uniform(-40, 40)
    throttle = 55 + random.uniform(-5, 5) if accelerating else 3
    return {
        "Speed": speed,
        "RPM": rpm,
        "Coolant": min(92, 25 + t * 1.5),
        "Oil": min(105, 22 + t * 1.0),
        "Intake air": 34 + random.uniform(-0.5, 0.5),
        "Outside": NOT_SUPPORTED,  # shows how unsupported stats get hidden
        "Catalyst": min(650, 100 + t * 12),
        "Throttle": throttle,
        "Accelerator pedal": throttle * 0.9,
        "Engine load": throttle * 1.2 + 15,
        "Manifold pressure": 30 + throttle * 1.1,
        "Air flow (MAF)": rpm / 400 + throttle * 0.25,
        "Timing advance": 30 - throttle * 0.3 + random.uniform(-2, 2),
        "Lambda (commanded)": 0.92 if throttle > 50 else 1.0,
        "Short fuel trim": random.uniform(-6, 6),
        "Long fuel trim": -2.3,
        "EGR (commanded)": NOT_SUPPORTED,
        "Engine run time": t,
        "Battery": 14.1 + random.uniform(-0.1, 0.1),
        "Barometric pressure": 96.0,
        "Since codes cleared": 2350,
        "Driven w/ check-engine": 0,
    }


# ---------------------------------------------------------------- trip computer


def estimate_gear(speed, rpm):
    """Gear from the RPM-to-speed ratio; None while shifting (ratio matches no gear), 0 when stopped."""
    if speed is None or rpm is None:
        return None
    if speed < 5:
        return 0
    if rpm <= 0:  # engine off / stalled while rolling: no gear to estimate
        return None
    ratio = rpm / speed
    best = min(range(len(GEAR_RPM_PER_KMH)), key=lambda i: abs(math.log(ratio / GEAR_RPM_PER_KMH[i])))
    return best + 1 if abs(math.log(ratio / GEAR_RPM_PER_KMH[best])) < 0.15 else None


class TripComputer:
    """Values calculated from what the car reports: gear, acceleration, trip distance, sprint times."""

    SPRINTS = (60, 100)  # km/h targets for the standing-start timers

    def __init__(self):
        self.start = self.last_time = time.time()
        self.distance = 0.0  # km
        self.last_speed = None
        self.accel = 0.0  # smoothed, in g
        self.sprint_start = None  # time the car last stood still, while a sprint is possible
        self.sprint_done = set()
        self.best = {}  # target km/h -> best seconds

    def update(self, values):
        now = time.time()
        dt, self.last_time = now - self.last_time, now
        speed, rpm = as_number(values.get("Speed")), as_number(values.get("RPM"))

        if speed is not None:
            self.distance += speed * dt / 3600
            if self.last_speed is not None and dt > 0:
                g = (speed - self.last_speed) / 3.6 / dt / 9.81
                self.accel = 0.6 * self.accel + 0.4 * g  # smoothed: speed only comes in whole km/h
            self.track_sprint(speed, now)
            self.last_speed = speed

        hours = (now - self.start) / 3600
        return {
            "Gear (estimated)": estimate_gear(speed, rpm),
            "Acceleration": self.accel if speed is not None else None,
            "Trip distance": self.distance,
            "Trip avg speed": self.distance / hours if hours > 0 else None,
            "0-60 km/h best": self.best.get(60),
            "0-100 km/h best": self.best.get(100),
            "RPM per km/h": rpm / speed if rpm is not None and speed and speed > 5 else None,
        }

    def track_sprint(self, speed, now):
        """Standing-start timers: armed while stopped, timed until each target speed is reached."""
        if speed < 1:
            self.sprint_start, self.sprint_done = now, set()
            return
        if self.sprint_start is None:
            return
        if self.last_speed is not None and speed < self.last_speed - 3:  # slowed down: run aborted
            self.sprint_start = None
            return
        for target in self.SPRINTS:
            if target not in self.sprint_done and speed >= target:
                seconds = now - self.sprint_start
                self.best[target] = min(self.best.get(target, seconds), seconds)
                self.sprint_done.add(target)


# ---------------------------------------------------------------- recording and replay


class Recorder:
    """Saves every raw reading to a CSV file, so a drive can be replayed later with --replay."""

    def __init__(self):
        os.makedirs(RECORD_DIR, exist_ok=True)
        self.path = os.path.join(RECORD_DIR, f"drive_{datetime.now():%Y%m%d_%H%M}.csv")
        self.file = open(self.path, "w", newline="", encoding="utf-8")
        self.labels = [s.label for s in STATS if s.command]
        self.writer = csv.writer(self.file)
        self.writer.writerow(["t"] + self.labels)
        self.start = time.time()

    def write(self, values):
        def cell(value):
            if value is None:
                return ""
            return value if isinstance(value, str) else f"{value:.6g}"

        self.writer.writerow([f"{time.time() - self.start:.2f}"] + [cell(values.get(k)) for k in self.labels])
        self.file.flush()  # keep the file complete even if the laptop sleeps or the script is killed

    def close(self):
        self.file.close()


def save_profile(connection, trouble):
    """What this car supports + one reading of everything, for designing without the car."""
    os.makedirs(RECORD_DIR, exist_ok=True)
    path = os.path.join(RECORD_DIR, "car_profile.json")
    profile = {
        "saved": datetime.now().isoformat(timespec="seconds"),
        "port": connection.port_name(),
        "protocol": connection.protocol_name(),
        "trouble_codes": trouble,
        "supported_commands": sorted(c.name for c in connection.supported_commands if c.mode == 1),
        "snapshot": read_everything(connection),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    return path


def replay_reader(path):
    """Plays a recorded drive back at its original pace, starting over at the end."""
    def parse(text):
        if text == "":
            return None
        return text if text == NOT_SUPPORTED else float(text)

    with open(path, newline="", encoding="utf-8") as f:
        frames = [(float(row.pop("t")), {k: parse(v) for k, v in row.items()}) for row in csv.DictReader(f)]
    if not frames:
        raise SystemExit(f"{path} has no readings.")
    state = {"start": time.time(), "i": 0}

    def read():
        elapsed = time.time() - state["start"]
        while state["i"] + 1 < len(frames) and frames[state["i"] + 1][0] <= elapsed:
            state["i"] += 1
        if state["i"] + 1 == len(frames) and elapsed > frames[-1][0] + 2:  # end reached: loop
            state["start"], state["i"] = time.time(), 0
        return frames[state["i"]][1]

    return read


# ---------------------------------------------------------------- main loop


def run(read, render, interval):
    """Refresh loop: read values, redraw the screen, repeat until Ctrl+C."""
    try:
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                started = time.time()
                values = read()
                took = time.time() - started
                live.update(render(values, took), refresh=True)
                time.sleep(max(0, interval - took))  # reading already took time; don't add it twice
    except KeyboardInterrupt:
        pass


def parse_args():
    parser = argparse.ArgumentParser(description="Show live OBD-II car stats")
    parser.add_argument("--port", help="COM port of the adapter, e.g. COM6; auto-detected if omitted")
    parser.add_argument("--protocol", help='ELM327 protocol id, e.g. "6" for CAN 11 bit 500 kbaud; auto if omitted')
    parser.add_argument(
        "--interval", type=float, default=0.1,
        help="Minimum seconds between refreshes (default 0.1; real rate is limited by how fast the car answers)",
    )
    parser.add_argument("--list-ports", action="store_true", help="List COM ports and exit")
    parser.add_argument("--all", action="store_true", help="Show every stat your car supports")
    parser.add_argument("--demo", action="store_true", help="Use fake data instead of a car")
    parser.add_argument("--replay", metavar="CSV", help="Play back a recorded drive instead of reading the car")
    parser.add_argument("--no-record", action="store_true", help="Don't save readings to the recordings folder")
    parser.add_argument(
        "--debug", action="store_true",
        help="Log every message sent to/from the adapter (on screen while connecting, then to obd.log)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.debug:
        import obd

        obd.logger.setLevel(obd.logging.DEBUG)
    if args.list_ports:
        print_ports()
        return

    throttle_history = deque(maxlen=400)
    peaks = {}
    trip = TripComputer()
    header = ""

    def dashboard(values, took):
        """Add trip-computer values, remember peaks and recent throttle, then draw."""
        values = {**values, **trip.update(values)}
        throttle_history.append(values.get("Throttle"))
        for label, value in values.items():
            if as_number(value) is not None:
                peaks[label] = max(peaks.get(label, value), value)
        return render_dashboard(STATS, values, peaks, throttle_history, header, took)

    if args.demo:
        start = time.time()
        header = "DEMO (fake data) · Trouble codes: none"
        run(lambda: fake_values(time.time() - start), dashboard, max(args.interval, 0.5))
        return

    if args.replay:
        header = f"REPLAY {os.path.basename(args.replay)} · recorded drive, not live"
        run(replay_reader(args.replay), dashboard, max(args.interval, 0.2))
        return

    connection = connect(args.port, args.protocol)
    if connection is None:
        return
    if not car_is_answering(connection):
        print(f"\nThe adapter answered on {connection.port_name()}, but the car's engine computer did not.")
        print("Turn the ignition fully ON (or start the engine), wait ~10s, and run again.")
        connection.close()
        return

    trouble = trouble_codes(connection)
    header = f"{connection.port_name()} · {connection.protocol_name()} · Trouble codes: {trouble}"
    recorder = None
    if not args.no_record and not args.all:
        print("Saving what this car supports (a few seconds) ...")
        print(f"  -> {save_profile(connection, trouble)}")
        recorder = Recorder()
        print(f"Recording readings to {recorder.path}")
        header += " · ● REC"

    updates = itertools.count()
    latest = {}

    def read_car():
        """Each stat every `stat.every` updates; in between, its value stays the last reading."""
        n = next(updates)
        latest.update(read_stats(connection, [s for s in STATS if s.command and n % s.every == 0]))
        if recorder:
            recorder.write(latest)
        return dict(latest)

    log_to_file()
    print(f"Connected. Adapter messages now go to {LOG_PATH}")
    try:
        if args.all:
            run(lambda: read_everything(connection), lambda v, took: render_all(v, header, took), args.interval)
        else:
            run(read_car, dashboard, args.interval)
    finally:
        connection.close()
        if recorder:
            recorder.close()
            print(f"Drive saved: {recorder.path}")


if __name__ == "__main__":
    main()
