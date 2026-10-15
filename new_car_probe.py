"""
Set up a new car. Run this once with the adapter plugged into the new car (ignition ON; engine
running is best):

    python new_car_probe.py                 # finds the adapter and the car's protocol, then asks a few questions
    python new_car_probe.py --port COM6
    python new_car_probe.py --title "Maruti Swift 1.2 petrol (2019)" --tank 37 --engine 1.197 --yes

What it does (read-only: nothing on the car is changed or cleared):
1. connects and finds the car's protocol by itself
2. reads the chassis number (VIN): the first 3 characters name the maker; also recognises a car set up before
3. scans every module on the car's network for what it supports, and reads everything once
4. asks a few questions (name, tank size, engine size, RPM limit); Enter keeps the suggestion or skips
5. creates cars/<name>/ with car.json (settings), probe.json (what the car supports), vin.txt and
   recordings/, and makes it the car the dashboards use from now on

Then: python webui/server.py   shows everything this car reports.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

import car_dashboard as cd
import car_profile
from car_profile import CARS_DIR, Car
from dashboard_design import console, print_probe_report

# python-OBD's fuel type texts (PID 51) -> the fuel names car.json uses
FUELS = {"gasoline": "petrol", "diesel": "diesel", "cng": "cng", "lpg": "lpg", "propane": "lpg",
         "electric": "electric", "ethanol": "ethanol", "methanol": "methanol"}


def read_fuel_type(connection):
    import obd

    if not connection.supports(obd.commands.FUEL_TYPE):
        return None
    response = connection.query(obd.commands.FUEL_TYPE)
    text = str(response.value).lower() if cd.answered(response, obd.commands.FUEL_TYPE) else ""
    if "hybrid" in text:
        return "hybrid"
    return next((fuel for word, fuel in FUELS.items() if word in text), None)


def ask(question, default=None, convert=str, interactive=True):
    """Ask in the terminal; Enter keeps the default. Without a terminal (or with --yes) the default is used."""
    if not interactive:
        return default
    shown = f" (Enter = {default})" if default not in (None, "") else " (Enter = skip)"
    while True:
        answer = re.sub(r"\s*\[.*?\]|\s*\(Enter = .*?\)", "", input(f"{question}{shown}: ")).strip()  # retyped hints
        if not answer:
            return default
        try:
            return convert(answer)
        except ValueError:
            print("  That doesn't look right; try again (or Enter to skip).")


def litres(text):
    """Engine size in litres; accepts cc too (1199 -> 1.199)."""
    value = float(text)
    return round(value / 1000, 3) if value > 20 else value


GENERIC_WORDS = {"petrol", "diesel", "cng", "lpg", "electric", "hybrid", "car", "my", "turbo", "amt", "manual", "automatic"}


def short_name(title, maker=None):
    """A short word for the dashboard's top bar: the model ("Tata Tiago 1.2 petrol" -> "TIAGO",
    "Mahindra XUV700 2.2 diesel" -> "XUV700"): the first word with letters that isn't the maker or a generic word."""
    skip = GENERIC_WORDS | {w.lower() for w in (maker or "").replace("/", " ").split()}
    words = [w.strip("()[],") for w in title.split()]
    words = [w for w in words if any(c.isalpha() for c in w)]
    model = next((w for w in words if w.lower() not in skip), "CAR")
    return model.upper()[:10]


def with_maker(title, maker):
    """Put the maker in front of the car's name if it isn't there yet ("XUV700 ..." -> "Mahindra XUV700 ...")."""
    first = (maker or "").split()[0] if maker else ""
    if first and first.lower() not in title.lower():
        return f"{first} {title}"
    return title


def free_folder(name):
    key, n = name, 2
    while os.path.exists(os.path.join(CARS_DIR, key)):
        key, n = f"{name}_{n}", n + 1
    return key


def parse_args():
    parser = argparse.ArgumentParser(description="Set up a new car: read-only scan + a settings folder under cars/")
    parser.add_argument("--port", help="COM port of the adapter, e.g. COM6; auto-detected if omitted")
    parser.add_argument("--protocol", help='ELM327 protocol id, e.g. "6"; auto-detected if omitted (recommended)')
    parser.add_argument("--title", help='Car name, e.g. "Maruti Swift 1.2 petrol (2019)"')
    parser.add_argument("--name", help="Folder name under cars/ (default: made from the title)")
    parser.add_argument("--tank", type=float, help="Fuel tank size in litres (owner's manual)")
    parser.add_argument("--engine", type=litres, help="Engine size in litres or cc, e.g. 1.197 or 1197")
    parser.add_argument("--fuel", help="petrol / diesel / cng ... (default: what the car reports)")
    parser.add_argument("--redline", type=int, default=4000, help="RPM where the display turns red (default 4000)")
    parser.add_argument("--yes", action="store_true", help="Don't ask questions: use the values given / suggested")
    return parser.parse_args()


