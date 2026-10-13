"""
Fuel left in the tank, in litres, as accurately as the car's data allows.

Four independent sources are combined, so one bad source can't fool the estimate:

1. Counting from the engine's fuel rate (PID 5E, litres/hour): precise over short spans,
   but small scale errors add up over a tank.
2. Counting from the air going into the engine (manifold pressure, RPM, intake temperature,
   lambda): a backup when the fuel rate isn't reported, scaled to agree with source 1.
3. The tank's float sensor (PID 2F, %): noisy (sloshing), non-linear (tank shape) and stuck at
   100 % for the first litres after a fill-up, but absolute, so it stops 1 and 2 from drifting.
   Only a one-minute median, taken while stopped or driving steadily, is used.
4. Fill-up receipts (--refuel LITRES [--full]): ground truth. They reset the estimate, teach the
   float sensor's real curve and correct the fuel-rate scale.

A one-dimensional Kalman filter does the combining: counting moves the estimate down and grows
its uncertainty; each float reading pulls it towards the sensor by as much as the two are trusted.
The result comes with a ± (one standard deviation). State is kept between drives in a JSON file.

Error sizes below are assumptions (typical for float senders and ECU fuel-rate figures), not
measured for this car; fill-ups with --full replace the most important ones with learned values.
"""

import json
import math
import os
import statistics
import time
from collections import deque
from datetime import datetime

PETROL_G_PER_LITRE = 745  # density of petrol
STOICHIOMETRIC_AFR = 14.7  # grams of air per gram of petrol for complete burning


def _number(value):
    return value if isinstance(value, (int, float)) else None


