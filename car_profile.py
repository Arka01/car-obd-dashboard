"""
Car settings: one folder per car under cars/, so the same code works for every car.

    cars/
      tata_tiago_2023/
        car.json        settings: tank size, redline, gears... (safe to share)
        probe.json      what the car supports, from new_car_probe.py (private: stays on this laptop)
        vin.txt         the car's chassis number, to recognise it next time (private)
        recordings/     every real drive (private)
        ui/             optional: this car's own web dashboard design (overrides webui/static)
      active_car.txt    which car was used last (private)

A setting left out of car.json (or set to null) switches off what needs it: no gear ratios -> no
gear estimate, no tank size -> no fuel-left estimate. Everything the car reports is still shown.
"""

import json
import math
import os
import re
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
CARS_DIR = os.path.join(HERE, "cars")
ACTIVE_PATH = os.path.join(CARS_DIR, "active_car.txt")


@dataclass
class Car:
    key: str = "demo"  # folder name under cars/
    name: str = "Demo car"
    short_name: str = "DEMO"  # shown in the web dashboard's top bar
    brand: str = None
    fuel: str = "petrol"
    protocol: str = None  # ELM327 protocol id, e.g. "6"; None = auto-detect
    shift_rpm: int = 3500  # RPM display turns amber
    redline_rpm: int = 4000  # ... and red
    tank_litres: float = None
    engine_litres: float = None
    volumetric_efficiency: float = 0.85
    gear_rpm_per_kmh: list = None  # measured with --calibrate-gears
    gear_ratios: list = None  # from the maker's specs, if known: gives a first guess before calibrating
    final_drive: float = None
    tyre: str = None  # e.g. "175/60 R15"
    spec: dict = None  # maker's peak torque / power, for comparison: torque_nm, torque_rpm, power_bhp, power_rpm
    folder: str = None  # None: nothing is saved (demo)
    settings: dict = field(default_factory=dict)  # car.json as loaded, to save it back unchanged

    @property
    def recordings(self):
        return os.path.join(self.folder, "recordings") if self.folder else os.path.join(HERE, "recordings")

    def path(self, name):
        """A file in this car's folder, or None for the demo car (which saves nothing)."""
        return os.path.join(self.folder, name) if self.folder else None

    @property
    def gears(self):
        """RPM per km/h for each gear: measured if calibrated, else from the specs, else None."""
        return self.gear_rpm_per_kmh or self.theoretical_rpm_per_kmh

    @property
    def theoretical_rpm_per_kmh(self):
        """Engine rpm per 1 km/h in each gear in theory: wheel turns per minute at 1 km/h x gear ratio x final drive."""
        circumference = tyre_circumference_m(self.tyre)
        if not (self.gear_ratios and self.final_drive and circumference):
            return None
        return [r * self.final_drive * (1000 / 60) / circumference for r in self.gear_ratios]

    @property
    def vin(self):
        path = self.path("vin.txt")
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip() or None
        return None


def tyre_circumference_m(tyre):
    """Rolling circumference from a tyre size like "175/60 R15": rim + 2 sidewalls."""
    match = re.match(r"\s*(\d+)\s*/\s*(\d+)\s*Z?R?\s*(\d+)", tyre or "", re.IGNORECASE)
    if not match:
        return None
    width_mm, profile_pct, rim_inch = (int(g) for g in match.groups())
    return math.pi * (rim_inch * 25.4 + 2 * width_mm * profile_pct / 100) / 1000


FIELDS = ("name", "short_name", "brand", "fuel", "protocol", "shift_rpm", "redline_rpm", "tank_litres",
          "engine_litres", "volumetric_efficiency")


def load(key):
    folder = os.path.join(CARS_DIR, key)
    with open(os.path.join(folder, "car.json"), encoding="utf-8") as f:
        settings = json.load(f)
    gears = settings.get("gears") or {}
    car = Car(key=key, folder=folder, settings=settings,
              **{k: settings[k] for k in FIELDS if settings.get(k) is not None},
              gear_rpm_per_kmh=gears.get("rpm_per_kmh"), gear_ratios=gears.get("gear_ratios"),
              final_drive=gears.get("final_drive"), tyre=gears.get("tyre"), spec=settings.get("spec"))
    if car.protocol is not None:
        car.protocol = str(car.protocol)
    return car


