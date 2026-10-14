# CLAUDE.md — instructions for Claude Code in this project

A live OBD-II car dashboard (terminal + web) for any car, via an ELM327-type adapter (built with a
Vgate vLinker MC over Bluetooth on Windows). README.md is the human operator guide; read it first.

## Map
- `car_dashboard.py` — logic + terminal entry point: STATS list, connecting, reading, trip computer,
  recording/replay, probe, gear calibration. `dashboard_design.py` — terminal look only.
- `car_profile.py` — per-car settings: `cars/<car>/car.json`, choosing the car, VIN -> maker.
- `new_car_probe.py` — sets up a new car (read-only scan + `cars/<car>/`).
- `fuel_tracker.py`, `battery_monitor.py`, `warmup_advisor.py` — calculations, no drawing.
- `webui/server.py` (stdlib HTTP + Server-Sent Events) and `webui/static/` — web dashboard. The JSON
  snapshot built in `Pipeline.process()` is the contract between server and page.
- `cars/<car>/ui/` — optional per-car web design; files there override `webui/static/`.

## Rules
1. **Read-only on the car.** Only OBD services 01, 03, 07, 09 (and ELM `AT` setup commands). Never
   send service 04 (clear codes), 08, 2F, 31 or anything that writes, resets or actuates.
2. **No car-specific numbers in shared code.** Tank, engine size, RPM limits, gears, protocol go in
   `cars/<car>/car.json` (with the source in its `notes`). A missing value means `null` (feature off),
   never a guess presented as fact.
3. **Private data never goes to git:** recordings, `probe*.json`, `vin.txt`, fuel/battery state,
   `active_car.txt` (see `.gitignore`). Only `car.json` and `ui/` per car are shared.
4. **Every real run is recorded** (anything except `--demo` / `--replay`); don't add a way to skip it.
5. **Keep README.md current** with every change: written for humans in the car, offline, plain words.
6. No new dependencies without asking (today: `obd`, `rich`; the web UI is standard library only).
7. Keep the terminal dashboard working; test both front ends after changes to shared code.

## Testing without a car
- `python car_dashboard.py --demo`, `python webui/server.py --demo` (open http://localhost:8765).
- `--replay cars/<car>/recordings/drive_<...>.csv` replays a real drive.
- Optional fake car: `pip install ELM327-emulator`, run `python -m elm -s car -n 35036`, then
  `--port socket://127.0.0.1:35036` (its VIN changes between runs, so use a throwaway car folder).

## New car
Use the `/new-car` skill (`.claude/skills/new-car/SKILL.md`).
