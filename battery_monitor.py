"""
Battery and charging health, from two independent voltage readings.

- The adapter's own port voltage (ELM327 "AT RV"): measured by the vLinker at the OBD socket, so it
  is answered quickly and keeps working while the engine computer is busy.
- The engine computer's supply voltage (PID 42): measured inside the ECU, read less often.

What a voltage means depends on what the engine is doing, so the reading is turned into a state:

- Engine running: CHARGING (alternator OK), LOW CHARGE, NOT CHARGING (alternator problem: the car
  will run on the battery until it dies, typically at night with lights and fan on) or OVERCHARGING.
- Engine off: SETTLING right after stopping (the "surface charge" left by charging makes the
  voltage slide down slowly for minutes), then RESTING with an estimated state of charge.
- Each engine start: the lowest voltage while cranking, the classic battery health test. Results
  are kept in a JSON file so a battery that's slowly weakening shows up over weeks.

Thresholds are typical 12 V lead-acid values (general knowledge), not Tata specifications. Levels
are returned as "good" / "warning" / "critical"; how they look is up to the dashboard.
"""

import json
import os
import time
from collections import deque
from datetime import datetime

# Resting (open-circuit) voltage -> state of charge, lead-acid at ~25 °C. Only meaningful after the
# battery has rested; with the ignition on there is some load, so it reads a little low.
RESTING_CHARGE = [(11.89, 0), (12.06, 25), (12.24, 50), (12.45, 75), (12.65, 100)]


def _number(value):
    return value if isinstance(value, (int, float)) else None


def charge_percent(volts):
    """Estimated state of charge for a rested battery, by straight lines between the table points."""
    if volts <= RESTING_CHARGE[0][0]:
        return 0.0
    for (v0, p0), (v1, p1) in zip(RESTING_CHARGE, RESTING_CHARGE[1:]):
        if volts <= v1:
            return p0 + (p1 - p0) * (volts - v0) / (v1 - v0)
    return 100.0


