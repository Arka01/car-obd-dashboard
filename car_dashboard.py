"""
Live OBD-II race-style dashboard for a Vgate vLinker MC (Bluetooth) on Windows.
Reads live stats from your car and shows what the car's own dashboard doesn't: temperatures,
engine internals, sprint times and more. Works on any OBD-II car; each car's own numbers (tank,
redline, gears...) live in cars/<car>/car.json (see car_profile.py). Set up a new car with
new_car_probe.py.

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
    python car_dashboard.py --car tata_tiago_2023   # pick the car (remembered for next time)
    python car_dashboard.py --port COM6     # use a specific port
    python car_dashboard.py --protocol 6    # skip protocol auto-detect (6 = CAN 11 bit, 500 kbaud)
    python car_dashboard.py --all           # every stat your car supports (as a list)
    python car_dashboard.py --demo          # fake data, no car needed
    python car_dashboard.py --replay cars/tata_tiago_2023/recordings/drive_20261013_1131.csv  # play back
    python car_dashboard.py --debug         # log adapter traffic (to obd.log once the dashboard starts)
    python car_dashboard.py --probe         # read-only scan: which modules answer (AMT?) and what they report
    python car_dashboard.py --refuel 30.5 --full   # after a fill-up: litres from the receipt, filled to cut-off
    python car_dashboard.py --calibrate-gears      # measure each gear from the recorded drives

Every real run (anything except --demo and --replay) is recorded in cars/<car>/recordings/, named by
date and time: drive_<date>_<time>.csv (dashboard) or all_<date>_<time>.csv (--all), plus
drive_<date>_<time>_profile.json with what the car supports, so drives can be analysed and replayed.
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
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from rich.live import Live

import car_profile
from battery_monitor import BatteryMonitor
from car_profile import Car
from fuel_tracker import FuelTracker
from warmup_advisor import WarmUpAdvisor
from dashboard_design import (
    CRITICAL,
    NOT_SUPPORTED,
    WARNING,
    as_number,
    console,
    gear_name,
    print_probe_report,
    minutes,
    render_all,
    render_dashboard,
)

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "obd.log")
ATTEMPTS = 2  # Bluetooth links often fail on the first try and work on the next

# ---------------------------------------------------------------- the car

# The car's own numbers (redline, tank, gears...) come from cars/<car>/car.json: main() picks the car
# and calls use_car(). Until then (and in the demo without any car set up) neutral settings apply.
CAR = Car()
DEMO_GEARS = [149.6, 79.0, 51.4, 36.6, 28.8]  # gearbox for the fake data when the car has none (a Tiago's)


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
    # Where it's drawn: "power" (shift lights, gear, speed, torque...), "meters" (bar meters in POWER), "heat",
    # "mixture" (in FUEL), "status" (bottom bar) or "inputs" (not drawn; used by calculations).
    group: str = "status"
    decimals: int = 1
    short: str = ""  # short name for tight spots (bar meters, status bar)
    empty: str = "waiting..."  # shown while there is no value yet
    fallback: str = None  # command to read instead if the car doesn't support `command`


STATS = [
    # --- power: shift lights, RPM / gear / speed, torque/power and sprint times
    Stat("Speed", "SPEED", "km/h", 0, 200, group="power"),
    Stat("RPM", "RPM", "rpm", 0, 5000, [  # scale: use_car() sets it to end 1000 above the redline
        (lambda v: v > CAR.redline_rpm, CRITICAL, "REDLINE"),
        (lambda v: v > CAR.shift_rpm, WARNING, "SHIFT"),
    ], group="power"),
    Stat("Gear (estimated)", None, "", 0, 5, fmt=gear_name, group="power"),

    # --- heat: coolant + oil in big digits, the other temperatures smaller
    Stat("Coolant", "COOLANT_TEMP", "degC", 0, 130, [
        (lambda v: v > 115, CRITICAL, "OVERHEATING"),
        (lambda v: v > 105, WARNING, "HOT"),
        (lambda v: v < 60, WARNING, "WARMING UP"),
    ], every=10, group="heat", decimals=0),
    Stat("Oil", "OIL_TEMP", "degC", 0, 150, [
        (lambda v: v > 135, CRITICAL, "OVERHEATING"),
        (lambda v: v > 120, WARNING, "HOT"),
        (lambda v: v < 60, WARNING, "COLD"),  # go easy until oil is warm
    ], every=10, group="heat", decimals=0),
    # short = the name shown on screen (the label stays the same: it names the column in recordings)
    Stat("Intake air", "INTAKE_TEMP", "degC", -20, 80, short="AIR TO ENGINE", alerts=[
        (lambda v: v > 60, WARNING, "HOT AIR"),  # heat soak: less power
    ], every=10, group="heat", decimals=0),
    Stat("Outside", "AMBIANT_AIR_TEMP", "degC", -20, 50, every=10, group="heat", decimals=0, short="OUTSIDE AIR"),
    Stat("Heat soak", None, "degC", -10, 40, [  # intake air minus outside air: hot air in = less power
        (lambda v: v > 25, WARNING, "HOT INTAKE"),
    ], group="heat", fmt=lambda v: f"{v:+.0f} °C", short="ENGINE-BAY HEAT"),
    Stat("Catalyst", "CATALYST_TEMP_B1S1", "degC", 0, 1000, [
        (lambda v: v > 900, CRITICAL, "TOO HOT"),
        (lambda v: v < 300, WARNING, "COLD"),  # cleans exhaust only once hot
    ], every=10, group="heat", decimals=0, short="CAT CONVERTER"),

    # --- bar meters in POWER, and the air-fuel mixture (shown in FUEL)
    # Relative versions (PIDs 45 / 5A) are rescaled to a true 0-100 %; the plain ones never drop below ~10-15 %.
    # Cars without them (e.g. a 2021+ Mahindra XUV700 diesel) get the plain ones instead.
    Stat("Throttle", "RELATIVE_THROTTLE_POS", "percent", 0, 100, every=2, group="meters", short="THROTTLE",
         decimals=0, fallback="THROTTLE_POS"),
    Stat("Accelerator pedal", "RELATIVE_ACCEL_POS", "percent", 0, 100, every=2, group="meters", short="PEDAL",
         decimals=0, fallback="ACCELERATOR_POS_D"),
    Stat("Engine load", "ENGINE_LOAD", "percent", 0, 100, every=2, group="meters", short="LOAD", decimals=0),
    Stat("Manifold pressure", "INTAKE_PRESSURE", "kilopascal", 0, 120, every=3, group="meters", short="MANIFOLD",
         decimals=0),
    Stat("Air flow (MAF)", "MAF", "gps", 0, 60, every=3, group="meters", short="AIRFLOW"),
    Stat("Timing advance", "TIMING_ADVANCE", "degree", -10, 50, every=3, center=0, group="meters", short="TIMING",
         decimals=0),
    Stat("Lambda (commanded)", "COMMANDED_EQUIV_RATIO", "ratio", 0.7, 1.3, every=3, center=1.0, group="mixture",
         short="LAMBDA", decimals=2),  # <1 = rich (more fuel, e.g. full throttle), >1 = lean
    Stat("Short fuel trim", "SHORT_FUEL_TRIM_1", "percent", -25, 25, every=3, center=0, group="mixture",
         short="TRIM NOW"),
    Stat("Long fuel trim", "LONG_FUEL_TRIM_1", "percent", -25, 25, [
        (lambda v: abs(v) > 20, CRITICAL, "CHECK"),  # engine constantly correcting the mix
        (lambda v: abs(v) > 10, WARNING, "OFF"),
    ], every=10, center=0, group="mixture", short="TRIM LEARN"),
    Stat("EGR (commanded)", "COMMANDED_EGR", "percent", 0, 100, every=3, group="meters", short="EGR", decimals=0),

    # --- turbo and injection (not drawn by the terminal dashboard; used by per-car web designs and ALL SENSORS)
    # Boost = manifold pressure above the outside air; negative = vacuum (normal on non-turbo petrol engines).
    Stat("Boost", None, "bar", -1.0, 2.0, center=0, group="turbo", short="BOOST", decimals=2),
    Stat("Fuel rail pressure", "FUEL_RAIL_PRESSURE_DIRECT", "bar", 0, 2000, every=2, group="engine", short="RAIL",
         decimals=0),  # common-rail diesel / direct-injection petrol

    # --- fuel: inputs to the fuel tracker (drawn as the FUEL LEFT block, not as tiles)
    Stat("Fuel level", "FUEL_LEVEL", "percent", 0, 100, every=5, group="fuel"),
    Stat("Fuel rate", "FUEL_RATE", "lph", 0, 15, every=2, group="fuel"),  # counting adds rate x time, so 2nd is enough

    # --- the two pedal sensors, read together every 3rd update for the redundancy check (not drawn)
    Stat("Pedal sensor D", "ACCELERATOR_POS_D", "percent", 0, 100, every=3, group="inputs"),
    Stat("Pedal sensor E", "ACCELERATOR_POS_E", "percent", 0, 100, every=3, group="inputs"),

    # --- engine output: raw inputs (not drawn) and the torque/power meters calculated from them
    Stat("Engine torque %", "ACTUAL_TORQUE_PCT", "percent", -125, 130, every=2, group="inputs"),
    Stat("Reference torque", "REFERENCE_TORQUE", "Nm", 0, 500, every=50, group="inputs"),  # a fixed number
    Stat("Torque", None, "Nm", -20, 120, center=0, group="power", short="TORQUE", decimals=0),  # 113 Nm peak
    Stat("Power", None, "bhp", -10, 90, center=0, group="power", short="POWER", decimals=0),  # 84 bhp peak

    # --- status bar: trip and slow-changing car info
    Stat("Acceleration", None, "g", -0.6, 0.6, center=0, decimals=2, group="power", short="ACCEL"),
    Stat("Trip distance", None, "km", 0, 100, decimals=1, group="status", short="TRIP"),
    Stat("Trip avg speed", None, "km/h", 0, 120, group="status", short="AVG", decimals=0),
    Stat("0-60 km/h best", None, "s", 0, 20, empty="--", group="power", short="0-60"),
    Stat("0-100 km/h best", None, "s", 0, 30, empty="--", group="power", short="0-100"),
    Stat("RPM per km/h", None, "", 0, 150, group="inputs"),  # used by --calibrate-gears
    Stat("Engine run time", "RUN_TIME", "second", 0, 3600, every=10, fmt=minutes, short="RUN"),
    Stat("Pedal sensor mismatch", None, "pts", 0, 30, [
        (lambda v: v > 20, CRITICAL, "PEDAL SENSORS DISAGREE"),
        (lambda v: v > 10, WARNING, "PEDAL SENSORS DISAGREE"),
    ], short="PEDAL CHECK", empty="move the pedal fully once"),
    # Battery: two independent readings for the battery monitor (drawn as the BATTERY item, not as tiles)
    Stat("Battery", "CONTROL_MODULE_VOLTAGE", "volt", 10, 16, every=10, group="inputs"),  # engine computer
    Stat("Battery (port)", "ELM_VOLTAGE", "volt", 10, 16, every=2, group="inputs"),  # adapter at the OBD socket
    Stat("Barometric pressure", "BAROMETRIC_PRESSURE", "kilopascal", 70, 105, every=10, short="BARO", decimals=0),
    Stat("Since codes cleared", "DISTANCE_SINCE_DTC_CLEAR", "kilometer", 0, 10000, every=10, decimals=0,
         short="CODES CLEARED"),
    Stat("Driven w/ check-engine", "DISTANCE_W_MIL", "kilometer", 0, 1000, [
        (lambda v: v > 0, WARNING, "LIGHT WAS ON"),
    ], every=10, decimals=0, short="KM W/ CHECK-ENGINE"),
]




def stat_named(label):
    return next(s for s in STATS if s.label == label)


# Read in the same update, because they're compared with each other (the pedal cross-check).
READ_TOGETHER = {"Pedal sensor E": "Pedal sensor D"}


def read_phases(stats):
    """{label: offset}: stats read every Nth update are spread evenly over those N updates.

    Each reading takes ~30 ms over Bluetooth. Reading all the slow stats in the same update made every
    10th update take ~0.7 s instead of ~0.3 s (measured on 15 Oct 2026): a visible stall on the dashboards.
    """
    phases, k = {}, 0
    for stat in stats:
        if stat.command and stat.every > 1 and stat.label not in READ_TOGETHER:
            phases[stat.label] = k % stat.every
            k += 1
    for label, partner in READ_TOGETHER.items():
        phases[label] = phases.get(partner, 0)
    return phases


PHASES = read_phases(STATS)


def due(n):
    """The stats to read in update number n (speed and RPM are in every update). The first update reads
    everything once, so slow stats (e.g. the reference torque, every 50th) don't leave gaps at the start."""
    return [s for s in STATS if s.command and (n == 0 or (n + PHASES.get(s.label, 0)) % s.every == 0)]