def save(car):
    """Write car.json back, keeping anything in it this code doesn't know about."""
    os.makedirs(car.folder, exist_ok=True)
    settings = dict(car.settings)
    settings.update({k: getattr(car, k) for k in FIELDS})
    settings["gears"] = {**settings.get("gears", {}), "rpm_per_kmh": car.gear_rpm_per_kmh,
                         "gear_ratios": car.gear_ratios, "final_drive": car.final_drive, "tyre": car.tyre}
    with open(os.path.join(car.folder, "car.json"), "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    car.settings = settings


def all_cars():
    """Folder names of the cars set up so far (folders starting with _ are templates)."""
    if not os.path.isdir(CARS_DIR):
        return []
    return sorted(d for d in os.listdir(CARS_DIR)
                  if not d.startswith((".", "_")) and os.path.exists(os.path.join(CARS_DIR, d, "car.json")))


def remember(key):
    os.makedirs(CARS_DIR, exist_ok=True)
    with open(ACTIVE_PATH, "w", encoding="utf-8") as f:
        f.write(key + "\n")


def remembered():
    if os.path.exists(ACTIVE_PATH):
        with open(ACTIVE_PATH, encoding="utf-8") as f:
            key = f.read().strip()
        if key in all_cars():
            return key
    return None


def choose(requested=None, demo=False):
    """The car to use: --car NAME (remembered for next time), else the one used last, else the only one.

    Returns a Car, or exits with a message saying what to do. The demo works without any car set up.
    """
    cars = all_cars()
    if requested:
        if requested not in cars:
            raise SystemExit(f"No car called '{requested}'. Set up so far: {', '.join(cars) or 'none'}.\n"
                             "Set up a new car with:  python new_car_probe.py")
        if not demo:
            remember(requested)
        return load(requested)
    key = remembered() or (cars[0] if len(cars) == 1 else None)
    if key:
        return load(key)
    if demo:
        return Car()
    if not cars:
        raise SystemExit("No car set up yet. Plug the adapter into the car and run:  python new_car_probe.py")
    raise SystemExit(f"Which car? Add --car with one of: {', '.join(cars)}")


def car_of_recording(path):
    """The car a recording belongs to, from its folder (cars/<car>/recordings/...), or None."""
    parts = os.path.normpath(os.path.abspath(path)).split(os.sep)
    if "recordings" in parts:
        i = len(parts) - 1 - parts[::-1].index("recordings")
        if i >= 2 and parts[i - 2] == "cars" and parts[i - 1] in all_cars():
            return parts[i - 1]
    return None


def match_vin(vin):
    """The car folder whose saved VIN matches, or None."""
    if not vin:
        return None
    return next((key for key in all_cars() if load(key).vin == vin), None)


# ---------------------------------------------------------------- recognising the maker from the VIN


# First 3 characters of the VIN (World Manufacturer Identifier) -> maker. Common car makers only;
# from Wikibooks' WMI list (Vehicle Identification Numbers / World Manufacturer Identifier).
MAKERS = {
    # India
    "MAT": "Tata Motors", "MA3": "Maruti Suzuki", "MAL": "Hyundai", "MB2": "Hyundai", "MA1": "Mahindra",
    "MAB": "Mahindra", "MAC": "Mahindra", "MAK": "Honda", "MBJ": "Toyota", "MAJ": "Ford", "MZB": "Kia",
    "MZ7": "MG Motor", "MEE": "Renault", "MDH": "Nissan", "MEX": "Skoda / Volkswagen", "MCA": "Fiat / Jeep",
    "MAH": "Fiat", "MZZ": "Citroen", "MBR": "Mercedes-Benz", "MBH": "Suzuki", "MC1": "Force Motors",
    # Elsewhere
    "WVW": "Volkswagen", "WVG": "Volkswagen", "WBA": "BMW", "WBS": "BMW M", "WBX": "BMW", "WDD": "Mercedes-Benz",
    "WDB": "Mercedes-Benz", "WDC": "Mercedes-Benz", "W1K": "Mercedes-Benz", "W1N": "Mercedes-Benz",
    "WAU": "Audi", "WA1": "Audi", "WP0": "Porsche", "WP1": "Porsche", "TMB": "Skoda", "VF1": "Renault", "WF0": "Ford", "JTD": "Toyota",
    "JT2": "Toyota", "JT3": "Toyota", "JHM": "Honda", "JS2": "Suzuki", "JS3": "Suzuki", "JN1": "Nissan",
    "JN8": "Nissan", "KMH": "Hyundai", "KM8": "Hyundai", "KNA": "Kia", "KND": "Kia", "LSJ": "MG / SAIC",
}


def maker_from_vin(vin):
    return MAKERS.get((vin or "")[:3].upper())


def folder_name(text):
    """'Tata Tiago 2023' -> 'tata_tiago_2023' (safe on every operating system)."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "my_car"
