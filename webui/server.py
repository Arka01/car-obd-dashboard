"""
Web dashboard: the same car data as the terminal dashboard, shown in a browser (laptop, phone or
tablet) with a sporty, motorsport-style look.

    python webui/server.py --demo                      # fake data
    python webui/server.py --port COM6 --protocol 6    # live, from the car (recorded, like the terminal one)
    python webui/server.py --replay recordings\\drive_20261013_1131.csv

Then open the address it prints (http://localhost:8765 on the laptop; the http://192.168.x.x:8765 one
on a phone/tablet connected to the laptop's hotspot or the same Wi-Fi).

How it works: a background thread reads the car (or fake/replayed data) a few times a second, runs
the same calculations as the terminal dashboard (trip computer, fuel, battery, warm-up advisor...)
and turns everything into one plain "snapshot" (a dict that becomes JSON). The page subscribes to
/stream and receives each snapshot as a Server-Sent Event. Only Python's standard library is used,
so nothing extra has to be installed and it works offline.

The logic files (car_dashboard.py, fuel_tracker.py, battery_monitor.py, warmup_advisor.py) are reused
unchanged. The loop below mirrors car_dashboard.main(); keep the two in step until they're merged.
"""

import argparse
import itertools
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # the project folder, for the shared logic files

import car_dashboard as car  # noqa: E402  (needs the path above)
from battery_monitor import BatteryMonitor  # noqa: E402
from dashboard_design import (  # noqa: E402
    CRITICAL, GOOD, NOT_SUPPORTED, UNIT_LABELS, WARNING, as_number, critical_alerts, status_of,
)
from fuel_tracker import FuelTracker  # noqa: E402
from warmup_advisor import WarmUpAdvisor  # noqa: E402

STATIC = os.path.join(HERE, "static")
LEVEL_NAMES = {GOOD: "good", WARNING: "warning", CRITICAL: "critical"}


# ---------------------------------------------------------------- turning readings into a snapshot


def number_text(stat, value):
    """The value as text without its unit, formatted like the terminal dashboard."""
    if stat.fmt:
        return stat.fmt(value)
    if round(value, stat.decimals) == 0:
        value = 0.0
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:.{stat.decimals}f}"


def stat_entry(stat, value, peaks):
    number = as_number(value)
    status = status_of(stat, number) if number is not None else None
    return {
        "label": stat.label,
        "short": stat.short or stat.label,
        "group": stat.group,
        "value": number,
        "text": number_text(stat, number) if number is not None else None,
        "unit": "" if stat.fmt else UNIT_LABELS.get(stat.unit, stat.unit),
        "level": LEVEL_NAMES[status[0]] if status else None,
        "status": status[1] if status else None,
        "low": stat.low,
        "high": stat.high,
        "center": stat.center,
        "peak": peaks.get(stat.label),
        "empty": stat.empty,
    }