def main():
    args = parse_args()
    interactive = sys.stdin.isatty() and not args.yes

    connection = cd.connect(args.port, args.protocol)
    if connection is None:
        return
    if not cd.car_is_answering(connection):
        print(f"\nThe adapter answered on {connection.port_name()}, but the car's engine computer did not.")
        print("Turn the ignition fully ON (or start the engine), wait ~10 s, and run again.")
        connection.close()
        return
    protocol_id = connection.protocol_id()
    print(f"\nConnected · {connection.protocol_name()} (protocol {protocol_id})")

    vin = cd.read_vin(connection)
    maker = car_profile.maker_from_vin(vin)
    print(f"Chassis number (VIN): {vin or 'not shared by this car'}" + (f" · maker: {maker}" if maker else ""))
    fuel = read_fuel_type(connection)

    print("Scanning every module on the car's network (read-only, ~20 s) ...")
    report = cd.scan_modules(connection)
    print("Reading everything once (a few seconds) ...")
    report.update({
        "vin": vin,
        "maker": maker,
        "fuel": fuel,
        "protocol_id": protocol_id,
        "trouble_codes": cd.trouble_codes(connection),
        "supported_commands": sorted(c.name for c in connection.supported_commands if c.mode == 1),
        "snapshot": cd.texts(cd.read_everything(connection)),
    })
    connection.close()  # everything is read: the questions below don't need the car
    print_probe_report(report)

    known = car_profile.match_vin(vin)
    if known:
        car = car_profile.load(known)
        print(f"\nThis car is already set up as '{known}' ({car.name}). Updated its probe.json; car.json is unchanged.")
    else:
        console.print("\n[bold]A few questions[/bold] (just press Enter to keep the suggestion or skip)")
        title = args.title or ask("Car name, e.g. Maruti Swift 1.2 petrol (2019)",
                                  f"{maker} car" if maker else "My car", interactive=interactive)
        title = with_maker(title, maker)
        key = free_folder(car_profile.folder_name(args.name or title))
        with open(os.path.join(CARS_DIR, "_template", "car.json"), encoding="utf-8") as f:
            template = json.load(f)
        car = Car(key=key, folder=os.path.join(CARS_DIR, key), settings=template, name=title,
                  short_name=short_name(title, maker), brand=maker, protocol=protocol_id,
                  fuel=args.fuel or fuel or ask("Fuel (petrol / diesel / cng)", "petrol", interactive=interactive))
        car.tank_litres = args.tank or ask("Fuel tank size in litres (owner's manual)", None, float, interactive)
        car.engine_litres = args.engine or ask("Engine size in litres or cc, e.g. 1.2 or 1199", None, litres,
                                               interactive)
        car.redline_rpm = ask("RPM where the display turns red (your own limit)", args.redline, int, interactive)
        car.shift_rpm = car.redline_rpm - 500
        car_profile.save(car)

    os.makedirs(car.recordings, exist_ok=True)
    report["saved"] = datetime.now().isoformat(timespec="seconds")
    with open(car.path("probe.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    if vin:
        with open(car.path("vin.txt"), "w", encoding="utf-8") as f:
            f.write(vin + "\n")
    car_profile.remember(car.key)

    live = sum(len(m["supported_live_data"]) for m in report["modules"].values())
    print(f"\nSet up: cars/{car.key}/  ({len(report['modules'])} module(s), {live} live values)")
    print("  car.json     settings: edit any time (null = unknown, switches that feature off)")
    print("  probe.json   what this car supports (stays on this laptop)")
    print("\nThe dashboards now use this car. Next:")
    print("  1. python webui/server.py                     shows everything this car reports")
    print("  2. drive through every gear (recorded automatically), then")
    print("     python car_dashboard.py --calibrate-gears  turns on the gear display")
    if not car.tank_litres:
        print(f"  -  add tank_litres to cars/{car.key}/car.json for fuel left in litres and range")
    print("Switch cars with --car <name>, e.g.  python car_dashboard.py --car tata_tiago_2023")


if __name__ == "__main__":
    main()
