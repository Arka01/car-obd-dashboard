"""
How the car dashboard looks: colours, big digits, shift lights, bar meters and the layout:
three sections side by side (HEAT | POWER | FUEL) with a STATUS bar underneath.

Nothing here talks to the car. Every function gets the stat definitions and current values passed
in and returns something `rich` can draw, so the look can be changed (or tested with fake values)
without touching the connection logic in car_dashboard.py.

Values follow one convention throughout: a number, None (no reading yet) or NOT_SUPPORTED.
"""

import time
from datetime import datetime

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console()

NOT_SUPPORTED = "n/a"  # value of a stat the car doesn't report; its tile is hidden

# ---------------------------------------------------------------- colours and characters

FILL = "#3987e5"  # bars and meters
TRACK = "grey27"  # empty part of a bar
PEAK = "grey70"  # peak marker
FRAME = "grey50"  # panel borders
LABEL = "grey78"  # captions
LIT = "grey93"  # lit part of the RPM sweep
REDZONE_UNLIT = "#4a1d1d"  # the RPM sweep's red zone when not reached

# Status levels: (icon, colour). Always shown with a text label, never colour alone.
GOOD, WARNING, CRITICAL = ("✔", "#0ca30c"), ("▲", "#fab219"), ("✖", "#d03b3b")

V_CHARS = " ▁▂▃▄▅▆▇█"  # vertical bar in eighths of a character

TREND_STEADY = 1.0  # temperature change (°C per minute) still shown as steady
FUEL_LOW, FUEL_RESERVE = 8.0, 4.0  # litres: warning / critical

# 3-line "digital" digits for the big numbers
BIG_DIGITS = {
    "0": ("█▀█", "█ █", "▀▀▀"), "1": ("▀█ ", " █ ", "▀▀▀"), "2": ("▀▀█", "█▀▀", "▀▀▀"),
    "3": ("▀▀█", " ▀█", "▀▀▀"), "4": ("█ █", "▀▀█", "  ▀"), "5": ("█▀▀", "▀▀█", "▀▀▀"),
    "6": ("█▀▀", "█▀█", "▀▀▀"), "7": ("▀▀█", "  █", "  ▀"), "8": ("█▀█", "█▀█", "▀▀▀"),
    "9": ("█▀█", "▀▀█", "▀▀▀"), "-": ("   ", "▀▀▀", "   "), " ": ("   ", "   ", "   "),
}

# 5-line chunky digits for the gear, the centrepiece of the POWER section
GEAR_DIGITS = {
    "0": ("████", "█  █", "█  █", "█  █", "████"), "1": ("  █ ", " ██ ", "  █ ", "  █ ", " ███"),
    "2": ("████", "   █", "████", "█   ", "████"), "3": ("████", "   █", " ███", "   █", "████"),
    "4": ("█  █", "█  █", "████", "   █", "   █"), "5": ("████", "█   ", "████", "   █", "████"),
    "6": ("████", "█   ", "████", "█  █", "████"), "7": ("████", "   █", "  █ ", " █  ", " █  "),
    "8": ("████", "█  █", "████", "█  █", "████"), "9": ("████", "█  █", "████", "   █", "████"),
    "-": ("    ", "    ", "████", "    ", "    "),
}
BADGE = "bold black on grey82"  # names of the big temperature readings
LED_TOP, LED_BOTTOM = "▄██▄", "▀██▀"  # one shift-light LED, two lines tall

UNIT_LABELS = {
    "km/h": "km/h", "rpm": "rpm", "degC": "°C", "percent": "%", "volt": "V", "kilopascal": "kPa",
    "gps": "g/s", "degree": "°", "second": "s", "ratio": "λ", "kilometer": "km",
}


# ---------------------------------------------------------------- value helpers and formatters


def as_number(value):
    """The value if it's a number, else None (for None / NOT_SUPPORTED)."""
    return value if isinstance(value, (int, float)) else None