class Pipeline:
    """Everything the terminal dashboard's `dashboard()` step does, ending in a snapshot dict."""

    def __init__(self, live, refuel=None, full=False, mute=False):
        self.trip = car.TripComputer()
        self.pedals = car.PedalCheck()
        self.trends = car.Trends([s.label for s in car.STATS if s.group == "heat"])
        self.fuel = FuelTracker(car.TANK_LITRES, car.ENGINE_LITRES, car.VOLUMETRIC_EFFICIENCY,
                                car.FUEL_STATE_PATH if live else None)
        if refuel or full:
            self.fuel.refuel(refuel, full)
        self.battery = BatteryMonitor(car.BATTERY_STATE_PATH if live else None)
        self.advisor = WarmUpAdvisor()
        self.peaks = {}
        self.mute = mute
        self.say = None  # {"id": n, "text": ..., "at": time}: the latest announcement, spoken once by the page
        self.say_ids = itertools.count(1)

    def process(self, values, took, header, mode):
        values = {**values, **self.trip.update(values), **car.engine_output(values), **self.pedals.update(values),
                  **car.heat_values(values)}
        self.trends.update(values)
        self.fuel.update(values, self.trip.distance, self.trip.accel)
        self.battery.update(values)
        self.advisor.update(values)
        sentence = self.advisor.announcement()
        if sentence and not self.mute:
            self.say = {"id": next(self.say_ids), "text": sentence, "at": time.time()}
        for label, value in values.items():
            if as_number(value) is not None:
                self.peaks[label] = max(self.peaks.get(label, value), value)

        warm_up = self.advisor.report()
        shown = [s for s in car.STATS if values.get(s.label) != NOT_SUPPORTED]
        fuel, battery = self.fuel.report(), self.battery.report()
        return {
            "mode": mode,
            "header": header,
            "clock": datetime.now().strftime("%H:%M:%S"),
            "took": round(took, 3),
            "stats": {s.label: stat_entry(s, values.get(s.label), self.peaks)
                      for s in shown if s.group != "inputs"},
            "hidden": len(car.STATS) - len(shown),
            "trends": self.trends.per_minute(),
            "engine": car.hot_state(values) or warm_up,
            "rpm": {"shift": car.SHIFT_RPM, "redline": car.REDLINE_RPM,
                    "limit": warm_up["rpm_limit"] if warm_up else None},
            "fuel": fuel,
            "battery": battery,
            "alerts": [{"section": section, "text": text}
                       for section, text in critical_alerts(shown, values, battery, fuel)],
            # age lets the page skip messages that are too old to be useful (e.g. opened mid-drive)
            "say": {**{k: v for k, v in self.say.items() if k != "at"}, "age": round(time.time() - self.say["at"], 1)}
            if self.say else None,
        }


# ---------------------------------------------------------------- the reading loop (background thread)


class Feed:
    """Runs the reading loop and keeps the latest snapshot for the web page."""

    def __init__(self):
        self.snapshot = None
        self.version = 0
        self.changed = threading.Condition()
        self.stopping = threading.Event()

    def publish(self, snapshot):
        with self.changed:
            self.snapshot, self.version = snapshot, self.version + 1
            self.changed.notify_all()

    def run(self, read, pipeline, header, mode, interval):
        while not self.stopping.is_set():
            started = time.time()
            values = read()
            took = time.time() - started
            self.publish(pipeline.process(values, took, header, mode))
            time.sleep(max(0, interval - took))


def live_reader(connection, recorder, battery):
    """Read the car like car_dashboard.main's read_car(): each stat every `stat.every` updates; only
    voltage + RPM while the engine is off (to catch the starting dip); every reading recorded."""
    updates = itertools.count()
    latest = {}

    def read():
        n = next(updates)
        if battery.engine_off() and n % 20:
            wanted = [s for s in car.STATS if s.label in ("Battery (port)", "RPM")]
        else:
            wanted = [s for s in car.STATS if s.command and n % s.every == 0]
        fresh = car.read_stats(connection, wanted)
        latest.update(fresh)
        now = time.time()
        latest["_read_at"] = {**latest.get("_read_at", {}), **{label: now for label in fresh}}
        recorder.write(latest)
        return dict(latest)

    return read


# ---------------------------------------------------------------- web server


class Handler(SimpleHTTPRequestHandler):
    """Serves the page from static/ and the live data at /stream (Server-Sent Events)."""

    feed = None  # set before the server starts

    def do_GET(self):
        if self.path.split("?")[0] == "/stream":
            return self.stream()
        return super().do_GET()

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = -1
        try:
            while not self.feed.stopping.is_set():
                with self.feed.changed:
                    self.feed.changed.wait_for(lambda: self.feed.version != seen or self.feed.stopping.is_set(),
                                               timeout=2)
                    snapshot, seen = self.feed.snapshot, self.feed.version
                payload = json.dumps(snapshot, ensure_ascii=False) if snapshot else "{}"
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the page was closed or reloaded

    def log_message(self, *args):
        pass  # keep the console quiet


