"""
How the car dashboard looks: colours, big digits, bar meters and the race-dash layout.

Nothing here talks to the car. Every function gets the stat definitions and current values passed
in and returns something `rich` can draw, so the look can be changed (or tested with fake values)
without touching the connection logic in car_dashboard.py.

Values follow one convention throughout: a number, None (no reading yet) or NOT_SUPPORTED.
"""

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
LABEL = "grey62"  # small captions
LIT = "grey93"  # lit part of the RPM sweep
REDZONE_UNLIT = "#4a1d1d"  # the RPM sweep's red zone when not reached

# Status levels: (icon, colour). Always shown with a text label, never colour alone.
GOOD, WARNING, CRITICAL = ("✔", "#0ca30c"), ("▲", "#fab219"), ("✖", "#d03b3b")

V_CHARS = " ▁▂▃▄▅▆▇█"  # vertical bar in eighths of a character

# 3-line "digital" digits for the big numbers
BIG_DIGITS = {
    "0": ("█▀█", "█ █", "▀▀▀"), "1": ("▀█ ", " █ ", "▀▀▀"), "2": ("▀▀█", "█▀▀", "▀▀▀"),
    "3": ("▀▀█", " ▀█", "▀▀▀"), "4": ("█ █", "▀▀█", "  ▀"), "5": ("█▀▀", "▀▀█", "▀▀▀"),
    "6": ("█▀▀", "█▀█", "▀▀▀"), "7": ("▀▀█", "  █", "  ▀"), "8": ("█▀█", "█▀█", "▀▀▀"),
    "9": ("█▀█", "▀▀█", "▀▀▀"), "-": ("   ", "▀▀▀", "   "), " ": ("   ", "   ", "   "),
}

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
    number_text = f"{value:,.0f}" if abs(value) >= 1000 else f"{value:.{stat.decimals}f}"
    unit = UNIT_LABELS.get(stat.unit, stat.unit)
    return Text.assemble((number_text, "bold"), (f" {unit}" if unit else "", "dim"))


def caption(text):
    return Text(text.upper(), style=LABEL)


def big_number(value, digits, scale, suffix=""):
    """Value as 3 lines of block 'digital' digits (scale=2 doubles the width); suffix on the last line."""
    text = (value if isinstance(value, str) else f"{round(value)}").rjust(digits)[-digits:]
    out = Text(style="bold")
    for row in range(3):
        glyphs = ["".join(ch * scale for ch in BIG_DIGITS.get(c, BIG_DIGITS[" "])[row]) for c in text]
        out.append((" " * scale).join(glyphs))
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