def minutes(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d} min"


def gear_name(gear):
    return "stopped" if gear == 0 else f"{int(gear)}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(int(gear), 'th') } gear"


def find(stats, label):
    return next(s for s in stats if s.label == label)


def fraction(value, low, high):
    """Where value sits between low and high, clamped to 0..1."""
    return min(max((value - low) / (high - low), 0), 1)


# ---------------------------------------------------------------- small pieces


def status_of(stat, value):
    """((icon, colour), text) for the first alert rule that matches, GOOD/"OK" if none do,
    or None for stats without alert rules (so they don't show 'OK' noise)."""
    if not stat.alerts:
        return None
    for test, level, text in stat.alerts:
        if test(value):
            return level, text
    return GOOD, "OK"


def status_text(stat, value, quiet_ok=False):
    """Coloured icon + label, e.g. '▲ HOT'. quiet_ok: show nothing while everything is OK."""
    status = status_of(stat, value)
    if status is None or (quiet_ok and status[0] == GOOD):
        return Text("")
    (icon, colour), text = status
    return Text(f"{icon} {text}", style=colour)


def value_text(stat, value):
    """'12.3 km/h' style text for a value (number bold, unit dim)."""
    if stat.fmt:
        return Text(stat.fmt(value), style="bold")
    if round(value, stat.decimals) == 0:
        value = 0.0  # no "-0" for tiny negatives
    number_text = f"{value:,.0f}" if abs(value) >= 1000 else f"{value:.{stat.decimals}f}"
    unit = UNIT_LABELS.get(stat.unit, stat.unit)
    return Text.assemble((number_text, "bold"), (f" {unit}" if unit else "", "dim"))


def caption(text):
    return Text(text.upper(), style=f"bold {LABEL}")


def big_digit_lines(text, scale):
    """The 3 text lines of `text` in block 'digital' digits; scale=2 doubles the width."""
    lines = []
    for row in range(3):
        glyphs = ["".join(ch * scale for ch in BIG_DIGITS.get(c, BIG_DIGITS[" "])[row]) for c in text]
        lines.append((" " * scale).join(glyphs))
    return lines


def big_number(value, digits, scale, suffix=""):
    """Value as 3 lines of block 'digital' digits (scale=2 doubles the width); suffix on the last line."""
    text = (value if isinstance(value, str) else f"{round(value)}").rjust(digits)[-digits:]
    out = Text(style="bold")
    for row, line in enumerate(big_digit_lines(text, scale)):
        out.append(line)
        if row == 2 and suffix:
            out.append(suffix, style="dim")
        if row < 2:
            out.append("\n")
    return out


def big_number_width(digits, scale):
    return digits * 3 * scale + (digits - 1) * scale


def vertical_bar(stat, value, peak, height, width):
    """Vertical bar meter, as `height` rows of text from top to bottom.

    Normal stats fill up from the bottom (with eighth-of-a-row precision) and mark their peak.
    Stats with a center (trims, timing, lambda) grow up or down from a centre line.
    """
    if value is None:
        return [Text("░" * width, style=TRACK) for _ in range(height)]
    if stat.center is None:
        return _filling_bar(stat, value, peak, height, width)
    return _centered_bar(stat, value, height, width)


def _filling_bar(stat, value, peak, height, width):
    level = fraction(value, stat.low, stat.high) * height * 8  # in eighths of a row
    peak_row = None
    if peak is not None and peak > value:
        peak_row = height - 1 - min(int(fraction(peak, stat.low, stat.high) * height), height - 1)
    rows = []
    for r in range(height):
        eighths = int(min(max(level - (height - 1 - r) * 8, 0), 8))
        if eighths:
            rows.append(Text(V_CHARS[eighths] * width, style=FILL))
        elif r == peak_row:
            rows.append(Text("━" * width, style=PEAK))
        else:
            rows.append(Text("░" * width, style=TRACK))
    return rows