def addresses(http_port):
    """(url, note) pairs to open: this laptop first, then its network addresses, the likely Wi-Fi/hotspot
    one (192.168.x.x / 10.x.x.x) marked for phones; virtual adapters (WSL, Hyper-V: 172.16-31.x.x) and
    unconnected ones (169.254.x.x) are left out."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    urls = [(f"http://localhost:{http_port}", "on this laptop")]
    for ip in sorted(found):
        first, second = (int(part) for part in ip.split(".")[:2])
        if first == 127 or (first, second) == (169, 254) or (first == 172 and 16 <= second <= 31):
            continue
        note = "phone/tablet on the same Wi-Fi or hotspot" if first in (192, 10) else "other network"
        urls.append((f"http://{ip}:{http_port}", note))
    return urls


def parse_args():
    parser = argparse.ArgumentParser(description="Web dashboard (motorsport style) for the car's OBD-II data")
    parser.add_argument("--demo", action="store_true", help="Use fake data instead of a car")
    parser.add_argument("--replay", metavar="CSV", help="Play back a recorded drive")
    parser.add_argument("--port", help="COM port of the adapter, e.g. COM6; auto-detected if omitted")
    parser.add_argument("--protocol", help='ELM327 protocol id, e.g. "6"; auto if omitted')
    parser.add_argument("--refuel", type=float, metavar="LITRES", help="Litres just put in (pump receipt)")
    parser.add_argument("--full", action="store_true", help="The tank was filled to the automatic cut-off")
    parser.add_argument("--mute", action="store_true", help="Don't speak the warm-up messages")
    parser.add_argument("--web-port", type=int, default=8765, help="Port for the web page (default 8765)")
    parser.add_argument("--interval", type=float, default=0.1, help="Minimum seconds between updates")
    return parser.parse_args()


def main():
    args = parse_args()
    live = not (args.demo or args.replay)
    pipeline = Pipeline(live, args.refuel, args.full, args.mute)
    feed = Feed()
    connection = recorder = None

    if args.demo:
        start = time.time()
        read, header, mode, interval = (lambda: car.fake_values(time.time() - start)), "DEMO · fake data", "demo", 0.5
    elif args.replay:
        read = car.replay_reader(args.replay)
        header, mode, interval = f"REPLAY · {os.path.basename(args.replay)}", "replay", 0.2
    else:
        connection = car.connect(args.port, args.protocol)
        if connection is None:
            return
        if not car.car_is_answering(connection):
            print("The adapter answered, but the car's engine computer did not. Turn the ignition fully ON.")
            connection.close()
            return
        trouble = car.trouble_codes(connection)
        name = f"drive_{datetime.now():%Y%m%d_%H%M%S}"  # every real run is recorded, like the terminal one
        print("Saving what this car supports (a few seconds) ...")
        print(f"  -> {car.save_profile(connection, trouble, os.path.join(car.RECORD_DIR, f'{name}_profile.json'))}")
        recorder = car.Recorder(os.path.join(car.RECORD_DIR, f"{name}.csv"), [s.label for s in car.STATS if s.command])
        print(f"Recording readings to {recorder.path}")
        car.log_to_file()
        read = live_reader(connection, recorder, pipeline.battery)
        header = f"{connection.port_name()} · {connection.protocol_name()} · codes: {trouble} · ● REC"
        mode, interval = "live", args.interval

    Handler.feed = feed
    try:
        server = ThreadingHTTPServer(("0.0.0.0", args.web_port), partial(Handler, directory=STATIC))
    except OSError:
        print(f"Port {args.web_port} is already in use (is another dashboard still running?). "
              f"Close it, or add --web-port {args.web_port + 1}.")
        if connection:
            connection.close()
        return
    server.daemon_threads = True
    threading.Thread(target=feed.run, args=(read, pipeline, header, mode, interval), daemon=True).start()

    print("\nWeb dashboard running. Open:")
    for url, note in addresses(args.web_port):
        print(f"   {url:<28} {note}")
    print("Phone not connecting? See 'Web page doesn't open on the phone' in README.md (Windows firewall).")
    print("Ctrl+C stops it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        feed.stopping.set()
        with feed.changed:
            feed.changed.notify_all()
        server.server_close()
        pipeline.fuel.save()
        if connection:
            connection.close()
        if recorder:
            recorder.close()
            print(f"Run saved: {recorder.path}")


if __name__ == "__main__":
    main()