def use_car(car):
    """Switch every calculation to this car's settings (redline, gears, tank...)."""
    global CAR
    CAR = car
    stat_named("RPM").high = car.redline_rpm + 1000  # RPM scale ends 1000 above the redline


def fuel_tracker(live):
    """The fuel-left estimate for this car; None if its tank size isn't set.
    The backup estimate from the air the engine takes in assumes petrol, so it's off for other fuels."""
    if not CAR.tank_litres:
        return None
    engine = CAR.engine_litres if CAR.fuel == "petrol" else None
    return FuelTracker(CAR.tank_litres, engine, CAR.volumetric_efficiency,
                       CAR.path("fuel_state.json") if live else None)


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


def is_answer(message, request):
    """True if a reply really answers `request` (b"010C" -> it starts with 0x41).

    Cars can also reply "can't do that now" (a negative response: 7F <service> <reason>), e.g. the
    Mahindra XUV700 right after the engine is switched off. python-OBD 0.7.3 decodes those bytes as
    if they were data (speed 17, coolant -23 °C, 4.35 V...), so every reply is checked here first.
    """
    return bool(message.data) and message.data[0] == int(request[:2], 16) + 0x40


def answered(response, cmd):
    """The car gave a real value for this command (not silence, not a refusal).
    The adapter's own commands (AT..., e.g. its voltage reading) aren't OBD replies: only check they answered."""
    if response.is_null():
        return False
    return cmd.command.upper().startswith(b"AT") or all(is_answer(m, cmd.command) for m in response.messages)


