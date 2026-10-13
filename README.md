# car-obd-dashboard — operator guide

A race-style dashboard in the terminal, fed live from the car's OBD-II port. It shows what the car's
own dashboard doesn't: oil and coolant temperatures, engine internals, live torque and power, fuel
left in litres, battery and charging health, sprint times and more.

Built for a **2023 Tata Tiago 1.2 petrol AMT** with a **Vgate vLinker MC** (Bluetooth) on **Windows**.
It works with other OBD-II cars too; anything a car doesn't report is hidden automatically.

Everything below works **offline**. Keep this file with the code.

---

## 1. Before every session

**In the car**
1. Plug the vLinker into the OBD port (under the dashboard, driver's side).
2. Start the engine (or turn the ignition fully ON).
3. Turn **off your phone's Bluetooth**, so no phone app grabs the adapter.

**On the laptop:** open PowerShell in the folder with the code (in File Explorer, open the folder,
type `powershell` in the address bar, press Enter). If `python` isn't recognised, run this first:

```powershell
Set-Alias python "C:\Program Files\Python39\python.exe"
```

**Best look:** use **Windows Terminal**, maximised.

**Stop anything:** press **Ctrl+C**.

---

## 2. What you can run

There are **two dashboards** showing the same data — pick one per drive:

- **Terminal dashboard:** `python car_dashboard.py …` — runs in the PowerShell window.
- **Web dashboard:** `python webui\server.py …` — opens in a browser, on the laptop or on a **phone/tablet**
  (see section 3, "Web dashboard"). Same options, same recordings and fuel/battery data.

For the terminal dashboard you only ever run **`car_dashboard.py`**; the other files are used by it automatically.
`COM6` is the vLinker's port on this laptop and `--protocol 6` is the Tiago's (CAN 11-bit, 500 kbaud).

| I want to… | Command | Needs the car? |
|---|---|---|
| **Drive with the dashboard** | `python car_dashboard.py --port COM6 --protocol 6` | Yes |
| Try the dashboard with fake data | `python car_dashboard.py --demo` | No |
| Watch a recorded drive again | `python car_dashboard.py --replay recordings\drive_20261013_1536.csv` | No |
| **Tell it about a fill-up** (start of the drive after refuelling) | `python car_dashboard.py --port COM6 --protocol 6 --refuel 30.5 --full` | Yes |
| Scan which modules answer (engine, AMT, …) | `python car_dashboard.py --port COM6 --protocol 6 --probe` | Yes |
| See every value the car supports, as a table | `python car_dashboard.py --port COM6 --protocol 6 --all` | Yes |
| Measure the gears from recorded drives | `python car_dashboard.py --calibrate-gears` | No |
| Find the vLinker's COM port | `python car_dashboard.py --list-ports` | Adapter paired |
| Troubleshoot the connection | add `--debug` (traffic goes to `obd.log`) | Yes |
| Drive without the spoken warm-up messages | add `--mute` | — |
| Slow down screen updates (e.g. if it flickers) | add `--interval 0.5` (seconds between updates; default 0.1) | — |
| List all options | `python car_dashboard.py --help` | No |

Options can be combined, e.g. `--demo --refuel 10` or `--port COM6 --protocol 6 --debug`.

**Every real run is recorded automatically** (see section 4) — everything except `--demo` (fake data)
and `--replay` (plays back a recording that already exists). There is no way to switch it off.

---

## 3. The modes in detail

### Drive (the normal mode)
```powershell
python car_dashboard.py --port COM6 --protocol 6
```
Connects (up to ~30 s per attempt), saves what the car supports, then shows the dashboard and records
every reading (see section 4). The header shows **● REC** while recording.

**On screen:** three sections side by side, a status bar underneath.

| Section | What's in it |
|---|---|
| **HEAT** (left) | **Engine state** from the warm-up advisor (`COLD START` → `READY TO MOVE` → `WARMING` → `READY TO PUSH`), or `HOT` / `TOO HOT`; **coolant** and **oil** in big digits with status and trend (`↑ +2.1°/min`, `→ steady`); intake air, outside air, **heat soak** (how much hotter the intake air is than outside — hot air means less power), catalyst |
| **POWER** (middle) | **Shift lights**: a row of LEDs that light up with RPM — green, amber from 3500, red from 4000 (lower while the engine is cold, see the warm-up advisor); they **blink** at the limit, meaning shift up / ease off. Below them **RPM**, the **estimated gear** in huge digits, and **speed**; **torque** (Nm) and **power** (bhp) with bars; bar meters for throttle, pedal, engine load, manifold pressure and timing; best **0-60 / 0-100** km/h times and acceleration |
| **FUEL** (right) | **FUEL LEFT** in litres with ± and a tank bar (and the tank sensor's own reading); **range** in km; consumption **now** (L/100 km and L/h) and for the **trip** (L/100 km and litres used); **mixture**: lambda and both fuel trims |
| **STATUS** (bottom) | Battery state and last starting dip; trip distance and average speed; engine run time; air pressure; distance counters; pedal sensor check |

- Bars with a centre line (timing, torque, power, trims, lambda) grow up/down or left/right from it;
  a thin `━` / `│` marks the highest value this drive.
- **When something is critical** (e.g. overheating, alternator not charging, weak battery, fuel on
  reserve), **that section's frame turns red** and the STATUS bar turns red with the reason as its title.
- Anything your car doesn't report is hidden (the bottom line says how many).

### Web dashboard (browser, phone or tablet)
Same data and features as the terminal dashboard, with a sporty motorsport-style display: shift
lights, slanted rev band, RPM / gear / speed, torque and power, bar meters, HEAT / POWER / FUEL /
STATUS sections that glow red on an alert. Live runs are recorded exactly like the terminal ones.

```powershell
python webui\server.py --demo                          # fake data
python webui\server.py --port COM6 --protocol 6        # live, from the car
python webui\server.py --replay recordings\drive_20261013_1131.csv
python webui\server.py --port COM6 --protocol 6 --refuel 30.5 --full   # after a fill-up
```
It prints the addresses to open, e.g. `http://localhost:8765` (on the laptop) and `http://192.168.x.x:8765`.

**On the laptop:** open `http://localhost:8765` in Edge or Chrome and click once ("Tap to start" =
full screen + spoken messages).

**On a phone or tablet (landscape):**
1. Put the laptop and the phone on the same network — easiest: turn on the **phone's hotspot** and
   connect the laptop to it (or use the laptop's Mobile hotspot and connect the phone to the laptop).
2. Start the server on the laptop; it prints a `http://192.168.x.x:8765` address.
3. Open that address in the phone's browser, turn the phone sideways, tap once. The screen stays on.
4. The first time, Windows may ask whether to allow Python on the network: allow it for **private networks**.

Options: `--web-port 8766` if 8765 is taken; `--mute` for no spoken messages; add `?autostart` to the
address to skip the tap screen (no full screen / voice then). **Ctrl+C** in PowerShell stops the server.

### Demo
```powershell
python car_dashboard.py --demo
```
Fake car repeating a 40-second accelerate-and-brake cycle. Safe to play with: demo and replay never
change the saved fuel or battery data. Fuel appears after ~10 s, the 0-60 time after ~40 s, range after
~2-3 minutes.

### Replay a recorded drive
```powershell
python car_dashboard.py --replay recordings\drive_20261013_1536.csv
```
Plays the drive at its original pace and loops. The header says **REPLAY**. Use it to look at the
design at home. Drives recorded before a feature existed won't have its values.

### Warm-up advisor (automatic)
After a **cold start** it tells you when to move off and when it's safe to push — out loud, so you
don't have to look at the screen — and lowers the red zone of the shift lights while the engine is cold.

| Stage | When | You hear | Red zone from |
|---|---|---|---|
| **COLD START** | Right after starting a cold engine (coolant below 70 °C) | "Cold start. Wait for the idle to settle." | 2,000 rpm |
| **READY TO MOVE** | The fast idle has settled (or after 60 s at most) | "Ready to move. Drive gently." | 2,500 rpm |
| **WARMING** | Coolant reaches 70 °C | "Engine warming up. Moderate driving." | 3,500 rpm |
| **READY TO PUSH** | Coolant ≥ 80 °C and oil ≥ 85 °C for a minute | "Engine warm. Full power OK." | normal (4,000) |

- Revving above the current limit makes the shift lights blink and shows **✖ KEEP BELOW … RPM: ENGINE COLD**.
- Starting an already-warm engine skips straight to the right stage, silently.
- Uses the laptop's speakers (Windows' built-in voice). Turn the voice off with `--mute`.
- The thresholds are tuned on a part-warm start; a recording of a real **cold morning start**
  will let us fine-tune them.

### Fuel: after refuelling
Start the **next drive** with the litres from the pump receipt:
```powershell
python car_dashboard.py --port COM6 --protocol 6 --refuel 30.5 --full   # pump clicked off: tank full
python car_dashboard.py --port COM6 --protocol 6 --refuel 10            # partial fill
```
- Use `--full` only when the pump clicked off by itself.
- Forgot? It detects a refuel by itself, just less precisely.
- Accuracy: about **±1 L** at first; after **two fills to full** it has learned the tank sensor and the
  fuel-rate correction and gets to about **±0.4 L**.

### Probe: what modules and data does the car have?
```powershell
python car_dashboard.py --port COM6 --protocol 6 --probe
```
~20-30 s, **read-only** (never clears codes or changes anything). Prints one panel per module
(on the Tiago: engine `7E8` and AMT `7E9`) with name, software ID, supported data, the gear PID and
trouble codes, and saves `recordings\probe_<date>_<time>.json`.

### Calibrate the gears
```powershell
python car_dashboard.py --calibrate-gears
```
Reads all recorded drives and prints each gear's RPM per km/h plus a line to paste into the
settings at the top of `car_dashboard.py` (`GEAR_RPM_PER_KMH = [...]`). Do this after a highway drive:
so far only 1st-3rd are measured.

### Battery and charging
Automatic in drive mode, shown in the bottom line:
- Engine running: **CHARGING** / **LOW CHARGE** / **NOT CHARGING** (alternator problem) / **OVERCHARGING**.
- Engine off: **SETTLING** for ~20 minutes, then **RESTING ≈ xx % charged**.
- **START DIP:** the lowest voltage while starting — above 10 V is healthy, below 9.6 V means the
  battery may not start the car next time. To measure it, **connect first with the ignition on and
  the engine off, then start the engine**; it samples fast while the engine is off.

---

## 4. Files

| File | What it is | Run it? |
|---|---|---|
| `car_dashboard.py` | Main program: car settings (top of file), stats, connection, recording/replay, probe | **Yes — the only one** |
| `dashboard_design.py` | How it looks: colours, big digits, shift lights, layout | No (used by the main program) |
| `fuel_tracker.py` | Fuel left in litres from four sources, learns from fill-ups | No |
| `battery_monitor.py` | Battery/charging state and starting-voltage test | No |
| `warmup_advisor.py` | Warm-up stages after a cold start, spoken messages, variable redline | No |
| `webui\server.py` | Web dashboard: reads the car like the terminal one and streams it to the browser | **Yes — instead of `car_dashboard.py`** |
| `webui\static\` | The web page itself: `index.html`, `style.css` (the look), `app.js` (drawing the data) | No (served by `server.py`) |
| `requirements.txt` | Python packages to install | `pip install -r requirements.txt` |

**Created while you use it** (personal, never uploaded to GitHub). Names carry the date and time the
run started, e.g. `drive_20261013_153612.csv` = 13 Oct 2026, 15:36:12:

| File | Made by | What's in it |
|---|---|---|
| `recordings\drive_<date>_<time>.csv` | Drive (dashboard) | One row per update: `t` (seconds since start), `time` (clock time), then every value read |
| `recordings\all_<date>_<time>.csv` | `--all` | Same, with every value the car supports |
| `recordings\<drive or all>_<date>_<time>_profile.json` | Drive and `--all` | What the car supported in that run + one snapshot of everything |
| `recordings\probe_<date>_<time>.json` | `--probe` | Every module and what it reports |
| `fuel_state.json` | Drive | Fuel left and what's been learned (keep it; deleting resets the learning) |
| `battery_state.json` | Drive | History of starting voltages |
| `obd.log` | Drive, `--all` | Adapter traffic (detailed with `--debug`) |

Open the CSV files in Excel or pandas for analysis; `--replay` works on the `drive_` files.
Recordings made before 13 Oct 2026 (13:15) have no `time` column and only minute-precision names.

**Settings you might change** (top of `car_dashboard.py`): shift/redline RPM, tank size, gear values,
and each stat's warning thresholds in the `STATS` list.

---

## 5. Troubleshooting

| Problem | Fix |
|---|---|
| `Python was not found` | Run `Set-Alias python "C:\Program Files\Python39\python.exe"` first |
| Stuck at `Trying COM6…` / `semaphore timeout` | Press the button on the vLinker to wake it; phone Bluetooth off; toggle laptop Bluetooth; re-plug the adapter |
| Which COM port? | `python car_dashboard.py --list-ports` — use the **outgoing** Bluetooth port |
| "The adapter answered … but the car's engine computer did not" | Turn the ignition fully ON or start the engine, wait ~10 s, run again |
| Screen cut off | Maximise the window; on small windows some detail is dropped automatically |
| Values look stale or slow | Check "updated in" at the top right; try `--debug` and read `obd.log` |
| Web page doesn't open on the phone | See **"Phone can't connect"** below (no admin rights: use the USB cable method) |
| Web page says "connecting…" / red dot top-right | The server stopped or the link dropped; it reconnects by itself when the server runs again |
| `Port 8765 is already in use` | Another web dashboard is still running: close it, or add `--web-port 8766` |
| No spoken messages | Check the laptop volume; messages only come after a cold start (or in `--demo`); `--mute` turns them off |
| Nothing works | Run `--demo` to check the software alone, then `--debug` with the car |

### Phone can't connect to the web dashboard
Usually the laptop's firewall. Check in this order:
1. **Right address:** use the `http://192.168.x.x:8765` address the server prints for "phone/tablet",
   never `localhost` (that means "this device" on the phone).
2. **Network marked Private:** Windows Settings → Network & internet → Wi-Fi → your network →
   *Network profile type* → **Private**. On a "Public" network Windows blocks the phone.
3. **Allow the dashboard (once, PowerShell as administrator):**
   ```powershell
   New-NetFirewallRule -DisplayName "Car dashboard (web)" -Direction Inbound -Program "C:\Program Files\Python39\python.exe" -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private
   ```
   This opens only port 8765, only for Python, only on private networks.
4. Restart the server and reload the page on the phone.

On a work-managed laptop steps 2-3 need admin rights or IT approval. **No admin rights? Use the USB
cable method below** (Android), or the laptop screen.

### Android phone over USB (no admin rights needed)
The phone reaches the dashboard through the USB cable, which the firewall doesn't block (it also charges
the phone).

**Once:**
1. Phone: Settings → About phone → tap **Build number** 7 times → *Developer options* → turn on **USB debugging**.
2. Laptop: download **SDK Platform-Tools for Windows** from developer.android.com (search "platform-tools"),
   unzip it to `Documents\platform-tools` (nothing to install).

**Each drive:**
1. Connect the phone with a **data** USB cable; the first time, tap **Allow** on the phone's "USB debugging" prompt.
2. In PowerShell:
   ```powershell
   & "$HOME\Documents\platform-tools\adb.exe" reverse tcp:8765 tcp:8765
   python webui\server.py --port COM6 --protocol 6
   ```
3. On the phone open **`http://localhost:8765`**, turn it sideways, tap once.

Re-run the `adb reverse` line whenever the cable is reconnected. `adb devices` shows whether the phone is seen.

---

## 6. Setup on a new laptop

```powershell
pip install -r requirements.txt
```
Pair the vLinker in Windows Settings → Bluetooth & devices (PIN `1234`), then find its port with
`--list-ports`.

Built with [python-OBD](https://github.com/brendan-w/python-OBD) and [rich](https://github.com/Textualize/rich).