def _centered_bar(stat, value, height, width):
    mid = height // 2  # row of the centre line
    if value >= stat.center:
        n = round(fraction(value, stat.center, stat.high) * mid)
        filled = range(mid - n, mid)
    else:
        n = round(fraction(stat.center - value, 0, stat.center - stat.low) * (height - mid - 1))
        filled = range(mid + 1, mid + 1 + n)
    rows = []
    for r in range(height):
        if r == mid:
            rows.append(Text("─" * width, style=FRAME))
        elif r in filled:
            rows.append(Text("█" * width, style=FILL))
        else:
            rows.append(Text("░" * width, style=TRACK))
    return rows


def gear_lines(text, scale=2):
    """The 5 text lines of the gear in GEAR_DIGITS; scale=2 doubles the width."""
    return ["".join("".join(ch * scale for ch in GEAR_DIGITS.get(c, GEAR_DIGITS["-"])[row]) for c in text)
            for row in range(5)]


def rpm_zone(rpm_stat, at_rpm, limit=None):
    """GOOD / WARNING / CRITICAL for an RPM, from the RPM stat's alert rules and a temporary limit
    (e.g. 2,500 rpm while the engine is cold: red above it, amber in the 500 rpm before it)."""
    status = status_of(rpm_stat, at_rpm)
    level = status[0] if status else GOOD
    if limit is not None and at_rpm > limit:
        return CRITICAL
    if limit is not None and at_rpm > limit - 500 and level == GOOD:
        return WARNING
    return level


