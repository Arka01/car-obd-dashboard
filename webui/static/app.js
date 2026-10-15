// Web dashboard: receives one snapshot per update from /stream (Server-Sent Events) and draws it.
// The snapshot's shape is built in webui/server.py (Pipeline.process).
//
// A car's own design (cars/<car>/ui/index.html) can reuse this file: sections it leaves out (e.g. no
// FUEL panel) are skipped, window.METERS = [labels] picks the bar meters, and window.extraRender(s)
// is called after every update to draw the design's own parts.

const $ = (id) => document.getElementById(id);
const LED_COUNT = 12;
const SEGMENTS = 48;
const FLUIDS = ["Coolant", "Oil"];
const TEMP_GOOD = [80, 105]; // °C: the normal warm band, highlighted on the coolant/oil scales

let voiceReady = false;
let lastSaid = 0; // id of the last announcement spoken or skipped
let built = false;
let wakeLock = null;

// ---------------------------------------------------------------- start: full screen, screen on, voice
// ?autostart in the address skips the tap screen (no full screen or voice then: browsers need a tap).
if (new URLSearchParams(location.search).has("autostart")) $("start").classList.add("hidden");
$("start").addEventListener("click", async () => {
  $("start").classList.add("hidden");
  try { await document.documentElement.requestFullscreen?.(); } catch (e) { /* not allowed: fine */ }
  keepScreenOn();
  if ("speechSynthesis" in window) {
    speechSynthesis.speak(new SpeechSynthesisUtterance("")); // browsers only allow speech after a tap
    voiceReady = true;
  }
});

async function keepScreenOn() {
  try { wakeLock = await navigator.wakeLock?.request("screen"); } catch (e) { /* unsupported */ }
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && wakeLock !== null) keepScreenOn();
});

// ALL SENSORS / DASHBOARD tab
$("view").addEventListener("click", () => {
  const all = document.body.classList.toggle("show-all");
  $("ALL").classList.toggle("hidden", !all);
  $("view").textContent = all ? "DASHBOARD" : "ALL SENSORS";
});

// ---------------------------------------------------------------- data
const stream = new EventSource("/stream");
stream.onmessage = (event) => {
  const snapshot = JSON.parse(event.data);
  $("link").classList.remove("down");
  if (snapshot.stats) render(snapshot);
};
stream.onerror = () => $("link").classList.add("down"); // EventSource reconnects by itself

// ---------------------------------------------------------------- helpers
const frac = (v, lo, hi) => Math.min(Math.max((v - lo) / (hi - lo), 0), 1);
const pct = (x) => `${(x * 100).toFixed(2)}%`;
const fixed = (v, d = 1) => (v === null || v === undefined ? "--" : v.toFixed(d));
const ICON = { good: "✔", warning: "▲", critical: "✖" };

function el(tag, cls, html) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

function statusHtml(stat, quietOk = false) {
  if (!stat || !stat.level || (quietOk && stat.level === "good")) return "";
  return `<span class="${stat.level}">${ICON[stat.level]} ${stat.status}</span>`;
}

function trendHtml(rate, withRate = true) {
  if (rate === null || rate === undefined) return "";
  if (Math.abs(rate) < 1) return `<span class="muted">→${withRate ? " steady" : ""}</span>`;
  const arrow = rate > 0 ? "↑" : "↓";
  return `<b>${arrow}${withRate ? ` ${rate > 0 ? "+" : ""}${rate.toFixed(1)}°/min` : ""}</b>`;
}

// RPM zone: from the shift/redline settings and the warm-up advisor's temporary limit.
function zone(rpm, z) {
  let level = rpm > z.redline ? "c" : rpm > z.shift ? "w" : "g";
  if (z.limit !== null && z.limit !== undefined) {
    if (rpm > z.limit) level = "c";
    else if (rpm > z.limit - 500 && level === "g") level = "w";
  }
  return level;
}

