// XUV700 diesel: draws the TURBO panel and the injection/checks part of ENGINE.
// The shared app.js draws everything else and calls window.extraRender(snapshot) after each update.

const BOOST_SCALE = 2.0; // bar shown full-width on the boost bar; grows if the turbo ever goes past it
const TRACE_SECONDS = 60;
const SPOOL_SECONDS = 180; // dots kept on the spool map
const history = []; // {t, boost, rpm}
let boostMax = BOOST_SCALE;

const num = (stat) => (stat && stat.value !== null && stat.value !== undefined ? stat.value : null);

// Canvas sized to its box in real pixels, so lines stay sharp on phones.
function canvas2d(id) {
  const c = $(id);
  const ratio = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (c.width !== Math.round(w * ratio) || c.height !== Math.round(h * ratio)) {
    c.width = Math.round(w * ratio);
    c.height = Math.round(h * ratio);
  }
  const g = c.getContext("2d");
  g.setTransform(ratio, 0, 0, ratio, 0, 0);
  g.clearRect(0, 0, w, h);
  return { g, w, h };
}

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function drawTicks() {
  const box = $("boostticks");
  box.innerHTML = "";
  for (let b = 0.5; b < boostMax; b += 0.5) {
    const tick = el("span");
    tick.style.left = pct(b / boostMax);
    box.appendChild(tick);
  }
}

// Boost over the last minute: a filled line, with a dashed line at this drive's peak.
function drawTrace(now, peak) {
  const { g, w, h } = canvas2d("trace");
  const y = (b) => h - 4 - (Math.max(b, 0) / boostMax) * (h - 8);
  const x = (t) => w - ((now - t) / TRACE_SECONDS) * w;
  g.strokeStyle = "#1f1f25";
  g.lineWidth = 1;
  for (let b = 0.5; b < boostMax; b += 0.5) { g.beginPath(); g.moveTo(0, y(b)); g.lineTo(w, y(b)); g.stroke(); }
  const points = history.filter((p) => now - p.t <= TRACE_SECONDS && p.boost !== null);
  if (points.length > 1) {
    const fill = g.createLinearGradient(0, 0, 0, h);
    fill.addColorStop(0, "rgba(255,255,255,0.45)");
    fill.addColorStop(1, "rgba(255,255,255,0.02)");
    g.beginPath();
    g.moveTo(x(points[0].t), h);
    for (const p of points) g.lineTo(x(p.t), y(p.boost));
    g.lineTo(x(points[points.length - 1].t), h);
    g.fillStyle = fill;
    g.fill();
    g.beginPath();
    points.forEach((p, i) => (i ? g.lineTo(x(p.t), y(p.boost)) : g.moveTo(x(p.t), y(p.boost))));
    g.strokeStyle = css("--fill");
    g.lineWidth = 2;
    g.stroke();
  }
  if (peak !== null && peak > 0) {
    g.setLineDash([6, 5]);
    g.strokeStyle = css("--accent");
    g.beginPath(); g.moveTo(0, y(peak)); g.lineTo(w, y(peak)); g.stroke();
    g.setLineDash([]);
  }
}

// Boost against RPM: where the turbo starts to work. Older dots fade; the newest is the bright one.
function drawSpool(now, rpmMax) {
  const { g, w, h } = canvas2d("spool");
  const x = (rpm) => (rpm / rpmMax) * w;
  const y = (b) => h - 4 - (Math.max(b, 0) / boostMax) * (h - 8);
  g.fillStyle = css("--muted");
  g.font = "italic 800 10px sans-serif";
  for (let k = 1; k * 1000 < rpmMax; k++) g.fillText(String(k), x(k * 1000) - 3, h - 3);
  const points = history.filter((p) => now - p.t <= SPOOL_SECONDS && p.boost !== null && p.rpm !== null);
  for (const p of points) {
    const age = (now - p.t) / SPOOL_SECONDS;
    g.fillStyle = `rgba(255,255,255,${(0.75 * (1 - age)).toFixed(2)})`;
    g.fillRect(x(p.rpm) - 1.5, y(p.boost) - 1.5, 3, 3);
  }
  const last = points[points.length - 1];
  if (last) {
    g.fillStyle = css("--accent");
    g.beginPath(); g.arc(x(last.rpm), y(last.boost), 5, 0, 2 * Math.PI); g.fill();
  }
}

function drawTurbo(s) {
  const st = s.stats;
  const boost = num(st.Boost), rpm = num(st.RPM);
  const peak = st.Boost ? st.Boost.peak : null;
  const now = Date.now() / 1000;
  history.push({ t: now, boost, rpm });
  while (history.length && now - history[0].t > SPOOL_SECONDS) history.shift();

  if (peak !== null && peak > boostMax) { boostMax = Math.ceil(peak * 2) / 2 + 0.5; drawTicks(); }
  if (!$("boostticks").children.length) drawTicks();

  $("boost").textContent = boost === null ? "--" : boost.toFixed(2);
  document.querySelector(".boost").classList.toggle("vac", boost !== null && boost < 0.05);
  $("boostpeak").textContent = peak !== null && peak > 0 ? `· PEAK ${peak.toFixed(2)}` : "";
  $("boostfill").style.width = boost === null ? "0" : pct(frac(boost, 0, boostMax));
  $("boostpeakmark").style.left = peak !== null && peak > 0 ? pct(frac(peak, 0, boostMax)) : "-10px";

  $("manifold").textContent = fixed(num(st["Manifold pressure"]), 0);
  $("airflow").textContent = fixed(num(st["Air flow (MAF)"]), 1);
  $("chargeair").textContent = fixed(num(st["Intake air"]), 0);

  drawTrace(now, peak);
  drawSpool(now, st.RPM ? st.RPM.high : 5000);
}

let railPeak = null;

function drawEngine(s) {
  const st = s.stats;
  const extra = Object.fromEntries(s.extra.map((x) => [x.label, x]));
  // Rail pressure: the dashboard stat (bar); recordings from before 15 Oct 2026 only have the raw value (kPa).
  let rail = num(st["Fuel rail pressure"]);
  const raw = extra["Fuel Rail Pressure (direct inject)"];
  if (rail === null && raw && raw.value !== null) rail = raw.value / 100;
  if (rail !== null) railPeak = Math.max(railPeak ?? rail, rail);
  $("rail").textContent = rail === null ? "--" : Math.round(rail).toLocaleString("en-US");
  setHBar(document.querySelector(".rail .hbar"), { value: rail, low: 0, high: 2000, center: null, peak: railPeak });

  // "check-engine light off, 0 codes" -> "OFF · 0 CODES"
  const status = extra["Status since DTCs cleared"] ? extra["Status since DTCs cleared"].text : null;
  const m = status && status.match(/light (ON|off), (\d+) codes/);
  $("checks").innerHTML = !status ? "" : `<div class="rname">CHECK ENGINE</div>` +
    `<div class="rval ${m && m[1] === "ON" ? "critical" : ""}">${m ? `${m[1].toUpperCase()} · ${m[2]} CODES` : status}</div><div></div>`;
}

window.extraRender = (s) => {
  // the shared alerts glow "HEAT" for engine problems; TURBO has none of its own yet
  if (!$("TURBO")) return;
  drawTurbo(s);
  drawEngine(s);
};