def read_vin(connection):
    """The car's 17-character chassis number (VIN), or None if it doesn't share it over OBD.

    Read with service 09 PID 02 and decoded here: python-OBD 0.7.3's own VIN decoder drops the last
    character. Older (non-CAN) cars send it in several messages, each starting 49 02 <n>.
    """
    import obd
    from obd.protocols import ECU

    def characters(messages):
        return b"".join(bytes(m.data[3:]) for m in messages if len(m.data) > 3 and is_answer(m, b"0902"))

    response = connection.query(obd.OBDCommand("VIN", "Vehicle ID", b"0902", 0, characters, ECU.ENGINE, False),
                                force=True)
    if response.is_null():
        return None
    text = response.value.decode("ascii", errors="ignore")
    vin = re.sub(r"[^A-HJ-NPR-Z0-9]", "", text.upper())[-17:]  # VINs never use I, O or Q
    return vin if len(vin) == 17 else None


def trouble_codes(connection):
    """Stored Diagnostic Trouble Codes (the 'check engine light' codes) as text."""
    import obd

    response = connection.query(obd.commands.GET_DTC)
    codes = response.value if answered(response, obd.commands.GET_DTC) else []
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


# Standard PIDs that python-OBD 0.7.3 doesn't know: name -> (request, reply bytes, formula on data bytes).
# Formulas from Wikipedia's OBD-II PID table. The probe showed the Tiago's engine supports both.
CUSTOM_PIDS = {
    "ACTUAL_TORQUE_PCT": (b"0162", 3, lambda d: d[0] - 125),  # actual engine torque, % of reference
    "REFERENCE_TORQUE": (b"0163", 4, lambda d: d[0] * 256 + d[1]),  # Nm that 100 % stands for
}
CUSTOM_FAILURES = {}  # custom PID -> unanswered requests in a row; after 3 it counts as unsupported


def custom_command(name):
    import obd
    from obd.protocols import ECU

    request, size, formula = CUSTOM_PIDS[name]
    return obd.OBDCommand(name, name, request, size, lambda messages: formula(messages[0].data[2:]), ECU.ENGINE, True)


def command_for(connection, stat):
    """The python-OBD command name to read for a stat on this car: its own, or its fallback if only that is supported."""
    import obd

    if stat.command in CUSTOM_PIDS or not stat.fallback or connection.supports(obd.commands[stat.command]):
        return stat.command
    return stat.fallback if connection.supports(obd.commands[stat.fallback]) else stat.command


def read_stats(connection, stats):
    """{label: number}, or None (no data) / NOT_SUPPORTED (car doesn't report it)."""
    import obd

    values = {}
    for stat in stats:
        if stat.command in CUSTOM_PIDS:
            values[stat.label] = read_custom(connection, stat.command)
            continue
        cmd = obd.commands[command_for(connection, stat)]
        if not connection.supports(cmd):
            values[stat.label] = NOT_SUPPORTED
            continue
        response = connection.query(cmd)
        values[stat.label] = response.value.to(stat.unit).magnitude if answered(response, cmd) else None
    return values


def read_custom(connection, name):
    """python-OBD doesn't track support for these, so just ask, and give up after 3 silent tries."""
    if CUSTOM_FAILURES.get(name, 0) >= 3:
        return NOT_SUPPORTED
    cmd = custom_command(name)
    response = connection.query(cmd, force=True)
    if not answered(response, cmd):
        CUSTOM_FAILURES[name] = CUSTOM_FAILURES.get(name, 0) + 1
        return None
    CUSTOM_FAILURES[name] = 0
    return float(response.value)


def read_everything(connection):
    """Every live-data (mode 01) command this car says it supports, as {description: (text, number)}.

    text is for showing ("44.0 °C"); number is the plain value for recording (None if not a number).
    """
    commands = [
        cmd for cmd in connection.supported_commands
        if cmd.mode == 1 and not cmd.name.startswith("PIDS_")  # PIDS_* only list what's supported
    ]
    values = {}
    for cmd in sorted(commands, key=lambda c: c.pid):
        response = connection.query(cmd)
        text = describe(response.value) if answered(response, cmd) else "no data"
        if text is not None:
            number = response.value.magnitude if hasattr(response.value, "magnitude") else None
            values[cmd.desc] = (text, number)
    return values


class ExtraReader:
    """Everything else the car reports beyond STATS, for the web dashboard's ALL SENSORS panel and the
    recording. Reads one per update, taking turns, so the main stats stay quick.

    read() gives {description: number or text}; units holds each number's unit ("°C", "kPa"...).
    """

    def __init__(self, connection):
        known = {command_for(connection, s) for s in STATS if s.command}  # already read as a dashboard stat
        self.connection = connection
        self.commands = sorted((c for c in connection.supported_commands
                                if c.mode == 1 and not c.name.startswith("PIDS_") and c.name not in known),
                               key=lambda c: c.pid)
        self.units = {}
        self.turn = 0

    @property
    def labels(self):
        return [c.desc for c in self.commands]

    def read(self):
        if not self.commands:
            return {}
        cmd = self.commands[self.turn % len(self.commands)]
        self.turn += 1
        response = self.connection.query(cmd)
        if not answered(response, cmd):
            return {cmd.desc: None}
        value = response.value
        if hasattr(value, "magnitude"):
            self.units[cmd.desc] = f"{value.units:~}"
            return {cmd.desc: float(value.magnitude)}
        text = describe(value)
        if text is None:  # too complex to show (bit fields): stop asking for it
            self.commands.remove(cmd)
            return {}
        return {cmd.desc: text}