// Horizontal bar (fills from the left, or grows from `center`), with a peak marker.
function setHBar(bar, stat) {
  const fill = bar.querySelector(".fill");
  const v = stat && stat.value;
  if (v === null || v === undefined) { fill.style.width = "0"; return; }
  const pos = (x) => frac(x, stat.low, stat.high);
  const start = stat.center !== null ? pos(stat.center) : 0;
  const end = pos(v);
  fill.style.left = pct(Math.min(start, end));
  fill.style.width = pct(Math.abs(end - start));
  const zero = bar.querySelector(".zero");
  if (zero) zero.style.left = pct(start);
  const peak = bar.querySelector(".peak");
  if (peak) peak.style.left = stat.peak !== null && stat.center === null ? pct(pos(stat.peak)) : "-10px";
}

// ---------------------------------------------------------------- build the parts that depend on the car
function build(s) {
  const leds = $("leds");
  for (let i = 0; i < LED_COUNT; i++) leds.appendChild(el("div", "led"));
  const band = $("revband");
  for (let i = 0; i < SEGMENTS; i++) {
    const seg = el("div", "seg");
    seg.style.height = `${28 + 72 * Math.pow((i + 1) / SEGMENTS, 1.4)}%`; // rising, like an M rev band
    band.appendChild(seg);
  }
  const max = s.stats.RPM ? s.stats.RPM.high : 5000;
  for (let k = 0; k <= Math.floor(max / 1000); k++) {
    const mark = el("span", "", String(k));
    mark.style.left = pct((k * 1000) / max);
    $("revscale").appendChild(mark);
  }

  for (const label of FLUIDS) {
    if (!s.stats[label] || !$("fluids")) continue;
    const box = el("div", "fluid");
    box.innerHTML = `<div class="name">${label.toUpperCase()}</div>
      <div class="line"><span class="num value" data-v></span><span class="unit">°C</span>
      <div class="side"><div data-trend></div><div data-status></div></div></div>
      <div class="thermo-wrap"><div class="thermo"></div><div class="thermo-mark" data-mark></div></div>`;
    box.dataset.label = label;
    $("fluids").appendChild(box);
  }
  for (const label of ["Torque", "Power"]) {
    if (!s.stats[label] || !$("output")) continue;
    const stat = s.stats[label];
    const box = el("div", "out");
    box.dataset.label = label;
    box.innerHTML = `<div class="head"><label>${stat.short}</label><span class="num val" data-v></span>
      <span class="unit">${stat.unit}</span></div><div class="hbar"><div class="fill"></div><div class="zero"></div></div>`;
    $("output").appendChild(box);
  }
  const meters = Object.values(s.stats).filter((x) => x.group === "meters" && (!window.METERS || window.METERS.includes(x.label)));
  if (window.METERS) meters.sort((a, b) => window.METERS.indexOf(a.label) - window.METERS.indexOf(b.label));
  for (const stat of $("meters") ? meters : []) {
    const box = el("div", "meter");
    box.dataset.label = stat.label;
    box.innerHTML = `<div class="mval" data-v></div><div class="vbar"><div class="fill"></div>
      ${stat.center !== null ? '<div class="zero"></div>' : ""}<div class="peak"></div></div><div class="mname">${stat.short}</div>`;
    $("meters").appendChild(box);
  }
  built = true;
}