def rpm_sweep(rpm_stat, rpm, width, rows=4):
    """Rising bar 'sweep' standing in for a round tachometer, with 1/2/3... (x1000 rpm) above it.

    Bars get taller from left to right and light up to the current RPM. The zones come from the
    RPM stat's own alert rules: white while OK, amber in the WARNING zone, red in the CRITICAL zone.
    The red zone stays dimly red even when unlit, like a painted redline.
    """
    high = rpm_stat.high

    def zone(at_rpm):
        status = status_of(rpm_stat, at_rpm)
        return status[0] if status else GOOD

    marks, mark_styles = [" "] * width, [LABEL] * width
    for k in range(1, int(high // 1000) + 1):
        i = min(width - 1, round(k * 1000 / high * width) - 1)
        marks[i] = str(k)
        mark_styles[i] = CRITICAL[1] if zone(k * 1000) == CRITICAL else LABEL
    lines = [Text.assemble(*zip(marks, mark_styles))]

    for r in range(rows):
        line = Text()
        for i in range(width):
            rpm_here = (i + 1) * high / width
            eighths = int(min(max((i + 1) / width * rows * 8 - (rows - 1 - r) * 8, 0), 8))
            lit = rpm is not None and rpm >= rpm_here
            level = zone(rpm_here)
            if level == CRITICAL:
                colour = CRITICAL[1] if lit else REDZONE_UNLIT
            elif lit:
                colour = WARNING[1] if level == WARNING else LIT
            else:
                colour = TRACK
            line.append(V_CHARS[eighths] if eighths else " ", style=colour)
        lines.append(line)
    return Group(*lines)


def histogram(history, low, high, width, rows=3):
    """Mini bar chart of recent values (newest on the right)."""
    points = [v for v in history if as_number(v) is not None][-width:]
    points = [None] * (width - len(points)) + points
    lines = []
    for r in range(rows):
        line = Text()
        for v in points:
            level = 0 if v is None else fraction(v, low, high) * rows * 8
            eighths = int(min(max(level - (rows - 1 - r) * 8, 0), 8))
            baseline = "▁" if r == rows - 1 else " "
            line.append(V_CHARS[eighths] if eighths else baseline, style=FILL if eighths else TRACK)
        lines.append(line)
    return Group(*lines)


# ---------------------------------------------------------------- sections of the race dash


def dash_left(temps, values, width):
    """Temperatures: coolant + oil in big digits, the rest as smaller rows."""
    fluids = [s for s in temps if s.label in ("Coolant", "Oil")]
    others = [s for s in temps if s not in fluids]
    scale = 2 if width >= big_number_width(3, 2) + 14 else 1
    parts = []
    for stat in fluids:
        value = as_number(values.get(stat.label))
        parts.append(caption(stat.label))
        if value is None:
            parts.append(Text(f"{stat.empty}\n\n\n", style="dim"))
        else:
            parts.append(big_number(value, 3, scale, suffix=" °C"))
            parts.append(status_text(stat, value))
    rows = Table.grid(expand=True, padding=(0, 1))
    rows.add_column(no_wrap=True)
    rows.add_column(justify="right", no_wrap=True)
    rows.add_column(no_wrap=True)
    for stat in others:
        value = as_number(values.get(stat.label))
        if value is None:
            rows.add_row(caption(stat.label), Text(stat.empty, style="dim"), "")
        else:
            rows.add_row(caption(stat.label), value_text(stat, value), status_text(stat, value))
    parts.append(rows)
    return Group(*parts)


def dash_center(stats, values, width):
    """RPM sweep, then big GEAR and SPEED."""
    rpm_stat = find(stats, "RPM")
    rpm, gear, speed = (as_number(values.get(k)) for k in ("RPM", "Gear (estimated)", "Speed"))
    gear_text = "-" if not gear else str(int(gear))  # "-" = stopped or shifting
    rpm_line = Text.assemble((f"{rpm:,.0f}" if rpm is not None else "--", "bold"), (" RPM  ", "dim"))
    if rpm is not None:
        rpm_line += status_text(rpm_stat, rpm, quiet_ok=True)
    return Group(
        rpm_sweep(rpm_stat, rpm, width),
        Align.center(rpm_line),
        Align.center(caption("gear")),
        Align.center(big_number(gear_text, 1, 2)),
        Text(""),
        Align.center(big_number(speed if speed is not None else "-", 3, 2, suffix=" km/h")),
    )


def dash_right(stats, values, throttle_history, width):
    """Sprint times, acceleration, throttle history, trip."""
    def caption_and_value(name, stat_label):
        stat = find(stats, stat_label)
        value = as_number(values.get(stat_label))
        shown = value_text(stat, value) if value is not None else Text(stat.empty, style="dim")
        return [caption(name), shown]

    distance, avg = as_number(values.get("Trip distance")), as_number(values.get("Trip avg speed"))
    trip = Text.assemble((f"{distance:.1f}" if distance is not None else "--", "bold"), (" km  ·  avg ", "dim"),
                         (f"{avg:.0f}" if avg is not None else "--", "bold"), (" km/h", "dim"))
    return Group(
        *caption_and_value("0-60 best", "0-60 km/h best"),
        *caption_and_value("0-100 best", "0-100 km/h best"),
        *caption_and_value("acceleration", "Acceleration"),
        caption("throttle · recent"),
        histogram(throttle_history, 0, 100, max(10, width)),
        caption("trip"),
        trip,
    )


def dash_footer(car_stats, values):
    """One line of slow-changing car info, like the mode line at the bottom of a race dash."""
    line = Text()
    for stat in car_stats:
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


def engine_panel(engine_stats, values, peaks, height, width):
    """Engine stats as a row of vertical bar meters: value on top, bar, short name, status."""
    column = max(6, min(12, (width - 4) // len(engine_stats) - 1))
    bar_width = max(2, column - 4)
    table = Table.grid(expand=True, padding=(0, 1))
    for _ in engine_stats:
        table.add_column(justify="center", ratio=1, no_wrap=True)
    tops, bars, names, statuses = [], [], [], []
    for stat in engine_stats:
        value = as_number(values.get(stat.label))
        tops.append(value_text(stat, value) if value is not None else Text("--", style="dim"))
        bars.append(vertical_bar(stat, value, peaks.get(stat.label), height, bar_width))
        names.append(Text(stat.short or stat.label, style=LABEL))
        statuses.append(status_text(stat, value, quiet_ok=True) if value is not None else Text(""))
    table.add_row(*tops)
    for r in range(height):
        table.add_row(*[bar[r] for bar in bars])
    table.add_row(*names)
    table.add_row(*statuses)
    return Panel(table, title=Text("ENGINE", style="bold"), title_align="left", border_style=FRAME,
                 box=box.ROUNDED)


# ---------------------------------------------------------------- whole screens


def render_dashboard(stats, values, peaks, throttle_history, header, took):
    """The race dash (temperatures | RPM, gear, speed | sprint & trip) with engine meters below."""
    shown = [s for s in stats if values.get(s.label) != NOT_SUPPORTED]  # hide what this car doesn't report
    hidden = len(stats) - len(shown)
    temps = [s for s in shown if s.group == "temps"]
    engine = [s for s in shown if s.group == "engine"]
    car = [s for s in shown if s.group == "car"]
    w, h = console.width, console.height

    # The dash: three columns (1 : 2 : 1) inside one heavy panel, car info line at the bottom.
    inner = w - 4
    side, middle = inner // 4 - 2, inner // 2 - 2
    dash = Table.grid(expand=True, padding=(0, 2))
    dash.add_column(ratio=1)
    dash.add_column(ratio=2)
    dash.add_column(ratio=1)
    dash.add_row(dash_left(temps, values, side), dash_center(stats, values, middle),
                 dash_right(stats, values, throttle_history, side))
    updated = Text(f"{datetime.now():%H:%M:%S} · updated in {took:.2f}s", style="dim")
    dash_panel = Panel(
        Group(dash, Rule(style=TRACK), dash_footer(car, values)),
        title=Text(f" {header} ", style="bold"), title_align="left", subtitle=updated, subtitle_align="right",
        border_style=FRAME, box=box.HEAVY,
    )

    # Engine meters below, as tall as the window allows (dash ~19 lines, engine panel = bars + 5).
    parts = [dash_panel]
    if engine:
        bar_height = max(3, min(8, h - 19 - 5 - 1))
        parts.append(engine_panel(engine, values, peaks, bar_height, w))
    extra = f" · {hidden} not reported by this car (hidden)" if hidden else ""
    parts.append(Text(f"━ = peak this drive · Ctrl+C quits{extra}", style="dim"))
    return Group(*parts)


def render_all(values, header, took):
    """Plain table of every stat the car supports (--all)."""
    table = Table(expand=True, border_style=FRAME)
    table.add_column("Stat")
    table.add_column("Value", justify="right")
    for label, text in values.items():
        table.add_row(label, text)
    return Group(Text(header, style="bold"), table, Text(f"Updated in {took:.2f}s · Ctrl+C to quit", style="dim"))