class MultiReader:
    """--fast-read: ask for up to 6 live values in one request instead of one request each.

    The OBD standard allows up to 6 PIDs in one service 01 request (still read-only); the engine computer
    answers them back to back in one reply, e.g. 41 0C 1A F8 0D 32 11 2B. Each request costs ~30 ms over
    Bluetooth, so fewer requests mean faster updates.

    The stats go into fixed groups (fastest-changing first: RPM and speed are in group 1, read every
    update). Fixed groups let python-OBD learn how many reply lines each one gets, so the adapter answers
    without waiting for a timeout.

    Guarding the data: check() reads every value the normal way first, then compares each group's
    combined reply with those readings, and only switches on if they agree and it's faster. During the
    drive a garbled reply (unknown PID, too short, a refusal) means that group's values are read the normal
    way instead; after 3 garbled replies in a row the group is switched off for the rest of the run.
    """

    MAX = 6
    MIN_SPEEDUP = 1.5  # not worth it below this

    def __init__(self, connection):
        self.connection = connection
        self.groups = []
        self.note = "off"

    @property
    def on(self):
        return any(not g["off"] for g in self.groups)

    def covers(self, label):
        """True if a working group reads this stat (so the normal schedule skips it)."""
        return any(label in g["labels"] for g in self.groups if not g["off"])

    def _spec(self, stat):
        """(pid, data bytes, decode) for a stat that can go in a combined request, or None."""
        import obd
        from obd.protocols import ECU
        from obd.protocols.protocol import Message

        name = command_for(self.connection, stat)
        if name in CUSTOM_PIDS:
            request, size, formula = CUSTOM_PIDS[name]
            return int(request[2:4], 16), size - 2, lambda chunk: float(formula(chunk))
        cmd = obd.commands[name]
        if cmd.mode != 1 or cmd.pid is None or cmd.bytes <= 2 or not self.connection.supports(cmd):
            return None

        def decode(chunk, cmd=cmd, unit=stat.unit):
            message = Message([])
            message.data, message.ecu = bytearray([0x41, cmd.pid]) + bytearray(chunk), ECU.ENGINE
            return cmd([message]).value.to(unit).magnitude

        return cmd.pid, cmd.bytes - 2, decode

    def _group(self, stats, every, phase):
        import obd
        from obd.protocols import ECU

        specs = {self._spec(s)[0]: (s.label,) + self._spec(s)[1:] for s in stats}
        request = b"01" + "".join(f"{pid:02X}" for pid in specs).encode()
        command = obd.OBDCommand("MULTI_" + request.decode(), "combined read", request, 0, lambda messages: messages,
                                 ECU.ENGINE, True)
        return {"command": command, "specs": specs, "labels": [s.label for s in stats], "every": every,
                "phase": phase, "failures": 0, "off": False}

    def _ask(self, group):
        """{label: value} from one combined request, or None if the reply is garbled.
        Values the car left out are simply missing (the caller reads them the normal way)."""
        response = self.connection.query(group["command"], force=True)
        messages = response.value if not response.is_null() else None
        if not messages:
            return None
        data = bytes(messages[0].data)
        if not data or data[0] != 0x41:
            return None  # silence or a refusal
        values, i = {}, 1
        while i < len(data):
            spec = group["specs"].get(data[i])
            if spec is None:
                if not any(data[i:]):  # padding at the end of the last frame
                    break
                return None  # an unexpected PID: the bytes don't line up
            label, size, decode = spec
            chunk = data[i + 1:i + 1 + size]
            if len(chunk) < size:
                return None  # cut off
            try:
                values[label] = decode(chunk)
            except (TypeError, ValueError, AttributeError, IndexError):
                return None
            i += 1 + size
        return values

    def check(self, stats):
        """Self-test at connect: build the groups and switch on only if they match normal reads and are faster."""
        single, seconds = {}, {}
        for stat in stats:
            if not stat.command or self._spec(stat) is None:
                continue
            started = time.time()
            value = read_stats(self.connection, [stat])[stat.label]
            seconds[stat.label] = time.time() - started
            if as_number(value) is not None:
                single[stat.label] = value
        answering = sorted((s for s in stats if s.label in single), key=lambda s: (s.every, STATS.index(s)))
        if len(answering) < 2:
            self.note = "off: too few values to combine"
            return False

        # Fixed groups of up to 6, keeping stats that are compared with each other together.
        chunks, current = [], []
        for stat in answering:
            if stat.label in READ_TOGETHER.values() and len(current) >= self.MAX - 1:
                chunks.append(current)
                current = []
            if len(current) == self.MAX:
                chunks.append(current)
                current = []
            current.append(stat)
        chunks.append(current)
        per_every = {}
        for chunk in chunks:
            every = min(s.every for s in chunk)  # nothing gets read less often than before
            self.groups.append(self._group(chunk, every, per_every.get(every, 0) % every))
            per_every[every] = per_every.get(every, 0) + 1

        combined_time = single_time = 0.0
        for group in self.groups:
            self._ask(group)  # the first time, python-OBD learns how many reply lines to expect
            started = time.time()
            got = self._ask(group)
            took = time.time() - started
            if got is None or set(got) != set(group["labels"]):
                missing = sorted(set(group["labels"]) - set(got or {}))
                self.groups, self.note = [], f"off: combined reply incomplete ({', '.join(missing) or 'garbled'})"
                return False
            for label, value in got.items():
                stat = stat_named(label)
                if abs(value - single[label]) > max(0.05 * (stat.high - stat.low), 1e-6):
                    self.groups, self.note = [], f"off: {label} read {value:.4g} combined vs {single[label]:.4g} normally"
                    return False
            combined_time += took
            single_time += sum(seconds[label] for label in group["labels"])
        speedup = single_time / combined_time if combined_time else 0
        if speedup < self.MIN_SPEEDUP:
            self.groups, self.note = [], f"off: not faster ({speedup:.1f}x)"
            return False
        self.note = (f"on: {len(answering)} values in {len(self.groups)} requests, "
                     f"{speedup:.1f}x faster than one by one")
        return True

    def read(self, n):
        """(values, asked): values from the groups due in update n, and the labels those groups were asked for."""
        values, asked = {}, set()
        for group in self.groups:
            if group["off"] or (n and (n + group["phase"]) % group["every"]):  # all groups in update 0
                continue
            asked.update(group["labels"])
            got = self._ask(group)
            if got is None:
                group["failures"] += 1
                if group["failures"] >= 3:
                    group["off"] = True
                    logging.getLogger("obd").warning("fast read: group %s switched off after 3 garbled replies",
                                                     group["command"].command)
                continue
            group["failures"] = 0
            values.update(got)
        return values, asked


def start_fast_read(connection):
    """--fast-read: run the self-test and say what came out of it."""
    print("Testing fast read: several values per request (a few seconds) ...")
    multi = MultiReader(connection)
    multi.check(STATS)
    print(f"  fast read {multi.note}")
    return multi


def read_update(connection, n, multi=None):
    """One update's readings: the stats due in update n (see due()), combined into fewer requests if
    --fast-read is on. Anything a combined reply didn't deliver is read the normal way."""
    if not (multi and multi.on):
        return read_stats(connection, due(n))
    values, asked = multi.read(n)
    retry = [s for s in STATS if s.label in asked and s.label not in values]
    others = [s for s in due(n) if not multi.covers(s.label)]
    values.update(read_stats(connection, retry + others))
    return values