// ---------------------------------------------------------------- draw one snapshot
function render(s) {
  if (!built) build(s);
  const st = s.stats;
  $("brand").textContent = s.car.short;
  document.title = `${s.car.short} · Car Dashboard`;
  $("header").textContent = s.header;
  $("clock").textContent = s.clock;
  $("took").textContent = `updated in ${s.took.toFixed(2)}s`;

  // Alerts: the affected section glows red, the banner names the problem.
  const alerted = new Set(s.alerts.map((a) => a.section));
  for (const id of ["HEAT", "POWER", "FUEL", "STATUS"]) $(id)?.classList.toggle("alert", alerted.has(id));
  $("banner").classList.toggle("hidden", s.alerts.length === 0);
  $("banner").textContent = s.alerts.map((a) => a.text).join("     ");

  if ($("HEAT")) renderHeat(s, st);
  if ($("POWER")) renderPower(s, st);
  if ($("FUEL")) renderFuel(s, st);
  renderStatus(s, st);
  if (!$("ALL").classList.contains("hidden")) renderAll(s, st);
  if (window.extraRender) window.extraRender(s);
  speak(s.say);
}

// Every value the car reports: the dashboard's own stats plus everything else, grouped like the panels.
const PANEL_OF = { power: "POWER", meters: "POWER", turbo: "POWER", heat: "HEAT", mixture: "FUEL", fuel: "FUEL",
                   engine: "OTHER", status: "OTHER" };
function renderAll(s, st) {
  const groups = { POWER: [], HEAT: [], FUEL: [], OTHER: [] };
  for (const x of Object.values(st)) {
    groups[PANEL_OF[x.group] || "OTHER"].push({ label: x.label, text: x.text !== null ? `${x.text} ${x.unit}` : null });
  }
  for (const x of s.extra) groups[x.group].push(x);
  const count = Object.values(groups).reduce((n, g) => n + g.length, 0);
  $("allcount").textContent = `· ${count} values from this car`;
  $("allgroups").innerHTML = Object.entries(groups).filter(([, items]) => items.length).map(([name, items]) =>
    `<div><h3>${name}</h3>${items.map((x) => `<div class="item"><span>${x.label}</span>` +
      `<b class="${x.text && x.text.length > 18 ? "long" : ""}">${x.text ?? "--"}</b></div>`).join("")}</div>`).join("");
}

function renderHeat(s, st) {
  const engine = $("engine");
  engine.className = `engine-state ${s.engine ? s.engine.level : ""}`;
  engine.innerHTML = s.engine ? `<b>${ICON[s.engine.level]} ${s.engine.state}</b><span>${s.engine.detail}</span>`
                              : `<b class="muted">ENGINE OFF</b><span>&nbsp;</span>`;
  for (const box of document.querySelectorAll(".fluid")) {
    const stat = st[box.dataset.label];
    box.querySelector("[data-v]").textContent = stat && stat.text !== null ? stat.text : "--";
    box.querySelector("[data-trend]").innerHTML = trendHtml(s.trends[box.dataset.label]);
    box.querySelector("[data-status]").innerHTML = statusHtml(stat);
    box.querySelector("[data-mark]").style.left = stat && stat.value !== null ? pct(frac(stat.value, stat.low, stat.high)) : "-10px";
  }
  const rows = Object.values(st).filter((x) => x.group === "heat" && !FLUIDS.includes(x.label));
  $("temps").innerHTML = rows.map((x) => `<div class="rname">${x.short.toUpperCase()}</div>
    <div class="rval">${x.text !== null ? `${x.text} <small>${x.unit}</small>` : `<small>${x.empty}</small>`}</div>
    <div class="rextra">${trendHtml(s.trends[x.label], false)} ${statusHtml(x, true)}</div>`).join("");
}