def shift_lights(rpm_stat, rpm, width, limit=None):
    """Row of chunky LEDs like an F1 steering wheel: green, amber in the shift zone, red at the limit.

    LEDs above the redline (or the cold-engine limit) are painted dark red even when off. At or past
    the limit every lit LED blinks (toggled by the clock, so it works in any terminal), telling the
    driver to shift up or ease off. A row of 1/2/3... (x1000 rpm) marks sits underneath.
    """
    high = rpm_stat.high
    count = max(8, min(20, width // 5))
    over = rpm is not None and rpm_zone(rpm_stat, rpm, limit) == CRITICAL
    blink_off = over and int(time.time() * 4) % 2 == 1
    top, bottom, marks = Text(), Text(), [" "] * (count * 5)
    mark_styles = [LABEL] * (count * 5)
    for i in range(count):
        at_rpm = (i + 1) * high / count
        level = rpm_zone(rpm_stat, at_rpm, limit)
        lit = rpm is not None and rpm >= at_rpm - high / count / 2 and not blink_off
        if lit:
            colour = {GOOD: GOOD[1], WARNING: WARNING[1], CRITICAL: CRITICAL[1]}[level]
        else:
            colour = REDZONE_UNLIT if level == CRITICAL else TRACK
        top.append(LED_TOP + " ", style=colour)
        bottom.append(LED_BOTTOM + " ", style=colour)
    for k in range(1, int(high // 1000) + 1):  # 1, 2, 3 ... under the LED where that rpm is reached
        i = min(count * 5 - 1, round(k * 1000 / high * count - 0.5) * 5 + 1)
        marks[i] = str(k)
        mark_styles[i] = CRITICAL[1] if rpm_zone(rpm_stat, k * 1000, limit) == CRITICAL else LABEL
    return Group(Align.center(top), Align.center(bottom), Align.center(Text.assemble(*zip(marks, mark_styles))))


def digital_cluster(stats, values, width, limit=None):
    """RPM | huge GEAR | SPEED side by side, with the RPM warning (shift / redline / cold limit) below."""
    rpm_stat = find(stats, "RPM")
    rpm, gear, speed = (as_number(values.get(k)) for k in ("RPM", "Gear (estimated)", "Speed"))
    cell = width // 3
    speed_scale = 2 if cell >= big_number_width(3, 2) + 2 else 1
    cluster = Table.grid(expand=True)
    for _ in range(3):
        cluster.add_column(justify="center", ratio=1)
    cluster.add_row(
        Group(caption("rpm"), Text(""), big_number(rpm if rpm is not None else "-", 4, 1)),
        Group(caption("gear"), Text("\n".join(gear_lines("-" if not gear else str(int(gear)))), style="bold")),
        Group(caption("km/h"), Text(""), big_number(speed if speed is not None else "-", 3, speed_scale)),
    )
    if rpm is not None and limit is not None and rpm > limit:
        warning = Text(f"{CRITICAL[0]} KEEP BELOW {limit:,} RPM: ENGINE COLD", style=f"bold {CRITICAL[1]}")
    else:
        warning = status_text(rpm_stat, rpm, quiet_ok=True) if rpm is not None else Text("")
    return Group(cluster, Align.center(warning))


# ---------------------------------------------------------------- shared pieces


def hbar(stat, value, peak, width):
    """Horizontal bar for a stat: fills from the left, or grows left/right from stat.center
    (marked with │); the peak this drive is a thin │ beyond the fill."""
    if value is None:
        return Text("░" * width, style=TRACK)

    def position(v):
        return round(fraction(v, stat.low, stat.high) * width)

    start, end = (position(stat.center), position(value)) if stat.center is not None else (0, position(value))
    start, end = min(start, end), max(start, end)
    bar = Text("░" * start, style=TRACK) + Text("█" * (end - start), style=FILL)
    bar += Text("░" * (width - end), style=TRACK)
    if stat.center is not None:
        mid = min(position(stat.center), width - 1)
        if not start <= mid < end:
            bar = bar[:mid] + Text("│", style=FRAME) + bar[mid + 1:]
    elif peak is not None and position(peak) > end:
        mark = min(position(peak), width - 1)
        bar = bar[:mark] + Text("│", style=PEAK) + bar[mark + 1:]
    return bar


LEVELS = {"good": GOOD, "warning": WARNING, "critical": CRITICAL}


def level_text(report):
    """Two lines for a {state, level, detail} report: '✔ READY TO MOVE' and, under it, the detail."""
    icon, colour = LEVELS[report["level"]]
    return Text.assemble((f"{icon} {report['state']}", f"bold {colour}"), (f"\n  {report['detail']}", "dim"))


def section(title, body, alert=False):
    """One of the three main sections; its frame turns red while something in it is critical."""
    colour = CRITICAL[1] if alert else FRAME
    return Panel(body, title=Text(f" {title} ", style=f"bold {colour}" if alert else "bold"), title_align="left",
                 border_style=colour, box=box.ROUNDED)


# ---------------------------------------------------------------- HEAT


def trend_text(per_minute, with_rate=True):
    """'↑ +2.1°/min', '↓ -1.3°/min' or '→ steady' (None: not enough history yet)."""
    if per_minute is None:
        return Text("")
    if abs(per_minute) < TREND_STEADY:
        return Text("→ steady" if with_rate else "→", style=LABEL)
    arrow = "↑" if per_minute > 0 else "↓"
    return Text(f"{arrow} {per_minute:+.1f}°/min" if with_rate else arrow, style="bold")


def heat_section(heat_stats, values, trends, warm, width):
    """Warm-up state; coolant and oil in big digits with status and trend; the other temperatures."""
    fluids = [s for s in heat_stats if s.label in ("Coolant", "Oil")]
    others = [s for s in heat_stats if s not in fluids]
    parts = [level_text(warm) if warm else Text("\n")]  # always two lines, so the layout doesn't jump
    for stat in fluids:
        value = as_number(values.get(stat.label))
        parts.append(Text.assemble((f" {stat.label.upper()} ", BADGE)))  # background on the word only
        if value is None:
            parts.append(Text(f"{stat.empty}\n\n", style="dim"))
            continue
        reading = Table.grid(padding=(0, 2))  # number on the left; trend and status beside it
        reading.add_column(no_wrap=True)
        reading.add_column(no_wrap=True)
        with_rate = width >= 30  # narrow windows: just the arrow
        reading.add_row(big_number(value, 3, 1, suffix=" °C"),
                        Group(Text(""), trend_text(trends.get(stat.label), with_rate), status_text(stat, value)))
        parts.append(reading)
    rows = Table.grid(expand=True, padding=(0, 1))
    rows.add_column(no_wrap=True)
    rows.add_column(justify="right", no_wrap=True)
    rows.add_column(no_wrap=True)
    for stat in others:
        value = as_number(values.get(stat.label))
        name = Text((stat.short or stat.label).upper(), style="bold")
        if value is None:
            rows.add_row(name, Text(stat.empty, style="dim"), "")
            continue
        shown = Text.assemble(value_text(stat, value), " ", trend_text(trends.get(stat.label), with_rate=False))
        rows.add_row(name, shown, status_text(stat, value, quiet_ok=True))
    parts.append(rows)
    return Group(*parts)


# ---------------------------------------------------------------- POWER


def power_section(stats, values, peaks, width, meter_height, rpm_limit=None):
    """Shift lights, RPM | GEAR | SPEED, torque and power, bar meters, sprint times."""
    by_label = {s.label: s for s in stats}
    parts = [shift_lights(find(stats, "RPM"), as_number(values.get("RPM")), width, rpm_limit),
             digital_cluster(stats, values, width, rpm_limit)]

    # Torque and power side by side: caption + value, bar underneath.
    output = Table.grid(expand=True, padding=(0, 2))
    output.add_column(ratio=1)
    output.add_column(ratio=1)
    cells = []
    for label in ("Torque", "Power"):
        stat = by_label.get(label)
        if stat is None:  # hidden: the car doesn't report it
            cells.append(Text(""))
            continue
        value = as_number(values.get(label))
        shown = value_text(stat, value) if value is not None else Text("--", style="dim")
        cells.append(Group(Text.assemble(caption(stat.short), "  ", shown),
                           hbar(stat, value, peaks.get(label), max(8, width // 2 - 3))))
    output.add_row(*cells)
    parts.append(output)

    meters = [s for s in stats if s.group == "meters"]
    if meters:
        parts.append(bar_meters(meters, values, peaks, meter_height, width))

    sprint = Text()
    for label in ("0-60 km/h best", "0-100 km/h best", "Acceleration"):
        stat = by_label.get(label)
        if stat is None:
            continue
        value = as_number(values.get(label))
        if sprint:
            sprint.append("     ")
        sprint.append(f"{stat.short} ", style=LABEL)
        sprint += value_text(stat, value) if value is not None else Text(stat.empty, style="dim")
    parts.append(Align.center(sprint))
    return Group(*parts)


def bar_meters(meters, values, peaks, height, width):
    """A row of vertical bar meters: value on top, bar, short name."""
    column = max(6, min(12, width // len(meters) - 1))
    bar_width = max(2, column - 4)
    gap = 1 if width // len(meters) >= 10 else 0  # narrow windows: no gaps, so 8-letter names still fit
    table = Table.grid(expand=True, padding=(0, gap))
    for _ in meters:
        table.add_column(justify="center", ratio=1, no_wrap=True)
    tops, bars, names = [], [], []
    for stat in meters:
        value = as_number(values.get(stat.label))
        tops.append(value_text(stat, value) if value is not None else Text("--", style="dim"))
        bars.append(vertical_bar(stat, value, peaks.get(stat.label), height, bar_width))
        names.append(Text(stat.short or stat.label, style=LABEL))
    table.add_row(*tops)
    for r in range(height):
        table.add_row(*[bar[r] for bar in bars])
    table.add_row(*names)
    return table


# ---------------------------------------------------------------- FUEL


def tank_gauge(litres, sigma, tank, width):
    """Horizontal tank bar: solid up to the estimate, lighter for the ± band, empty beyond."""
    def cells(value):
        return round(fraction(value, 0, tank) * width)

    low, mid, high = cells(litres - sigma), cells(litres), cells(litres + sigma)
    colour = CRITICAL[1] if litres < FUEL_RESERVE else WARNING[1] if litres < FUEL_LOW else FILL
    return Text.assemble(("█" * low, colour), ("▓" * (mid - low), colour), ("▒" * (high - mid), colour),
                         ("░" * (width - high), TRACK))


def figure(value, unit, digits=1):
    """('12.3', 'bold'), (' unit', 'dim') parts for Text.assemble; '--' when there's no value."""
    shown = (f"{value:.{digits}f}", "bold") if value is not None else ("--", "dim")
    return shown, (f" {unit}", "dim")


def fuel_section(mixture_stats, values, fuel, width):
    """Fuel left (big), range, consumption now and this trip, and the air-fuel mixture."""
    parts = []
    if not fuel or fuel["litres"] is None:
        parts += [caption("fuel left"), Text("waiting for fuel data...\n\n\n\n", style="dim")]
    else:
        litres, sigma = fuel["litres"], fuel["sigma"] or 0.0
        number = big_number(int(litres), 2, 2 if width >= big_number_width(2, 2) + 12 else 1,
                            suffix=f".{int(litres * 10) % 10} L")
        number.append(f"  ±{sigma:.1f}", style="dim")
        status = Text("")
        if litres < FUEL_RESERVE:
            status = Text(f"{CRITICAL[0]} RESERVE", style=CRITICAL[1])
        elif litres < FUEL_LOW:
            status = Text(f"{WARNING[0]} LOW FUEL", style=WARNING[1])
        sensor = (f"sensor {fuel['sensor_litres']:.1f} L ({fuel['sensor_pct']:.0f}%)"
                  if fuel["sensor_litres"] is not None else "")
        parts += [Text.assemble(caption("fuel left"), "  ", status), number,
                  tank_gauge(litres, sigma, fuel["tank"], max(10, width)), Text(sensor, style="dim")]

    if fuel:
        now = fuel["now_l_per_100km"]
        now_parts = figure(now, "L/100km") if now is not None else (("stopped", "dim"),)
        parts.append(Text.assemble(("RANGE ", LABEL), *figure(fuel["range_km"], "km", 0)))
        parts.append(Text.assemble(("NOW   ", LABEL), *now_parts, ("  ·  ", "dim"), *figure(fuel["flow_lph"], "L/h")))
        used = "L used" if width >= 34 else "L"  # keep it on one line in narrow windows
        parts.append(Text.assemble(("TRIP  ", LABEL), *figure(fuel["trip_l_per_100km"], "L/100km"), ("  ·  ", "dim"),
                                   *figure(fuel["used_trip"], used, 2)))
    if mixture_stats:
        parts += [Text(""), caption("mixture")]
        rows = Table.grid(expand=True, padding=(0, 1))
        rows.add_column(no_wrap=True)
        rows.add_column(justify="right", no_wrap=True)
        rows.add_column(no_wrap=True)
        bar_width = max(6, width - 26)
        for stat in mixture_stats:
            value = as_number(values.get(stat.label))
            shown = value_text(stat, value) if value is not None else Text("--", style="dim")
            status = status_text(stat, value, quiet_ok=True) if value is not None else Text("")
            rows.add_row(caption(stat.short or stat.label), shown,
                         Text.assemble(hbar(stat, value, None, bar_width), " ", status))
        parts.append(rows)
    return Group(*parts)


# ---------------------------------------------------------------- STATUS bar


def battery_text(battery):
    """'BATTERY 13.7 V ✔ CHARGING alternator OK · START DIP 10.4 V ✔' for the status bar."""
    if not battery:
        return Text("")
    line = Text.assemble(("BATTERY ", LABEL), (f"{battery['volts']:.1f}", "bold"), (" V ", "dim"))
    if battery["state"]:
        icon, colour = LEVELS[battery["level"]]
        line.append(f"{icon} {battery['state']}", style=colour)
        line.append(f" {battery['detail']}", style="dim")
    crank = battery["crank"]
    if crank:
        line.append("  ·  START DIP ", style=LABEL)
        line.append(f"{crank['min_volts']:.1f}", style="bold")
        line.append(" V ", style="dim")
        if crank["level"] in LEVELS:
            icon, colour = LEVELS[crank["level"]]
            line.append(f"{icon} {crank['text']}", style=colour)
        else:
            line.append(crank["text"], style="dim")
    if battery["sources_note"]:
        line.append(f"  ·  {WARNING[0]} {battery['sources_note']}", style=WARNING[1])
    return line


def status_items(status_stats, values):
    """'TRIP 12.4 km   AVG 42 km/h   RUN 12:34 min ...' for the status bar."""
    line = Text()
    for stat in status_stats:
        value = as_number(values.get(stat.label))
        if value is None:
            continue
        if line:
            line.append("   ")
        line.append(f"{(stat.short or stat.label).upper()} ", style=LABEL)
        line += value_text(stat, value)
        status = status_text(stat, value, quiet_ok=True)
        if status:
            line.append(" ")
            line += status
    return line


# ---------------------------------------------------------------- alerts


SECTIONS = {"power": "POWER", "meters": "POWER", "heat": "HEAT", "mixture": "FUEL", "fuel": "FUEL"}


def critical_alerts(stats, values, battery=None, fuel=None):
    """[(section, '✖ COOLANT OVERHEATING 118 °C'), ...] for everything critical right now.
    RPM is left out: redline is a brief, expected moment when revving and the shift lights show it."""
    alerts = []
    for stat in stats:
        value = as_number(values.get(stat.label))
        if stat.label == "RPM" or value is None:
            continue
        status = status_of(stat, value)
        if status and status[0] == CRITICAL:
            text = f"{CRITICAL[0]} {stat.label.upper()} {status[1]}  {value_text(stat, value).plain}"
            alerts.append((SECTIONS.get(stat.group, "STATUS"), text))
    if fuel and fuel["litres"] is not None and fuel["litres"] < FUEL_RESERVE:
        alerts.append(("FUEL", f"{CRITICAL[0]} FUEL RESERVE  {fuel['litres']:.1f} L"))
    if battery and battery["level"] == "critical":
        alerts.append(("STATUS", f"{CRITICAL[0]} BATTERY {battery['state']}  {battery['volts']:.1f} V"))
    if battery and battery["crank"] and battery["crank"]["level"] == "critical":
        alerts.append(("STATUS", f"{CRITICAL[0]} BATTERY WEAK  start dip {battery['crank']['min_volts']:.1f} V"))
    return alerts


# ---------------------------------------------------------------- whole screen


def render_dashboard(stats, values, peaks, trends, fuel, battery, warm, rpm_limit, header, took):
    """HEAT | POWER | FUEL side by side, a STATUS bar underneath.

    A section's frame turns red while something in it is critical; the STATUS bar then names it.
    """
    shown = [s for s in stats if values.get(s.label) != NOT_SUPPORTED]  # hide what this car doesn't report
    hidden = len(stats) - len(shown)
    by_group = {g: [s for s in shown if s.group == g] for g in ("heat", "mixture", "status")}
    w, h = console.width, console.height
    alerts = critical_alerts(shown, values, battery, fuel)
    alerted = {name for name, _ in alerts}

    # Columns in the ratio 10 : 20 : 12 (POWER widest); content width = column - 2 borders - 2 padding.
    usable = w - 2  # 1-character gaps between the three columns
    heat_w, power_w, fuel_w = (usable * r // 42 - 4 for r in (10, 20, 12))
    # POWER is the tallest column: LEDs 3 + RPM/GEAR/SPEED 7 + torque/power 2 + meters (height + 2) +
    # sprint 1, plus the top line, section borders, the status bar and the hint line.
    meter_height = max(3, min(8, h - (1 + 2 + 3 + 7 + 2 + 2 + 1 + 4 + 1)))

    columns = Table.grid(expand=True, padding=(0, 1))
    columns.add_column(ratio=10)
    columns.add_column(ratio=20)
    columns.add_column(ratio=12)
    columns.add_row(
        section("HEAT", heat_section(by_group["heat"], values, trends, warm, heat_w), "HEAT" in alerted),
        section("POWER", power_section(shown, values, peaks, power_w, meter_height, rpm_limit), "POWER" in alerted),
        section("FUEL", fuel_section(by_group["mixture"], values, fuel, fuel_w), "FUEL" in alerted),
    )

    status_body = Group(battery_text(battery), status_items(by_group["status"], values))
    if alerts:
        title = Text(f" {'   '.join(text for _, text in alerts)} ", style=f"bold white on {CRITICAL[1]}")
        status = Panel(status_body, title=title, title_align="left", border_style=CRITICAL[1], box=box.HEAVY)
    else:
        status = Panel(status_body, title=Text(" STATUS ", style="bold"), title_align="left", border_style=FRAME,
                       box=box.ROUNDED)

    top = Table.grid(expand=True)
    top.add_column()
    top.add_column(justify="right")
    top.add_row(Text.assemble(("CAR DASHBOARD  ", "bold"), (header, "")),
                Text(f"{datetime.now():%H:%M:%S} · updated in {took:.2f}s", style="dim"))
    extra = f" · {hidden} not reported by this car (hidden)" if hidden else ""
    hint = Text(f"━ │ = peak this drive · Ctrl+C quits{extra}", style="dim")
    return Group(top, columns, status, hint)


def render_all(values, header, took):
    """Plain table of every stat the car supports (--all)."""
    table = Table(expand=True, border_style=FRAME)
    table.add_column("Stat")
    table.add_column("Value", justify="right")
    for label, text in values.items():
        table.add_row(label, text)
    return Group(Text(header, style="bold"), table, Text(f"Updated in {took:.2f}s · Ctrl+C to quit", style="dim"))


def print_probe_report(report):
    """Readable summary of a --probe scan: one panel per module that answered."""
    modules = report["modules"]
    console.print(Text(f"\n{len(modules)} module(s) answered · {report['protocol']}", style="bold"))
    if len(modules) <= 1:
        console.print(Text("Only one module answered: the car's other controllers (e.g. the gearbox) don't share "
                           "data over standard OBD-II (theirs would need the maker's own codes).", style=WARNING[1]))
    for address, module in modules.items():
        info = Table.grid(padding=(0, 2))
        info.add_column(style=LABEL, no_wrap=True)
        info.add_column()
        info.add_row("Name", module["name"] or "(not reported)")
        info.add_row("Software ID", module["calibration_id"] or "(not reported)")
        gear = module["gear_pid_a4"]
        if gear is None:
            info.add_row("Gear PID A4", Text("not answered", style="dim"))
        else:
            ratio = f" · gear ratio {gear['gear_ratio']:.3f}" if gear["gear_ratio"] is not None else ""
            info.add_row("Gear PID A4", Text(f"ANSWERED  raw {gear['raw']}{ratio}", style=f"bold {GOOD[1]}"))
        info.add_row("Trouble codes", ", ".join(module["stored_codes"]) or "none")
        info.add_row("Pending codes", ", ".join(module["pending_codes"]) or "none")
        live = module["supported_live_data"]
        info.add_row(f"Live data ({len(live)})", "\n".join(live) or "none")
        title = Text(f" {address} · usually {module['usually']} ", style="bold")
        console.print(Panel(info, title=title, title_align="left", border_style=FRAME))