def texts(values):
    """{description: text} from read_everything's {description: (text, number)}."""
    return {label: text for label, (text, _) in values.items()}


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

    Starts like a cold start (25 s standing with a fast idle), then repeats a 40 s cycle: accelerate
    from a stop to 110 km/h through the gears, then brake to a stop.
    """
    x = (t - 25) % 40 if t >= 25 else None  # first 25 s: standing, like after a cold start
    accelerating = x is not None and x < 20
    if x is None:
        speed = 0.0
    else:
        speed = 110 * (x / 20) ** 0.8 if accelerating else 110 * (1 - (x - 20) / 20)
    gear = sum(speed > s for s in (0.5, 20, 35, 55, 75))  # 0 = stopped
    fast_idle = max(0.0, 380 - t * 12)  # cold start: idle starts high and settles
    gears = CAR.gears or DEMO_GEARS
    rpm = 860 + fast_idle if gear == 0 else max(900, speed * gears[min(gear, len(gears)) - 1]) + random.uniform(-40, 40)
    throttle = 55 + random.uniform(-5, 5) if accelerating else (0 if x is None else 3)
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
        "Pedal sensor D": 14.9 + throttle * 0.9 * 0.74,  # raw sensors: offset and different ranges, like the real car
        "Pedal sensor E": 7.5 + throttle * 0.9 * 0.37,
        "Engine load": throttle * 1.2 + 15,
        # turbo diesels: boost builds with the throttle; petrol engines run below outside pressure
        "Manifold pressure": 96 + throttle * 1.7 if CAR.fuel == "diesel" else 30 + throttle * 1.1,
        "Fuel rail pressure": 300 + throttle * 22 if CAR.fuel == "diesel" else NOT_SUPPORTED,
        "Air flow (MAF)": rpm / 400 + throttle * 0.25,
        "Timing advance": 30 - throttle * 0.3 + random.uniform(-2, 2),
        "Lambda (commanded)": 0.92 if throttle > 50 else 1.0,
        "Short fuel trim": random.uniform(-6, 6),
        "Long fuel trim": -2.3,
        "EGR (commanded)": NOT_SUPPORTED,
        "Fuel level": 62 - t * 0.01 + random.uniform(-3, 3),  # float sensor sloshes around
        "Fuel rate": 0.8 + throttle * 0.11,  # litres/hour: ~0.8 at idle, ~7 when accelerating
        "Engine torque %": throttle * 1.6 - 5,  # % of reference torque; negative = engine braking
        "Reference torque": 113,
        "Engine run time": t,
        "Battery": 14.0 + random.uniform(-0.1, 0.1),
        "Battery (port)": 14.1 + random.uniform(-0.1, 0.1),
        "Barometric pressure": 96.0,
        "Since codes cleared": 2350,
        "Driven w/ check-engine": 0,
    }


# ---------------------------------------------------------------- trip computer


def estimate_gear(speed, rpm):
    """Gear from the RPM-to-speed ratio; None while shifting (ratio matches no gear), 0 when stopped.
    NOT_SUPPORTED if this car's gears aren't known yet (run --calibrate-gears after a drive)."""
    gears = CAR.gears
    if not gears:
        return NOT_SUPPORTED
    if speed is None or rpm is None:
        return None
    if speed < 5:
        return 0
    if rpm <= 0:  # engine off / stalled while rolling: no gear to estimate
        return None
    ratio = rpm / speed
    best = min(range(len(gears)), key=lambda i: abs(math.log(ratio / gears[i])))
    return best + 1 if abs(math.log(ratio / gears[best])) < 0.15 else None


def engine_output(values):
    """Torque (Nm) and power (PS) from the engine computer's own torque estimate.

    "Actual torque %" is the ECU's modelled output as a percentage of its reference torque, not a
    dyno measurement, but it follows the real engine closely. Power = torque x rpm / 9549 (kW) x 1.341 (bhp).
    Negative values mean engine braking.
    """
    pct, reference = values.get("Engine torque %"), values.get("Reference torque")
    if NOT_SUPPORTED in (pct, reference):
        return {"Torque": NOT_SUPPORTED, "Power": NOT_SUPPORTED}
    pct, reference, rpm = as_number(pct), as_number(reference), as_number(values.get("RPM"))
    if pct is None or not reference:
        return {"Torque": None, "Power": None}
    torque = pct / 100 * reference
    return {"Torque": torque, "Power": torque * rpm / 9549 * 1.341 if rpm is not None else None}


# Calculated values saved in every drive recording too (not only the raw readings), so a dyno-style
# torque & power vs RPM graph can be drawn straight from the file.
RECORDED_CALCULATED = ["Torque", "Power"]


def recording_row(latest):
    """What goes into the recording for one update: the raw readings plus torque (Nm) and power (bhp)."""
    return {**latest, **engine_output(latest)}


def heat_values(values):
    """Heat soak: how much hotter the intake air is than outside (engine-bay heat getting sucked in)."""
    intake, outside = values.get("Intake air"), values.get("Outside")
    if NOT_SUPPORTED in (intake, outside):
        return {"Heat soak": NOT_SUPPORTED}
    intake, outside = as_number(intake), as_number(outside)
    return {"Heat soak": intake - outside if intake is not None and outside is not None else None}


def boost_values(values):
    """Turbo boost in bar: manifold pressure minus barometric (outside air) pressure."""
    manifold, outside = values.get("Manifold pressure"), values.get("Barometric pressure")
    if NOT_SUPPORTED in (manifold, outside):
        return {"Boost": NOT_SUPPORTED}
    manifold, outside = as_number(manifold), as_number(outside)
    return {"Boost": (manifold - outside) / 100 if manifold is not None and outside is not None else None}


HOT_ALERTS = {  # coolant/oil alert text -> engine state shown in HEAT (most severe first)
    "OVERHEATING": {"state": "TOO HOT", "level": "critical", "detail": "ease off / stop safely"},
    "HOT": {"state": "HOT", "level": "warning", "detail": "ease off"},
}


def hot_state(values):
    """TOO HOT / HOT from the coolant and oil alert rules, or None while the engine isn't hot.
    (How far a cold engine has warmed up is the warm-up advisor's job.)"""
    coolant, oil = as_number(values.get("Coolant")), as_number(values.get("Oil"))
    triggered = {text for label, value in (("Coolant", coolant), ("Oil", oil)) if value is not None
                 for test, _, text in stat_named(label).alerts if test(value)}
    for text, state in HOT_ALERTS.items():
        if text in triggered:
            return state
    return None


def speak(sentence):
    """Say a sentence out loud in the background, so the driver needn't look at the screen.

    Windows' built-in voice (System.Speech via PowerShell); spd-say or espeak elsewhere. Never blocks
    and never stops the dashboard: if no voice is available, nothing happens.
    """
    sentence = sentence.replace("'", "").replace('"', "")
    if os.name == "nt":
        command = ["powershell", "-NoProfile", "-Command", "Add-Type -AssemblyName System.Speech; "
                   f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{sentence}')"]
    elif shutil.which("spd-say"):
        command = ["spd-say", sentence]
    elif shutil.which("espeak"):
        command = ["espeak", sentence]
    else:
        return
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # no console flash on Windows
    except OSError:
        pass


class PedalCheck:
    """Cross-checks the two accelerator pedal sensors (PIDs 49 / 4A), like the engine computer does.

    The sensors deliberately don't read the same numbers (different ranges, so one wiring fault
    can't fool both). Each is rescaled by the range it has shown this drive, and the two rescaled
    positions must agree. A mismatch has to last HOLD seconds, because the two are read ~40 ms apart
    and can briefly differ while the pedal is moving. Also stands in for the relative pedal reading
    (PID 5A) if that ever stops answering.
    """

    MIN_TRAVEL = 20  # percent a sensor must have moved before its range means anything
    HOLD = 1.5  # seconds a mismatch must last to count

    def __init__(self):
        self.ranges = {"D": [math.inf, -math.inf], "E": [math.inf, -math.inf]}
        self.mismatch_since = None
        self.reported = None  # mismatch currently shown (points)
        self.last_pair = None

    def update(self, values):
        d, e = values.get("Pedal sensor D"), values.get("Pedal sensor E")
        if NOT_SUPPORTED in (d, e):
            return {"Pedal sensor mismatch": NOT_SUPPORTED}
        d, e = as_number(d), as_number(e)
        out = {"Pedal sensor mismatch": None}
        if d is None or e is None:
            return out
        for key, value in (("D", d), ("E", e)):
            low, high = self.ranges[key]
            self.ranges[key] = [min(low, value), max(high, value)]
        positions = {key: self._position(key, value) for key, value in (("D", d), ("E", e))}
        if as_number(values.get("Accelerator pedal")) is None and positions["D"] is not None:
            out["Accelerator pedal"] = positions["D"]  # fallback when PID 5A isn't answering
        if None in positions.values() or (d, e) == self.last_pair:
            return {**out, "Pedal sensor mismatch": self._held(None)}
        self.last_pair = (d, e)
        out["Pedal sensor mismatch"] = self._held(abs(positions["D"] - positions["E"]))
        return out

    def _position(self, key, value):
        low, high = self.ranges[key]
        return (value - low) / (high - low) * 100 if high - low >= self.MIN_TRAVEL else None

    def _held(self, mismatch):
        """Report the mismatch only once it has lasted HOLD seconds; until then show it as agreeing."""
        now = time.time()
        if mismatch is None:
            return self.reported
        if mismatch > 10:
            self.mismatch_since = self.mismatch_since or now
            self.reported = mismatch if now - self.mismatch_since >= self.HOLD else min(mismatch, 10.0)
        else:
            self.mismatch_since, self.reported = None, mismatch
        return self.reported


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


class Trends:
    """How fast slow values (temperatures) are changing, from the readings of the last minute."""

    WINDOW = 60  # seconds of history to compare against
    MIN_SPAN = 10  # seconds of history needed before showing a trend

    def __init__(self, labels):
        self.history = {label: deque() for label in labels}

    def update(self, values):
        now = time.time()
        for label, history in self.history.items():
            value = as_number(values.get(label))
            if value is None:
                continue
            history.append((now, value))
            while now - history[0][0] > self.WINDOW:
                history.popleft()

    def per_minute(self):
        """{label: change per minute}, or None for labels without enough history yet."""
        rates = {}
        for label, history in self.history.items():
            if len(history) < 2 or history[-1][0] - history[0][0] < self.MIN_SPAN:
                rates[label] = None
                continue
            (t0, v0), (t1, v1) = history[0], history[-1]
            rates[label] = (v1 - v0) / (t1 - t0) * 60
        return rates


# ---------------------------------------------------------------- recording and replay


class Recorder:
    """Saves every raw reading to a CSV file: one row per update, with seconds since the start (t)
    and the clock time. Drive recordings can be played back with --replay.

    labels: the columns; None takes them from the first row written (used for --all).
    """

    def __init__(self, path, labels=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.file = open(self.path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.file)
        self.labels = labels
        if labels is not None:
            self.writer.writerow(["t", "time"] + labels)
        self.start = time.time()

    def write(self, values):
        def cell(value):
            if value is None:
                return ""
            return value if isinstance(value, str) else f"{value:.6g}"

        if self.labels is None:
            self.labels = [k for k in values if not k.startswith("_")]
            self.writer.writerow(["t", "time"] + self.labels)
        now = datetime.now().isoformat(sep=" ", timespec="milliseconds")
        self.writer.writerow([f"{time.time() - self.start:.2f}", now] + [cell(values.get(k)) for k in self.labels])
        self.file.flush()  # keep the file complete even if the laptop sleeps or the script is killed

    def close(self):
        self.file.close()


def save_profile(connection, trouble, path, notes=None):
    """What this car supports + one reading of everything, saved next to the run's recording.
    notes: extra facts about the run, e.g. {"fast_read": "on: ..."}."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    profile = {
        "car": CAR.key,
        "saved": datetime.now().isoformat(timespec="seconds"),
        "port": connection.port_name(),
        "protocol": connection.protocol_name(),
        "trouble_codes": trouble,
        "supported_commands": sorted(c.name for c in connection.supported_commands if c.mode == 1),
        "snapshot": texts(read_everything(connection)),
        **(notes or {}),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    return path


def replay_reader(path):
    """Plays a recorded drive back at its original pace, starting over at the end."""
    def parse(text):
        if text == "":
            return None
        try:
            return float(text)
        except ValueError:
            return text  # NOT_SUPPORTED or a text value

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row.pop("time", None)  # clock time column (recordings made after 2026-10-13)
    frames = [(float(row.pop("t")), {k: parse(v) for k, v in row.items()}) for row in rows]
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


# ---------------------------------------------------------------- probe: who's on the network


GEAR_PID = 0xA4  # standard "Transmission Actual Gear" (newer OBD-II addition; python-OBD 0.7.3 doesn't know it)


def probe(connection, folder):
    """--probe: scan every module (see scan_modules) and save the report as probe_<date>_<time>.json."""
    report = scan_modules(connection)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"probe_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return report, path


def scan_modules(connection):
    """Read-only scan of every module that answers on the car's network.

    Asks all modules (not just the engine) which standard live data they support, tries the
    standard gear PID, reads stored + pending trouble codes and each module's name/software ID.
    Only standard read requests (services 01, 03, 07, 09) are sent; nothing is written or cleared.
    """
    import obd
    from obd.decoders import parse_dtc
    from obd.protocols import ECU

    can_11bit = connection.protocol_id() in ("6", "8")

    def address(tx_id):
        # On 11-bit CAN, module n replies from 7E8 + n; other protocols just report the sender byte.
        return f"{0x7E8 + tx_id:X}" if can_11bit else f"{tx_id:02X}"

    def ask(request):
        """Send one request to every module: {reply address: reply bytes (service + PID echo first)}."""
        def by_module(messages):
            return {address(m.tx_id): bytes(m.data) for m in messages
                    if m.parsed() and m.tx_id is not None and is_answer(m, request.encode())}

        # fast=False: wait for every module to answer, not just the first one.
        command = obd.OBDCommand(f"PROBE_{request}", f"probe {request}", request.encode(), 0, by_module,
                                 ECU.ALL, False)
        return connection.query(command, force=True).value or {}

    def text(data):
        return data.decode("ascii", errors="ignore").strip("\x00 ").strip() or None

    # Which live-data PIDs each module supports: 0100 lists 01-20, 0120 lists 21-40, and so on.
    supported = {}
    for base in range(0x00, 0xE0, 0x20):
        replies = ask(f"01{base:02X}")
        for module, data in replies.items():
            bits = data[2:6]
            pids = supported.setdefault(module, set())
            pids.update(base + i + 1 for i in range(len(bits) * 8) if bits[i // 8] & (0x80 >> (i % 8)))
        if not any(base + 0x20 in pids for pids in supported.values()):
            break  # nobody supports the next range

    gear_replies = ask(f"01{GEAR_PID:02X}")
    stored, pending = ask("03"), ask("07")
    names, calibration = ask("090A"), ask("0904")

    def pid_name(pid):
        if pid == GEAR_PID:
            return "Transmission actual gear"
        if obd.commands.has_pid(1, pid):
            return obd.commands[1][pid].desc
        return f"PID {pid:02X} (not in python-OBD)"

    def codes(replies, module):
        data = replies.get(module, b"")[2:]
        return [code for code, _ in filter(None, (parse_dtc((data[i], data[i + 1]))
                                                  for i in range(0, len(data) - 1, 2)))]

    modules = sorted(set(supported) | set(gear_replies) | set(stored) | set(pending) | set(names))
    report = {
        "saved": datetime.now().isoformat(timespec="seconds"),
        "protocol": connection.protocol_name(),
        "modules": {},
    }
    for module in modules:
        gear_data = gear_replies.get(module)
        report["modules"][module] = {
            "usually": {"7E8": "engine", "7E9": "transmission"}.get(module, "other module") if can_11bit
            else "unknown",
            "name": text(names[module][3:]) if module in names else None,
            "calibration_id": text(calibration[module][3:]) if module in calibration else None,
            # pid % 0x20 skips 20, 40, 60...: those only say "the next range is supported"
            "supported_live_data": [f"{pid:02X} {pid_name(pid)}" for pid in sorted(supported.get(module, ()))
                                    if pid % 0x20],
            "gear_pid_a4": None if gear_data is None else {
                "raw": gear_data[2:].hex(" "),
                # Ratio per Wikipedia's PID table: (256*C + D) / 1000. Gear position encoding unconfirmed.
                "gear_ratio": (gear_data[4] * 256 + gear_data[5]) / 1000 if len(gear_data) >= 6 else None,
            },
            "stored_codes": codes(stored, module),
            "pending_codes": codes(pending, module),
        }
    return report


# ---------------------------------------------------------------- gear calibration from recordings


def calibrate_gears(car, save_without_asking=False):
    """Measure RPM per km/h for each gear from the car's recorded drives (--calibrate-gears).

    While in gear, RPM / speed is fixed by the gearbox, so the readings cluster around one value
    per gear. Shifting, clutch slip and idling fall between the clusters and are filtered out.
    The result can be saved to the car's car.json, which turns on the gear estimate.
    """
    import glob
    import statistics
    import sys
    from collections import Counter

    ratios = []
    for path in sorted(glob.glob(os.path.join(car.recordings, "drive_*.csv"))):
        with open(path, encoding="utf-8") as f:
            previous = None
            for row in csv.DictReader(f):
                try:
                    speed, rpm = float(row["Speed"]), float(row["RPM"])
                except (KeyError, ValueError, TypeError):
                    previous = None
                    continue
                steady = previous and abs(previous[0] - speed) <= 2 and abs(previous[1] - rpm) < 250
                if speed >= 10 and rpm > 900 and steady:
                    ratios.append(rpm / speed)
                previous = (speed, rpm)
    if not ratios:
        print(f"No usable readings in {car.recordings} (record a drive first).")
        return

    # Peaks of a histogram on a log scale (gears are spread evenly in log), 2 % wide bins.
    bins = Counter(round(math.log(r) / 0.02) for r in ratios)
    floor = max(15, 0.01 * len(ratios))
    peaks = [math.exp(b * 0.02) for b, n in bins.items()
             if n >= floor and all(n >= bins.get(b + k, 0) for k in (-2, -1, 1, 2))]
    measured = sorted((statistics.median([r for r in ratios if abs(math.log(r / p)) < 0.08]) for p in peaks),
                      reverse=True)

    theory = car.theoretical_rpm_per_kmh
    print(f"{len(ratios)} in-gear readings from {car.recordings}")
    if theory:
        # Match each measured cluster to the theoretical gear it's closest to; fill unseen gears from theory.
        by_gear = {}
        for value in measured:
            gear = min(range(len(theory)), key=lambda i: abs(math.log(value / theory[i])))
            by_gear[gear] = round(value, 1)
        scale = statistics.median(by_gear[g] / theory[g] for g in by_gear)
        suggested = [by_gear.get(g, round(theory[g] * scale, 1)) for g in range(len(theory))]
        for g in range(len(theory)):
            source = "measured" if g in by_gear else f"not driven in this gear: theory x {scale:.3f}"
            print(f"  gear {g + 1}: {suggested[g]:6.1f} rpm per km/h   ({source})")
    else:
        # No gear ratios known for this car: number the clusters in order. Only right if the drive
        # used every gear from 1st up; a skipped gear shifts the numbers.
        suggested = [round(v, 1) for v in measured]
        for g, value in enumerate(suggested):
            print(f"  gear {g + 1}: {value:6.1f} rpm per km/h   (measured)")
        print("  (numbered in order: check the drive used every gear from 1st, none skipped)")

    if car.folder is None:
        return
    if not save_without_asking:
        if not sys.stdin.isatty():
            print(f"\nNot saved. Run again with --yes to save to cars/{car.key}/car.json.")
            return
        if not confirm(f"\nSave these to cars/{car.key}/car.json? [y/N] "):
            print("Not saved.")
            return
    car.gear_rpm_per_kmh = suggested
    car_profile.save(car)
    print(f"Saved to cars/{car.key}/car.json: the gear estimate now uses them.")


# ---------------------------------------------------------------- main loop


def confirm(question):
    """Yes/no question in the terminal; always "no" when nobody can answer (e.g. run by a script)."""
    import sys

    if not sys.stdin.isatty():
        return False
    return input(question).strip().lower() in ("y", "yes")


def check_car(connection, requested):
    """Make sure readings go to the right car's folder, using the chassis number (VIN) when the car shares it.

    A car with a saved VIN is switched to automatically. A VIN that belongs to no car set up here stops
    the run, so another car's drive never lands in this car's recordings. If the current car has no VIN
    saved yet, it learns this one (with --car, or after asking), so it's recognised from then on.
    """
    vin = read_vin(connection)
    if not vin:
        return True  # this car doesn't share its VIN: trust --car / the remembered car
    known = car_profile.match_vin(vin)
    if known and known != CAR.key:
        print(f"Recognised this car by its chassis number: {known} (switching from {CAR.key}).")
        use_car(car_profile.load(known))
        car_profile.remember(known)
        return True
    if known:
        return True
    if CAR.folder and not CAR.vin and (requested or confirm(f"Is this the {CAR.name}? [y/N] ")):
        with open(CAR.path("vin.txt"), "w", encoding="utf-8") as f:
            f.write(vin + "\n")
        print(f"Saved this car's chassis number to {CAR.key}, to recognise it next time.")
        return True
    print(f"\nThis car's chassis number (VIN) doesn't match {CAR.key} or any other car set up here.")
    print("  New car?   python new_car_probe.py")
    print(f"  Known car? add --car <name>   (set up: {', '.join(car_profile.all_cars())})")
    return False


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
    parser.add_argument("--car", help="Which car (folder name under cars/); remembered for next time")
    parser.add_argument("--port", help="COM port of the adapter, e.g. COM6; auto-detected if omitted")
    parser.add_argument("--protocol",
                        help='ELM327 protocol id, e.g. "6" for CAN 11 bit 500 kbaud; default: from car.json, else auto')
    parser.add_argument(
        "--interval", type=float, default=0.1,
        help="Minimum seconds between refreshes (default 0.1; real rate is limited by how fast the car answers)",
    )
    parser.add_argument("--list-ports", action="store_true", help="List COM ports and exit")
    parser.add_argument("--all", action="store_true", help="Show every stat your car supports")
    parser.add_argument("--demo", action="store_true", help="Use fake data instead of a car")
    parser.add_argument("--replay", metavar="CSV", help="Play back a recorded drive instead of reading the car")
    parser.add_argument("--refuel", type=float, metavar="LITRES",
                        help="Litres just put in (from the pump receipt); use at the start of the drive after refuelling")
    parser.add_argument("--full", action="store_true", help="The tank was filled to the automatic cut-off (full)")
    parser.add_argument("--mute", action="store_true", help="Don't speak the warm-up messages")
    parser.add_argument("--fast-read", action="store_true",
                        help="Try reading up to 6 values per request (self-tested at connect; off if it doesn't help)")
    parser.add_argument("--calibrate-gears", action="store_true",
                        help="Measure each gear's RPM per km/h from the car's recorded drives, then exit")
    parser.add_argument("--yes", action="store_true", help="With --calibrate-gears: save without asking")
    parser.add_argument("--probe", action="store_true",
                        help="Read-only scan of every module on the car's network (finds AMT data), then exit")
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
    live = not (args.demo or args.replay)  # only real drives may change saved state
    # a replay uses the car the recording belongs to (its folder), unless --car says otherwise
    requested = args.car or (car_profile.car_of_recording(args.replay) if args.replay else None)
    use_car(car_profile.choose(requested, demo=not live or args.calibrate_gears))
    if args.calibrate_gears:
        calibrate_gears(CAR, args.yes)
        return

    peaks = {}
    trip = TripComputer()
    pedals = PedalCheck()
    trends = Trends([s.label for s in STATS if s.group == "heat"])
    fuel = fuel_tracker(live)
    if (args.refuel or args.full) and fuel:
        fuel.refuel(args.refuel, args.full)
    elif args.refuel or args.full:
        print(f"Refuel not saved: set tank_litres in cars/{CAR.key}/car.json first.")
    battery = BatteryMonitor(CAR.path("battery_state.json") if live else None)
    advisor = WarmUpAdvisor()
    header = ""

    def dashboard(values, took):
        """Add trip-computer values, remember peaks, recent throttle and temperature trends, then draw."""
        values = {**values, **trip.update(values), **engine_output(values), **pedals.update(values),
                  **heat_values(values), **boost_values(values)}
        trends.update(values)
        if fuel:
            fuel.update(values, trip.distance, trip.accel)
        battery.update(values)
        advisor.update(values)
        sentence = advisor.announcement()
        if sentence and not args.mute:
            speak(sentence)
        warm_up = advisor.report()
        for label, value in values.items():
            if as_number(value) is not None:
                peaks[label] = max(peaks.get(label, value), value)
        engine_state = hot_state(values) or warm_up  # hot beats warming up
        rpm_limit = warm_up["rpm_limit"] if warm_up else None
        return render_dashboard(STATS, values, peaks, trends.per_minute(), fuel.report() if fuel else None,
                                battery.report(), engine_state, rpm_limit, header, took)

    if args.demo:
        start = time.time()
        header = f"DEMO (fake data) · {CAR.name} settings · Trouble codes: none"
        run(lambda: fake_values(time.time() - start), dashboard, max(args.interval, 0.5))
        return

    if args.replay:
        header = f"REPLAY {os.path.basename(args.replay)} · recorded drive, not live"
        run(replay_reader(args.replay), dashboard, max(args.interval, 0.2))
        return

    connection = connect(args.port, args.protocol or CAR.protocol)
    if connection is None:
        return
    if args.probe:
        print("Probing every module on the car's network (read-only, ~20 s) ...")
        report, path = probe(connection, CAR.recordings)
        connection.close()
        print_probe_report(report)
        print(f"\nSaved: {path}")
        return
    if not car_is_answering(connection):
        print(f"\nThe adapter answered on {connection.port_name()}, but the car's engine computer did not.")
        print("Turn the ignition fully ON (or start the engine), wait ~10s, and run again.")
        print(f"(Or this isn't the {CAR.name}: set up a new car with  python new_car_probe.py)")
        connection.close()
        return
    if not check_car(connection, args.car):
        connection.close()
        return

    trouble = trouble_codes(connection)
    header = f"{CAR.name} · {connection.port_name()} · {connection.protocol_name()} · Trouble codes: {trouble}"
    multi = start_fast_read(connection) if args.fast_read and not args.all else None
    if multi and multi.on:
        header += " · FAST READ"
    # Every real run is recorded, named by date and time (to the second, so runs never overwrite).
    name = f"{'all' if args.all else 'drive'}_{datetime.now():%Y%m%d_%H%M%S}"
    print("Saving what this car supports (a few seconds) ...")
    notes = {"fast_read": multi.note} if multi else None
    print(f"  -> {save_profile(connection, trouble, os.path.join(CAR.recordings, f'{name}_profile.json'), notes)}")
    # --all: whatever the car reports; otherwise every stat read from the car plus torque and power
    labels = None if args.all else [s.label for s in STATS if s.command] + RECORDED_CALCULATED
    recorder = Recorder(os.path.join(CAR.recordings, f"{name}.csv"), labels)
    print(f"Recording readings to {recorder.path}")
    header += " · ● REC"

    updates = itertools.count()
    latest = {}

    def read_car():
        """Each stat every `stat.every` updates; in between, its value stays the last reading.

        While the engine is off, only voltage and RPM are read (plus everything every 20th update),
        so the dip when the engine is started is sampled ~10 times a second instead of every few.
        """
        n = next(updates)
        if battery.engine_off() and n % 20:
            wanted = [s for s in STATS if s.label in ("Battery (port)", "RPM")]
            fresh = read_stats(connection, wanted)
        else:
            fresh = read_update(connection, n, multi)
        latest.update(fresh)
        now = time.time()
        latest["_read_at"] = {**latest.get("_read_at", {}), **{label: now for label in fresh}}  # for freshness checks
        recorder.write(recording_row(latest))
        return dict(latest)

    log_to_file()
    print(f"Connected. Adapter messages now go to {LOG_PATH}")
    try:
        if args.all:
            def read_all_and_record():
                values = read_everything(connection)
                recorder.write({label: text if number is None else number
                                for label, (text, number) in values.items()})
                return texts(values)

            run(read_all_and_record, lambda v, took: render_all(v, header, took), args.interval)
        else:
            run(read_car, dashboard, args.interval)
    finally:
        connection.close()
        if fuel:
            fuel.save()
        recorder.close()
        print(f"Run saved: {recorder.path}")


if __name__ == "__main__":
    main()