function renderPower(s, st) {
  const rpmStat = st.RPM;
  const rpm = rpmStat ? rpmStat.value : null;
  const max = rpmStat ? rpmStat.high : 5000;
  const z = s.rpm;

  // Shift lights: blink when at/over the limit (shift up / ease off).
  const leds = $("leds").children;
  for (let i = 0; i < leds.length; i++) {
    const at = ((i + 1) * max) / leds.length;
    const level = zone(at, z);
    const on = rpm !== null && rpm >= at - max / leds.length / 2;
    leds[i].className = `led ${level === "c" ? "zone-c" : ""} ${on ? `on ${level}` : ""}`;
  }
  $("leds").classList.toggle("blink", rpm !== null && zone(rpm, z) === "c");

  // Rev band: lit up to the RPM; the red zone (incl. the cold limit) painted even when off.
  const segs = $("revband").children;
  for (let i = 0; i < segs.length; i++) {
    const at = ((i + 1) * max) / segs.length;
    const level = zone(at, z);
    const on = rpm !== null && rpm >= at;
    segs[i].className = `seg ${level === "c" ? "zone-c" : ""} ${on ? `on ${level}` : ""}`;
  }
  for (const mark of $("revscale").children) mark.className = zone(Number(mark.textContent) * 1000, z) === "c" ? "c" : "";

  $("rpm").textContent = rpm !== null ? Math.round(rpm).toLocaleString("en-US") : "--";
  let warn = "";
  if (rpm !== null && z.limit !== null && rpm > z.limit) warn = `<span class="critical">✖ KEEP BELOW ${z.limit.toLocaleString("en-US")} · ENGINE COLD</span>`;
  else warn = statusHtml(rpmStat, true);
  $("rpmwarn").innerHTML = warn;

  const gear = st["Gear (estimated)"];
  $("gear").textContent = gear && gear.value ? String(Math.round(gear.value)) : "-";
  $("speed").textContent = st.Speed && st.Speed.value !== null ? Math.round(st.Speed.value) : "--";

  for (const box of $("output")?.children ?? []) {
    const stat = st[box.dataset.label];
    box.querySelector("[data-v]").textContent = stat && stat.text !== null ? stat.text : "--";
    setHBar(box.querySelector(".hbar"), stat);
  }
  for (const box of $("meters")?.children ?? []) {
    const stat = st[box.dataset.label];
    box.classList.toggle("hidden", !stat);
    if (!stat) continue;
    box.querySelector("[data-v]").innerHTML = stat.text !== null ? `${stat.text}<small> ${stat.unit}</small>` : "--";
    setVBar(box.querySelector(".vbar"), stat);
  }

  const sprint = ["0-60 km/h best", "0-100 km/h best", "Acceleration"].filter((l) => st[l]);
  if ($("sprint")) $("sprint").innerHTML = sprint.map((l) => `<div><span>${st[l].short}</span><b>${st[l].text ?? "--"}</b> ${st[l].text !== null ? st[l].unit : ""}</div>`).join("");
}

// Vertical bar meter: fills from the bottom, or up/down from `center`; thin line at the peak.
function setVBar(bar, stat) {
  const fill = bar.querySelector(".fill");
  const pos = (x) => frac(x, stat.low, stat.high);
  if (stat.value === null) { fill.style.height = "0"; return; }
  const start = stat.center !== null ? pos(stat.center) : 0;
  const end = pos(stat.value);
  fill.style.bottom = pct(Math.min(start, end));
  fill.style.height = pct(Math.abs(end - start));
  const zero = bar.querySelector(".zero");
  if (zero) zero.style.bottom = pct(start);
  const peak = bar.querySelector(".peak");
  peak.style.bottom = stat.peak !== null && stat.center === null && stat.peak > stat.value ? pct(pos(stat.peak)) : "-10px";
}