class FuelTracker:
    FLOAT_SIGMA = 2.0  # litres (1σ): smoothed float sensor before any fill-up calibration
    FLOAT_SIGMA_CALIBRATED = 1.2  # once two or more fill-ups have taught the sensor curve
    RATE_ERROR = 0.05  # counting from fuel rate: error as a fraction of litres counted
    RATE_ERROR_CALIBRATED = 0.02  # after a fill-to-full pair has corrected the scale
    AIR_ERROR = 0.15  # counting from the air estimate
    MIN_SIGMA = 1.0  # litres: never claim to be surer than this while the float curve is unlearned...
    MIN_SIGMA_CALIBRATED = 0.4  # ...or than this once fill-ups have taught it (its errors are shared, not random)
    SMOOTH_SECONDS = 60  # float readings are reduced to their median over this window...
    FLOAT_EVERY = 60  # ...and used at most this often (its errors are correlated, not random)
    MIN_FLOAT_READINGS = 5
    STEADY_G = 0.05  # only trust the float when acceleration/braking is below this
    PLATEAU_PCT = 97  # float reads ~100 % for the first litres after filling: only a lower bound up here
    REFUEL_JUMP = 4.0  # litres: a bigger rise since last drive (beyond the noise) counts as a refuel
    SAVE_EVERY = 30  # seconds between saves of the state file

    def __init__(self, tank_litres, engine_litres, volumetric_efficiency, state_path=None):
        self.tank = tank_litres
        self.engine_litres = engine_litres
        self.volumetric_efficiency = volumetric_efficiency
        self.path = state_path  # None: don't load or save anything (demo, replay)
        self.state = self._load()
        self.litres = self.state.get("litres")
        self.variance = self.state.get("variance")
        self.counted = self.litres  # same start, but only ever reduced by counting (no float corrections)
        self.since_anchor = 0.0  # litres counted since the last fill-up or reset
        self.readings = deque()  # (time, float %)
        self.last_time = None
        self.last_float_update = 0.0
        self.last_save = time.time()
        self.flow = None
        self.flow_source = None
        self.used_trip = 0.0
        self.trip_km = 0.0
        self.trip_km_saved = 0.0
        self.first_seen = None
        self.used_saved = 0.0
        self.pending_refuel = None
        self.notes = []
        self.started = False
        self.consumption_now = None  # L/100 km right now (None when stopped)

    # ------------------------------------------------------------ public

    def refuel(self, litres_added=None, full=False):
        """Record a fill-up; applied at the first reading of this run (needs the float sensor)."""
        self.pending_refuel = (litres_added, full)

    def update(self, values, trip_km=0.0, accel_g=0.0):
        """Feed one set of readings; call once per dashboard update."""
        now = time.time()
        pct = _number(values.get("Fuel level"))
        if pct is not None:
            self.readings.append((now, pct))
            while now - self.readings[0][0] > self.SMOOTH_SECONDS:
                self.readings.popleft()

        if not self.started and not self._start(now):
            return  # waiting for the first float readings
        dt = now - self.last_time if self.last_time else 0.0
        self.last_time = now

        self.flow, self.flow_source = self._flow(values)
        speed = _number(values.get("Speed"))
        self.consumption_now = self.flow / speed * 100 if self.flow is not None and speed and speed > 5 else None
        if self.flow is not None and dt > 0:
            self._count(self.flow * dt / 3600)

        if now - self.last_float_update >= self.FLOAT_EVERY and abs(accel_g or 0) < self.STEADY_G:
            self._float_update(now)

        self.trip_km = trip_km
        if self.path and now - self.last_save >= self.SAVE_EVERY:
            self.save()

    def report(self):
        """Everything the dashboard needs; litres is None until there's enough data."""
        pct = self._smoothed_pct()
        consumption = self._consumption()
        litres = None if self.litres is None else min(max(self.litres, 0.0), self.tank)
        return {
            "litres": litres,
            "sigma": None if self.variance is None else math.sqrt(self.variance),
            "tank": self.tank,
            "sensor_pct": pct,
            "sensor_litres": None if pct is None else self.float_litres(pct),
            "counted_litres": self.counted,
            "flow_lph": self.flow,
            "flow_source": self.flow_source,
            "used_trip": self.used_trip,
            "now_l_per_100km": self.consumption_now,
            "trip_l_per_100km": self.used_trip / self.trip_km * 100 if self.trip_km > 1 else None,
            "consumption_l_per_100km": consumption,
            "range_km": litres / consumption * 100 if litres is not None and consumption else None,
            "fill_ups_learned": len(self.state.get("float_curve", [])),
            "notes": list(self.notes),
        }

    def float_litres(self, pct):
        """Litres for a float reading, from the sensor curve learned at fill-ups (straight line until then)."""
        points = sorted({0.0: 0.0, 100.0: self.tank, **{float(p): l for p, l in self.state.get("float_curve", [])}}
                        .items())
        for (p0, l0), (p1, l1) in zip(points, points[1:]):
            if p0 <= pct <= p1:
                return l0 + (l1 - l0) * (pct - p0) / (p1 - p0) if p1 > p0 else l1
        return points[-1][1] if pct > 100 else 0.0

    def save(self):
        """Write the state file (atomically, so a crash can't leave half a file)."""
        if not self.path or self.litres is None:
            return
        km, used = self.trip_km - self.trip_km_saved, self.used_trip - self.used_saved
        self.trip_km_saved, self.used_saved = self.trip_km, self.used_trip
        self.state.update({
            "saved": datetime.now().isoformat(timespec="seconds"),
            "litres": round(self.litres, 3),
            "variance": round(self.variance, 4),
            "float_pct_at_save": self._smoothed_pct(),
            "total_km": round(self.state.get("total_km", 0.0) + km, 3),
            "total_litres": round(self.state.get("total_litres", 0.0) + used, 3),
        })
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(temp, self.path)
        self.last_save = time.time()

    # ------------------------------------------------------------ starting a run

    def _start(self, now):
        """First estimate of this run: saved state, a fill-up, a detected refuel, or the float alone.

        Returns False (try again next update) until there's something to start from.
        """
        self.first_seen = self.first_seen or now
        pct = self._smoothed_pct(minimum=3)
        if pct is None and now - self.first_seen < 10:
            return False  # give the float sensor a few seconds to report

        if self.pending_refuel:
            self._apply_refuel(pct)
        elif self.litres is None and pct is not None:  # first run ever: the float is all we have
            self._reset(self.float_litres(pct), self._float_sigma() ** 2)
            self.notes.append("First run: starting from the float sensor. Fill up with --refuel L --full "
                              "for the best accuracy.")
        elif self.litres is not None and pct is not None:
            sensor, saved = self.float_litres(pct), self.litres
            noise = 3 * math.sqrt(self.variance + self._float_sigma() ** 2)
            if pct < self.PLATEAU_PCT and sensor - saved > max(self.REFUEL_JUMP, noise) or \
                    pct >= self.PLATEAU_PCT and saved < self.float_litres(self.PLATEAU_PCT) - noise:
                self._reset(max(sensor, saved), self._float_sigma() ** 2)
                self.state["counting_since_full_valid"] = False
                self.notes.append(f"Refuel detected (~{sensor - saved:.0f} L added). Next time start with "
                                  f"--refuel <litres> [--full] for the best accuracy.")
        if self.litres is None:
            return False  # no saved state and no float sensor: nothing to start from yet
        self.started, self.last_time, self.last_float_update = True, now, now
        self.counted = self.litres
        return True

    def _apply_refuel(self, pct_now):
        added, full = self.pending_refuel
        self.pending_refuel = None
        before = self.litres
        if full:
            if added:
                self._learn_from_full_fill(added)
                if before is not None and self.state.get("float_pct_at_save") is not None:
                    # Tank was (full - added) litres just before this fill: a real point on the float curve.
                    self._add_curve_point(self.state["float_pct_at_save"], self.tank - added)
            self._reset(self.tank, 0.5 ** 2)  # brimmed tanks still vary a little
            self.state.update(counting_since_full=0.0, receipts_since_full=0.0, counting_since_full_valid=True)
            self.notes.append(f"Fill-up to full recorded{f' ({added:.1f} L added)' if added else ''}.")
        elif added:
            base = before if before is not None else (self.float_litres(pct_now) if pct_now is not None else 0.0)
            variance = (self.variance if before is not None else self._float_sigma() ** 2) + 0.3 ** 2
            self._reset(min(self.tank, base + added), variance)
            self.state["receipts_since_full"] = self.state.get("receipts_since_full", 0.0) + added
            self.notes.append(f"Fill-up of {added:.1f} L recorded.")

    def _learn_from_full_fill(self, added):
        """Between two fills to full, the litres added = the fuel really burned: correct the counting scale."""
        counted = self.state.get("counting_since_full", 0.0)
        really_burned = self.state.get("receipts_since_full", 0.0) + added
        if self.state.get("counting_since_full_valid") and counted > 5:
            new_scale = self.state.get("rate_scale", 1.0) * really_burned / counted
            old = self.state.get("rate_scale")
            self.state["rate_scale"] = new_scale if old is None else (old + new_scale) / 2
            self.state["rate_scale_learned"] = self.state.get("rate_scale_learned", 0) + 1
            self.notes.append(f"Fuel-rate scale corrected to {self.state['rate_scale']:.3f}.")

    def _add_curve_point(self, pct, litres):
        points = [p for p in self.state.get("float_curve", []) if abs(p[0] - pct) > 3]  # replace close ones
        points.append([round(pct, 1), round(litres, 2)])
        self.state["float_curve"] = sorted(points)[-20:]

    # ------------------------------------------------------------ counting

    def _flow(self, values):
        """Fuel flowing now, litres/hour, and where the number came from."""
        air = self._air_flow_lph(values)
        rate = _number(values.get("Fuel rate"))
        if rate is not None:
            if air:  # keep the backup scaled to agree with the engine's own figure
                ratio = rate / air if air > 0.3 else None
                if ratio and 0.3 < ratio < 3:
                    self.state["air_scale"] = 0.99 * self.state.get("air_scale", 1.0) + 0.01 * ratio
            return rate * self.state.get("rate_scale", 1.0), "engine fuel rate"
        if air is not None:
            return air * self.state.get("air_scale", 1.0), "estimated from air intake"
        return None, None

    def _air_flow_lph(self, values):
        """Fuel needed for the air the engine takes in (speed-density: pressure x volume / temperature)."""
        pressure, rpm = _number(values.get("Manifold pressure")), _number(values.get("RPM"))
        if pressure is None or rpm is None:
            return None
        intake = _number(values.get("Intake air"))
        kelvin = (intake if intake is not None else 25) + 273.15
        air_g_per_s = (pressure * 1000 * self.engine_litres / 1000 * self.volumetric_efficiency * rpm / 120
                       / (287.05 * kelvin) * 1000)
        lam = _number(values.get("Lambda (commanded)")) or 1.0
        return air_g_per_s / (STOICHIOMETRIC_AFR * lam) / PETROL_G_PER_LITRE * 3600

    def _count(self, litres):
        """Predict step: fuel burned lowers the estimate; the uncertainty grows with what was counted.

        Counting errors are mostly a scale error (systematic), so they grow with the total counted
        since the last anchor, not like independent noise.
        """
        error = self._counting_error()
        self.since_anchor += litres
        self.variance += (error * litres) ** 2 + 2 * error ** 2 * (self.since_anchor - litres) * litres
        self.litres -= litres
        self.counted -= litres
        self.used_trip += litres
        scale = self.state.get("rate_scale", 1.0)
        if self.flow_source == "engine fuel rate":
            self.state["counting_since_full"] = self.state.get("counting_since_full", 0.0) + litres / scale
        else:
            self.state["counting_since_full_valid"] = False  # backup counting can't calibrate the scale

    def _counting_error(self):
        if self.flow_source != "engine fuel rate":
            return self.AIR_ERROR
        return self.RATE_ERROR_CALIBRATED if self.state.get("rate_scale_learned") else self.RATE_ERROR

    # ------------------------------------------------------------ float corrections

    def _float_update(self, now):
        """Update step: pull the estimate towards the smoothed float reading, weighted by trust."""
        pct = self._smoothed_pct()
        if pct is None:
            return
        self.last_float_update = now
        measured = self.float_litres(pct)
        noise = self._float_sigma() ** 2
        if pct >= self.PLATEAU_PCT and self.litres >= measured:
            return  # up on the plateau the float only says "at least this much"
        if pct <= 3:
            noise *= 4  # near empty the sender is least reliable
        gain = self.variance / (self.variance + noise)
        self.litres += gain * (measured - self.litres)
        floor = self.MIN_SIGMA_CALIBRATED if len(self.state.get("float_curve", [])) >= 2 else self.MIN_SIGMA
        self.variance = max((1 - gain) * self.variance, floor ** 2)

    def _float_sigma(self):
        return self.FLOAT_SIGMA_CALIBRATED if len(self.state.get("float_curve", [])) >= 2 else self.FLOAT_SIGMA

    def _smoothed_pct(self, minimum=None):
        values = [p for _, p in self.readings]
        if len(values) < (minimum or self.MIN_FLOAT_READINGS):
            return None
        return statistics.median(values)

    # ------------------------------------------------------------ range

    def _consumption(self):
        """L/100 km for the range: this trip once it's long enough, blended with the long-term average."""
        total_km, total_l = self.state.get("total_km", 0.0), self.state.get("total_litres", 0.0)
        long_term = total_l / total_km * 100 if total_km > 20 else None
        trip_km = getattr(self, "trip_km", 0.0)
        trip = self.used_trip / trip_km * 100 if trip_km > 2 else None
        if long_term and trip:
            weight = min(trip_km / 50, 0.7)  # longer trips count more, but history always keeps a say
            return weight * trip + (1 - weight) * long_term
        return trip or long_term

    # ------------------------------------------------------------ helpers

    def _reset(self, litres, variance):
        self.litres, self.variance = litres, variance
        self.counted, self.since_anchor = litres, 0.0

    def _load(self):
        if self.path and os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        return {}