class BatteryMonitor:
    RUNNING_RPM = 400  # above this the engine counts as running
    STOPPED_RPM = 100
    CHARGING_MIN = 13.0  # volts while running: below this (sustained) is a warning...
    NOT_CHARGING = 12.6  # ...and below this the alternator isn't charging at all
    OVERCHARGE_WARNING, OVERCHARGE_CRITICAL = 15.0, 15.5
    WARNING_AFTER, CRITICAL_AFTER = 10, 30  # seconds a charging problem must last (ignores brief dips)
    SETTLE_SECONDS = 20 * 60  # surface charge is mostly gone after ~20 minutes off (rough)
    CRANK_GOOD, CRANK_WEAK = 10.0, 9.6  # lowest volts while starting: above GOOD fine, below WEAK bad
    CRANK_WINDOW = 5  # seconds before the engine catches that count as cranking
    CRANK_DENSE_SECONDS, CRANK_MIN_SAMPLES = 2, 5  # need >= 5 readings in the last 2 s to see the real dip
    MIN_OFF_SECONDS = 2  # the engine must have been off this long for a restart to count as a start
    SOURCES_DISAGREE = 0.6  # volts between the two readings that suggests a wiring/terminal issue
    DISAGREE_AFTER = 30  # seconds the difference must last (the ECU reading is updated less often)

    def __init__(self, state_path=None):
        self.path = state_path  # None: don't load or save (demo, replay)
        self.state = self._load()
        self.samples = deque()  # (time, volts), the last few seconds, for the cranking dip
        self.volts = self.port = self.ecu = None
        self.running = None
        self.off_since = None
        self.problem_since = None
        self.disagree_since = None
        self.now = time.time()
        self.last_reading = None  # (read time or value) of the last reading added to samples
        cranks = self.state.get("cranks", [])
        self.last_crank = cranks[-1] if cranks else None

    def update(self, values):
        """Feed one set of readings; call once per dashboard update."""
        self.now = now = time.time()
        self.port, self.ecu = _number(values.get("Battery (port)")), _number(values.get("Battery"))
        self.volts = self.port if self.port is not None else self.ecu
        if self.volts is not None and self._is_new_reading(values):
            self.samples.append((now, self.volts))
            while now - self.samples[0][0] > self.CRANK_WINDOW + 1:
                self.samples.popleft()

        rpm = _number(values.get("RPM"))
        if rpm is not None:
            running = rpm >= self.RUNNING_RPM
            off_long_enough = self.off_since is not None and now - self.off_since >= self.MIN_OFF_SECONDS
            if self.running is False and running and off_long_enough:
                self._record_crank(now)  # the engine just started
            if running:
                self.off_since = None
            elif rpm < self.STOPPED_RPM and self.off_since is None and self.running is not None:
                self.off_since = now
            self.running = running
        self._track_timers(now)

    def _is_new_reading(self, values):
        """Only real new readings count towards the cranking dip, not the last value shown again.

        Live, the dashboard passes when each value was read ("_read_at"); in replays that isn't
        recorded, so a changed value is taken as a new reading.
        """
        read_at = values.get("_read_at")
        if read_at is not None:
            key = read_at.get("Battery (port)" if self.port is not None else "Battery")
        else:
            key = self.volts
        if key is None or key == self.last_reading:
            return False
        self.last_reading = key
        return True

    def _track_timers(self, now):
        """How long a charging problem / source disagreement has lasted (independent of screen refreshes)."""
        charging_low = self.running and self.volts is not None and self.volts < self.CHARGING_MIN
        self.problem_since = (self.problem_since or now) if charging_low else None
        apart = self.port is not None and self.ecu is not None and abs(self.port - self.ecu) > self.SOURCES_DISAGREE
        self.disagree_since = (self.disagree_since or now) if apart else None

    def engine_off(self):
        """True while the engine is known to be off (the dashboard then samples voltage fast)."""
        return self.running is False

    def report(self):
        """Everything the dashboard needs, or None until there's a voltage reading."""
        if self.volts is None:
            return None
        state, level, detail = self._state()
        crank = self._crank_verdict()
        sources = self._sources_check()
        return {
            "volts": self.volts,
            "source": "adapter port" if self.port is not None else "engine computer",
            "ecu_volts": self.ecu,
            "port_volts": self.port,
            "state": state,
            "level": level,
            "detail": detail,
            "crank": crank,
            "sources_note": sources,
        }

    # ------------------------------------------------------------ states

    def _state(self):
        volts, now = self.volts, self.now
        if self.running:
            if volts > self.OVERCHARGE_CRITICAL:
                return "OVERCHARGING", "critical", "voltage regulator fault: can damage the battery"
            if volts > self.OVERCHARGE_WARNING:
                return "OVERCHARGING", "warning", "higher than normal charging voltage"
            lasted = now - self.problem_since if self.problem_since else 0
            if volts < self.NOT_CHARGING and lasted >= self.CRITICAL_AFTER:
                return "NOT CHARGING", "critical", "alternator not charging: running on battery"
            if lasted >= self.WARNING_AFTER:
                return "LOW CHARGE", "warning", "alternator output low"
            return "CHARGING", "good", "alternator OK"
        if self.running is None:
            return "", "good", ""
        if self.off_since is not None and now - self.off_since < self.SETTLE_SECONDS:
            minutes = (self.SETTLE_SECONDS - (now - self.off_since)) / 60
            return "SETTLING", "good", f"true resting value in ~{minutes:.0f} min"
        percent = charge_percent(volts)
        level = "critical" if percent < 25 else "warning" if percent < 50 else "good"
        prefix = "" if self.off_since is not None else "if rested: "
        return "RESTING", level, f"{prefix}≈{percent:.0f} % charged"

    def _record_crank(self, now):
        """Lowest voltage between the engine stopping (at most CRANK_WINDOW s back) and it catching."""
        since = max(now - self.CRANK_WINDOW, self.off_since or 0)
        window = [v for t, v in self.samples if t >= since]
        if not window:
            return
        dense = sum(1 for t, _ in self.samples if t >= now - self.CRANK_DENSE_SECONDS and t >= since)
        self.last_crank = {
            "when": datetime.now().isoformat(timespec="seconds"),
            "min_volts": round(min(window), 2),
            "samples": dense,  # readings in the last 2 s: enough to have caught the dip?
        }
        self.state["cranks"] = (self.state.get("cranks", []) + [self.last_crank])[-100:]
        self._save()

    def _crank_verdict(self):
        crank = self.last_crank
        if not crank:
            return None
        volts = crank["min_volts"]
        if crank["samples"] < self.CRANK_MIN_SAMPLES:
            level, text = "unknown", "too few readings to judge"
        elif volts >= self.CRANK_GOOD:
            level, text = "good", "battery strong"
        elif volts >= self.CRANK_WEAK:
            level, text = "warning", "battery getting weak"
        else:
            level, text = "critical", "battery weak: may not start next time"
        return {**crank, "level": level, "text": text}

    def _sources_check(self):
        """The two readings should agree; a lasting gap points at a terminal, ground or wiring issue."""
        if self.disagree_since is None or self.now - self.disagree_since < self.DISAGREE_AFTER:
            return None
        return f"port {self.port:.1f} V vs engine computer {self.ecu:.1f} V: check battery terminals"

    # ------------------------------------------------------------ state file

    def _load(self):
        if self.path and os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save(self):
        if not self.path:
            return
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(temp, self.path)