function renderFuel(s, st) {
  const f = s.fuel;
  const ready = f && f.litres !== null;
  if (!s.car.tank_known) { // no tank size in car.json: show the tank sensor's % instead of litres
    const level = st["Fuel level"];
    $("litres").textContent = level && level.value !== null ? level.value.toFixed(0) : "--";
    $("litresunit").textContent = " %";
    $("tankfill").style.width = level && level.value !== null ? pct(frac(level.value, 0, 100)) : "0";
    $("sensor").textContent = "tank size not set: add tank_litres to car.json for litres and range";
  } else {
    $("litres").textContent = ready ? f.litres.toFixed(1) : "--";
  }
  $("sigma").textContent = ready ? `±${(f.sigma || 0).toFixed(1)}` : "";
  const tank = document.querySelector(".tank");
  tank.classList.toggle("low", ready && f.litres < 8 && f.litres >= 4);
  tank.classList.toggle("reserve", ready && f.litres < 4);
  $("fuelstatus").innerHTML = !ready ? "" : f.litres < 4 ? '<span class="critical">✖ RESERVE</span>'
                                          : f.litres < 8 ? '<span class="warning">▲ LOW FUEL</span>' : "";
  if (ready) {
    const t = f.tank, sig = f.sigma || 0;
    $("tankfill").style.width = pct(frac(f.litres - sig, 0, t));
    $("tankband").style.left = pct(frac(f.litres - sig, 0, t));
    $("tankband").style.width = pct(frac(f.litres + sig, 0, t) - frac(f.litres - sig, 0, t));
  }
  if (s.car.tank_known) $("sensor").textContent = ready && f.sensor_litres !== null ? `tank sensor ${f.sensor_litres.toFixed(1)} L (${f.sensor_pct.toFixed(0)}%)` : "";
  $("range").textContent = f ? fixed(f.range_km, 0) : "--";
  $("now").textContent = f ? (f.now_l_per_100km === null ? "stop" : fixed(f.now_l_per_100km)) : "--";
  $("flow").textContent = f ? fixed(f.flow_lph) : "--";
  $("trip").textContent = f ? fixed(f.trip_l_per_100km) : "--";
  $("used").textContent = f ? fixed(f.used_trip, 2) : "--";

  const rows = Object.values(st).filter((x) => x.group === "mixture");
  $("mixture").innerHTML = rows.map((x) => `<div class="rname">${x.short}</div>
    <div class="rval">${x.text ?? "--"} <small>${x.unit}</small></div>
    <div class="rextra"><div class="cbar" data-label="${x.label}"><div class="fill"></div><div class="zero"></div></div>${statusHtml(x, true)}</div>`).join("");
  for (const bar of $("mixture").querySelectorAll(".cbar")) setHBar(bar, st[bar.dataset.label]);
}

function renderStatus(s, st) {
  const b = s.battery;
  let html = "";
  if (b) {
    html = `<span class="muted">BATTERY</span> <b>${b.volts.toFixed(1)}</b> V`;
    if (b.state) html += ` <span class="${b.level}">${ICON[b.level]} ${b.state}</span> <span class="muted">${b.detail}</span>`;
    if (b.crank) {
      const lvl = ICON[b.crank.level] ? `<span class="${b.crank.level}">${ICON[b.crank.level]} ${b.crank.text}</span>` : `<span class="muted">${b.crank.text}</span>`;
      html += ` · <span class="muted">START DIP</span> <b>${b.crank.min_volts.toFixed(1)}</b> V ${lvl}`;
    }
    if (b.sources_note) html += ` · <span class="warning">▲ ${b.sources_note}</span>`;
  }
  $("battery").innerHTML = html;
  const items = Object.values(st).filter((x) => x.group === "status" && x.text !== null);
  $("status").innerHTML = items.map((x) => `<div><span>${x.short.toUpperCase()}</span><b>${x.text}</b> ${x.unit} ${statusHtml(x, true)}</div>`).join("");
  $("hint").textContent = `${s.mode.toUpperCase()}${s.hidden ? ` · ${s.hidden} not reported by this car` : ""}`;
}

// Speak each new warm-up announcement once, on the device showing the dashboard. Browsers only allow
// speech after a tap, so a fresh message waits for it; messages older than 15 s are skipped.
function speak(say) {
  if (!say || say.id === lastSaid) return;
  if (say.age > 15) { lastSaid = say.id; return; }
  if (!voiceReady) return; // spoken on the first snapshot after the tap
  lastSaid = say.id;
  speechSynthesis.speak(new SpeechSynthesisUtterance(say.text));
}
