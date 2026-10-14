---
name: new-car
description: Set up a new car for the OBD dashboard - run new_car_probe.py, create cars/<car>/, explain what the car supports, fill in car.json and get the web dashboard and gear display working. Use when the user plugs the adapter into a car that isn't in cars/ yet, or asks to add, set up or onboard a car.
---

# Set up a new car

Goal: the owner ends with `cars/<car>/` (settings + scan), the web dashboard showing everything their
car reports, and a clear list of what still needs a drive or a number. Explain simply; the owner may
not be technical. Follow the project rules in `CLAUDE.md` (read-only on the car, no car numbers in
shared code, private data out of git, README current).

## 1. Check where the adapter is
- The adapter is only reachable from the computer it's paired with (Windows: a Bluetooth COM port).
  If you run somewhere else (e.g. WSL, a remote machine), give the owner the commands to run in
  PowerShell in the project folder and ask them to paste the output.
- List cars already set up: folders in `cars/` (ignore `_template`). If this car might be one of
  them, `new_car_probe.py` recognises it by VIN and only refreshes its scan.
- COM port unknown? `python car_dashboard.py --list-ports` (use the *outgoing* Bluetooth port).

## 2. Ask the owner first (the script can't ask questions when you run it)
Ask in one message; every answer is optional ("don't know" is fine -> leave it out):
- Car name, e.g. "Maruti Swift 1.2 petrol (2019)"
- Fuel tank size in litres (owner's manual)
- Engine size (litres or cc)
- RPM where the display should turn red (their own comfort limit; default 4000)
- Before running: engine running (or ignition fully ON), phone Bluetooth off, adapter plugged in.

## 3. Run the probe (read-only, ~1 minute)
```
python new_car_probe.py --port COM6 --title "<name>" --tank <litres> --engine <size> --redline <rpm> --yes
```
Leave out flags the owner couldn't answer. Never add `--protocol` unless the auto-detect fails.
It creates `cars/<folder>/` (`car.json`, `probe.json`, `vin.txt`, `recordings/`) and makes it the active car.

## 4. Explain what the car supports
Read `cars/<folder>/probe.json` (`modules`, `supported_live_data`, `snapshot`, `vin`, `maker`) and
tell the owner, in plain words, which dashboard features work:
- Temperatures: coolant 05, oil 5C, intake 0F, ambient 46, catalyst 3C
- Torque and power: PIDs 62 + 63 (python-OBD doesn't list them; the probe shows "not in python-OBD")
- Fuel: rate 5E (best), level 2F, manifold pressure 0B (backup estimate, petrol only)
- Pedal cross-check: 49 + 4A; battery: 42
- Other modules (e.g. transmission at 7E9) and any stored/pending trouble codes
Mention what's missing and what that switches off. Don't speculate about manufacturer-specific data.

## 5. Fill in car.json gaps (optional)
- Only with a source: owner's manual, maker spec page or a reputable car site. Write the source in
  `notes` (e.g. `"tank_litres": "Maruti spec sheet, 2019"`). If unsure, leave `null` and say so.
- Gear ratios / final drive / tyre size (e.g. `"175/60 R15"`) are optional: they give a first gear
  guess before calibrating.

## 6. Check the dashboard
`python webui/server.py --port COM6`, open http://localhost:8765. The **ALL SENSORS** button lists
every value. (No car at hand: `python webui/server.py --demo --car <folder>` shows the layout.)

## 7. After the first drive
Every live run is recorded in `cars/<folder>/recordings/`. After a drive that used every gear from
1st up: `python car_dashboard.py --calibrate-gears --car <folder> --yes` -> gear display on.

## 8. Wrap up
- Back in another car: `--car <folder>` once (or the VIN switches automatically).
- Own web design (optional): copy only the files to change from `webui/static/` into
  `cars/<folder>/ui/` and edit there; keep the snapshot fields `app.js` reads.
- Update README.md only if you changed how things work (not for adding a car).
- Never commit recordings, probe files or the VIN.
