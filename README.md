# car-obd-dashboard

A race-style car dashboard in the terminal, fed live from the car's OBD-II port.
It shows what the car's own instrument cluster doesn't: coolant and oil temperatures, engine
internals (load, manifold pressure, timing, fuel trims, lambda), an RPM sweep with shift/redline
zones, estimated gear, 0-60 / 0-100 km/h times and more.

Built for a 2023 Tata Tiago 1.2 petrol AMT with a Vgate vLinker MC (Bluetooth ELM327 adapter)
on Windows, but it works with any OBD-II car and ELM327-compatible adapter. Stats a car doesn't
report are hidden automatically.

## Setup

```bash
pip install -r requirements.txt
```

1. Plug the adapter into the car's OBD port and turn the ignition on.
2. Pair it in Windows Bluetooth settings (vLinker MC PIN: `1234`).
3. Find its COM port: `python car_dashboard.py --list-ports` (use the *outgoing* one).

## Run

```bash
python car_dashboard.py --demo                       # fake data, no car needed
python car_dashboard.py --port COM6 --protocol 6     # live (6 = CAN 11 bit, 500 kbaud)
python car_dashboard.py --all                        # every stat the car supports, as a table
python car_dashboard.py --replay recordings/<file>.csv   # play back a recorded drive
python car_dashboard.py --help                       # all options
```

Every live run records its readings to `recordings/`, so drives can be replayed and the design
worked on without the car.

## Files

| File | What's in it |
|---|---|
| `car_dashboard.py` | Logic: car settings, which stats to read, adapter connection, trip computer, recording/replay, main loop |
| `dashboard_design.py` | Look: colours, big digits, bar meters, RPM sweep, the dash layout |

Car-specific settings (shift/redline RPM, gear ratios, tyre size, alert thresholds) are at the top
of `car_dashboard.py`. The Tiago's gear ratios are estimates, not official figures.

## Built with

[python-OBD](https://github.com/brendan-w/python-OBD) and [rich](https://github.com/Textualize/rich).
