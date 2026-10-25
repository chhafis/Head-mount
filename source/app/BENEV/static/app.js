"use strict";

const LIVE_WINDOW_S = 10;
const HISTORY_WINDOW_S = 3600;
const STALE_AFTER_MS = 2000;
const AXES = ["X", "Y", "Z"];

// gap: a jump in time bigger than this (s) breaks the line instead of bridging missing data
const CHARTS = {
  voltage: { title: "Cell voltage", unit: "V", series: ["Voltage"], digits: 3, minSpan: 0.05, window: HISTORY_WINDOW_S, clock: true, area: true, gap: 5 },
  soc:     { title: "State of charge", unit: "%", series: ["Charge"], digits: 1, minSpan: 2, window: HISTORY_WINDOW_S, clock: true, area: true, gap: 5 },
  accel:   { title: "Accelerometer", unit: "g", series: AXES, digits: 3, minSpan: 0.2, window: LIVE_WINDOW_S, gap: 0.25 },
  gyro:    { title: "Gyroscope", unit: "°/s", series: AXES, digits: 2, minSpan: 10, window: LIVE_WINDOW_S, gap: 0.25 },
  mag:     { title: "Magnetic field", unit: "µT", series: AXES, digits: 2, minSpan: 10, window: LIVE_WINDOW_S, gap: 0.25 },
};

const FLAG_LABELS = {
  reset: "Power-on reset",
  over_voltage: "Over-voltage",
  under_voltage: "Under-voltage",
  voltage_reset: "Voltage reset",
  soc_low: "Below alert threshold",
  soc_change: "Charge changed 1 %",
};

// ---------- helpers

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function fmt(v, digits) {
  if (v == null || !Number.isFinite(v)) return "–";
  return v.toFixed(digits).replace("-", "−");
}

function setText(id, text) {
  document.getElementById(id).textContent = text ?? "–";
}

function lowerBound(a, v) {
  let lo = 0, hi = a.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (a[m] < v) lo = m + 1; else hi = m;
  }
  return lo;
}

function nearest(a, v) {
  const i = lowerBound(a, v);
  if (i === 0) return 0;
  if (i >= a.length) return a.length - 1;
  return v - a[i - 1] < a[i] - v ? i - 1 : i;
}

function niceTicks(lo, hi, count) {
  let mag = 10 ** Math.floor(Math.log10((hi - lo) / count));
  const n = (hi - lo) / count / mag;
  let base = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  if (base === 10) { base = 1; mag *= 10; }
  const step = base * mag;
  const a = Math.floor(lo / step + 1e-9) * step;
  const b = Math.ceil(hi / step - 1e-9) * step;
  const decimals = Math.max(0, -Math.round(Math.log10(mag)) + (base === 2.5 ? 1 : 0));
  const values = [];
  for (let k = 0; a + k * step <= b + step / 2 && k < 50; k++) values.push(a + k * step);
  return { lo: a, hi: b, step, decimals, values };
}

function clockLabel(t, seconds) {
  const opts = { hour: "2-digit", minute: "2-digit" };
  if (seconds) opts.second = "2-digit";
  return new Date(t * 1000).toLocaleTimeString([], opts);
}

let colors = {};
function readColors() {
  const cs = getComputedStyle(document.body);
  for (const k of ["surface-1", "text-muted", "grid", "axis", "series-1", "series-2", "series-3"]) {
    colors[k] = cs.getPropertyValue("--" + k).trim();
  }
}

// ---------- chart

class Chart {
  constructor(fig, def) {
    this.def = def;
    this.t = [];
    this.ys = def.series.map(() => []);
    this.pointerX = null;   // mouse/touch hover, in CSS px
    this.kbBack = null;     // keyboard hover, in samples back from the newest
    this.visibleN = 0;
    this.yr = null;
    this.dirty = true;
    this.tableDirty = true;
    this.tableAt = 0;

    const cap = el("figcaption");
    const head = el("div");
    head.append(el("span", "title", def.title), el("span", "unit", def.unit));
    this.toggle = el("button", "table-toggle", "Table");
    this.toggle.type = "button";
    this.toggle.setAttribute("aria-pressed", "false");
    cap.append(head, this.toggle);
    fig.append(cap);

    this.legendVals = [];
    if (def.series.length >= 2) {
      const ul = el("ul", "legend");
      def.series.forEach((name, i) => {
        const li = el("li");
        const key = el("span", "key");
        key.style.background = `var(--series-${i + 1})`;
        const val = el("span", "val", "–");
        li.append(key, el("span", "name", name), val);
        ul.append(li);
        this.legendVals.push(val);
      });
      fig.append(ul);
    }

    this.plot = el("div", "plot");
    this.canvas = el("canvas");
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute("role", "img");
    this.canvas.setAttribute("aria-label",
      `${def.title} in ${def.unit}. Arrow keys step through values; the Table button lists recent values.`);
    this.tip = el("div", "tooltip");
    this.tip.hidden = true;
    this.plot.append(this.canvas, this.tip);
    this.tableWrap = el("div", "table-wrap");
    this.tableWrap.hidden = true;
    fig.append(this.plot, this.tableWrap);

    this.toggle.addEventListener("click", () => this.setTableView(this.tableWrap.hidden));
    this.canvas.addEventListener("pointermove", (e) => { this.pointerX = e.offsetX; this.kbBack = null; this.dirty = true; });
    this.canvas.addEventListener("pointerleave", () => { this.pointerX = null; this.dirty = true; });
    this.canvas.addEventListener("focus", () => {
      if (this.canvas.matches(":focus-visible")) { this.kbBack = 0; this.dirty = true; }
    });
    this.canvas.addEventListener("blur", () => { this.kbBack = null; this.dirty = true; });
    this.canvas.addEventListener("keydown", (e) => this.onKey(e));
    new ResizeObserver(() => this.resize()).observe(this.plot);
  }

  onKey(e) {
    const step = e.shiftKey ? 10 : 1;
    const max = Math.max(0, this.visibleN - 1);
    const back = this.kbBack ?? 0;
    if (e.key === "ArrowLeft") this.kbBack = Math.min(max, back + step);
    else if (e.key === "ArrowRight") this.kbBack = Math.max(0, back - step);
    else if (e.key === "Home") this.kbBack = max;
    else if (e.key === "End") this.kbBack = 0;
    else if (e.key === "Escape") this.kbBack = null;
    else return;
    e.preventDefault();
    this.pointerX = null;
    this.dirty = true;
  }

  setTableView(on) {
    this.tableWrap.hidden = !on;
    this.plot.hidden = on;
    this.toggle.textContent = on ? "Chart" : "Table";
    this.toggle.setAttribute("aria-pressed", String(on));
    if (on) this.renderTable(); else this.dirty = true;
  }

  push(t, vals) {
    const n = this.t.length;
    if (n && t <= this.t[n - 1]) return;
    this.t.push(t);
    vals.forEach((v, i) => this.ys[i].push(v ?? NaN));
    const cutoff = t - this.def.window * 1.1;
    let k = 0;
    while (k < this.t.length && this.t[k] < cutoff) k++;
    if (k) {
      this.t.splice(0, k);
      this.ys.forEach((a) => a.splice(0, k));
    }
    this.dirty = this.tableDirty = true;
  }

  reset() {
    this.t.length = 0;
    this.ys.forEach((a) => { a.length = 0; });
    this.yr = null;
    this.dirty = this.tableDirty = true;
  }

  resize() {
    const r = this.plot.getBoundingClientRect();
    if (!r.width) return;
    const dpr = window.devicePixelRatio || 1;
    this.w = r.width;
    this.h = r.height;
    this.canvas.width = Math.round(r.width * dpr);
    this.canvas.height = Math.round(r.height * dpr);
    this.ctx = this.canvas.getContext("2d");
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.dirty = true;
  }

  yRange(i0) {
    const { def } = this;
    const n = this.t.length;
    let lo = Infinity, hi = -Infinity;
    for (const ys of this.ys) {
      for (let i = i0; i < n; i++) {
        const v = ys[i];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    if (lo > hi) { lo = 0; hi = 0; }
    // Keep the current scale while the data still fits it comfortably, so noise doesn't make it jump.
    const prev = this.yr;
    if (prev && lo >= prev.lo && hi <= prev.hi && prev.hi - prev.lo <= 2.5 * Math.max(hi - lo, def.minSpan)) return prev;
    if (hi - lo < def.minSpan) {
      const m = (lo + hi) / 2;
      lo = m - def.minSpan / 2;
      hi = m + def.minSpan / 2;
    }
    this.yr = niceTicks(lo, hi, Math.max(3, Math.floor((this.h - 30) / 40)));
    return this.yr;
  }

  draw() {
    this.dirty = false;
    const { ctx, w, h, def } = this;
    if (!ctx || this.plot.hidden) return;
    ctx.clearRect(0, 0, w, h);

    const n = this.t.length;
    const g = { l: 52, r: w - 10, top: 8, b: h - 22 };
    const t1 = n ? this.t[n - 1] : Date.now() / 1000;
    let t0 = t1 - def.window;
    if (def.clock) t0 = Math.min(Math.max(t0, n ? this.t[0] : t0), t1 - 60);
    const i0 = lowerBound(this.t, t0);
    this.visibleN = n - i0;

    const yr = this.yRange(i0);
    const x = (t) => g.l + ((t - t0) / (t1 - t0)) * (g.r - g.l);
    const y = (v) => g.b - ((v - yr.lo) / (yr.hi - yr.lo)) * (g.b - g.top);

    // grid + y ticks
    ctx.font = "11px system-ui, -apple-system, 'Segoe UI', sans-serif";
    ctx.lineWidth = 1;
    ctx.fillStyle = colors["text-muted"];
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const v of yr.values) {
      const isZero = Math.abs(v) < yr.step * 1e-6;
      const yy = Math.round(y(v)) + 0.5;
      ctx.strokeStyle = isZero ? colors.axis : colors.grid;
      ctx.beginPath();
      ctx.moveTo(g.l, yy);
      ctx.lineTo(g.r, yy);
      ctx.stroke();
      ctx.fillText((isZero ? 0 : v).toFixed(yr.decimals).replace("-", "−"), g.l - 8, yy);
    }

    // x ticks
    ctx.textBaseline = "top";
    const xLabels = [];
    if (def.clock) {
      const span = t1 - t0;
      const step = [60, 120, 300, 600, 900, 1800, 3600].find((s) => span / s <= 6) || 3600;
      for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) xLabels.push([t, clockLabel(t, false)]);
    } else {
      for (let s = def.window; s >= 0; s -= 2) xLabels.push([t1 - s, s === 0 ? "now" : `−${s} s`]);
    }
    ctx.textAlign = "center";
    for (const [t, label] of xLabels) {
      const half = ctx.measureText(label).width / 2;
      ctx.fillText(label, Math.min(Math.max(x(t), g.l + half), w - half - 2), g.b + 6);
    }

    // series
    ctx.save();
    ctx.beginPath();
    ctx.rect(g.l, 0, g.r - g.l + 8, h);
    ctx.clip();
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    this.ys.forEach((ys, s) => {
      const color = colors[`series-${s + 1}`];
      const segments = [];
      let seg = null;
      for (let i = Math.max(0, i0 - 1); i < n; i++) {
        const v = ys[i];
        if (!Number.isFinite(v) || (seg && this.t[i] - this.t[i - 1] > def.gap)) seg = null;
        if (!Number.isFinite(v)) continue;
        if (!seg) segments.push((seg = []));
        seg.push([x(this.t[i]), y(v)]);
      }
      for (const pts of segments) {
        if (def.area && pts.length > 1) {
          ctx.globalAlpha = 0.1;
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.moveTo(pts[0][0], g.b);
          for (const [px, py] of pts) ctx.lineTo(px, py);
          ctx.lineTo(pts[pts.length - 1][0], g.b);
          ctx.fill();
          ctx.globalAlpha = 1;
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        pts.forEach(([px, py], k) => (k ? ctx.lineTo(px, py) : ctx.moveTo(px, py)));
        ctx.stroke();
      }
      const last = segments.at(-1)?.at(-1);
      if (last) this.dot(last[0], last[1], color);
    });
    ctx.restore();

    // hover: crosshair snapped to the nearest sample, one tooltip for every series
    const idx = this.hoverIndex(g, t0, t1, i0);
    if (idx == null) {
      this.tip.hidden = true;
    } else {
      const hx = Math.round(x(this.t[idx])) + 0.5;
      ctx.strokeStyle = colors.axis;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(hx, g.top);
      ctx.lineTo(hx, g.b);
      ctx.stroke();
      this.ys.forEach((ys, s) => {
        if (Number.isFinite(ys[idx])) this.dot(hx, y(ys[idx]), colors[`series-${s + 1}`]);
      });
      this.showTip(idx, hx);
    }

    this.legendVals.forEach((elv, s) => { elv.textContent = fmt(this.ys[s][n - 1], def.digits); });
  }

  dot(px, py, color) {
    const { ctx } = this;
    ctx.fillStyle = colors["surface-1"];
    ctx.beginPath();
    ctx.arc(px, py, 6, 0, 2 * Math.PI);
    ctx.fill();
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(px, py, 4, 0, 2 * Math.PI);
    ctx.fill();
  }

  hoverIndex(g, t0, t1, i0) {
    const n = this.t.length;
    if (n - i0 <= 0) return null;
    if (this.pointerX != null) {
      if (this.pointerX < g.l - 12 || this.pointerX > g.r + 12) return null;
      const t = t0 + ((this.pointerX - g.l) / (g.r - g.l)) * (t1 - t0);
      return Math.max(i0, nearest(this.t, t));
    }
    if (this.kbBack != null) return Math.max(i0, n - 1 - this.kbBack);
    return null;
  }

  timeLabel(t) {
    if (this.def.clock) return clockLabel(t, true);
    const ago = this.t[this.t.length - 1] - t;
    return ago < 0.005 ? "Newest sample" : `${ago.toFixed(2)} s ago`;
  }

  showTip(idx, px) {
    const { def, tip } = this;
    tip.replaceChildren(el("div", "tt-time", this.timeLabel(this.t[idx])));
    def.series.forEach((name, s) => {
      const row = el("div", "tt-row");
      const key = el("span", "key");
      key.style.background = `var(--series-${s + 1})`;
      row.append(key, el("strong", null, `${fmt(this.ys[s][idx], def.digits)} ${def.unit}`), el("span", null, name));
      tip.append(row);
    });
    tip.hidden = false;
    const tw = tip.offsetWidth;
    let left = px + 12;
    if (left + tw > this.w) left = px - 12 - tw;
    tip.style.left = `${Math.max(0, left)}px`;
  }

  renderTable() {
    const { def } = this;
    this.tableDirty = false;
    this.tableAt = performance.now();
    const table = el("table");
    const thead = el("thead");
    const hr = el("tr");
    [def.clock ? "Time" : "Age", ...def.series.map((s) => `${s} (${def.unit})`)].forEach((h) => hr.append(el("th", null, h)));
    thead.append(hr);
    const tbody = el("tbody");
    const n = this.t.length;
    for (let i = n - 1; i >= Math.max(0, n - 20); i--) {
      const tr = el("tr");
      tr.append(el("td", null, def.clock ? clockLabel(this.t[i], true) : `${(this.t[n - 1] - this.t[i]).toFixed(2)} s`));
      this.ys.forEach((ys) => tr.append(el("td", null, fmt(ys[i], def.digits))));
      tbody.append(tr);
    }
    table.append(thead, tbody);
    this.tableWrap.replaceChildren(table);
  }
}

// ---------- page

const charts = {};
document.querySelectorAll("figure[data-chart]").forEach((fig) => {
  charts[fig.dataset.chart] = new Chart(fig, CHARTS[fig.dataset.chart]);
});

const conn = document.getElementById("conn");
const pauseBtn = document.getElementById("pause");
const themeBtn = document.getElementById("theme");
let paused = false;
let lastMsgAt = 0;
let msgCount = 0;
let lastBattery = null;   // [t, voltage, soc] of the newest gauge reading

function setConn(state, text) {
  if (conn.dataset.state === state) return;
  conn.dataset.state = state;
  conn.querySelector(".status-text").textContent = text;
  document.querySelectorAll(".plot").forEach((p) => p.classList.toggle("stale", state !== "ok"));
}

function updateStatus(status) {
  for (const [name, s] of Object.entries(status)) {
    const box = document.querySelector(`.dev-status[data-dev="${name}"]`);
    const key = JSON.stringify(s);
    if (!box || box.dataset.key === key) continue;
    box.dataset.key = key;
    box.dataset.state = s.ok ? "ok" : s.error ? "error" : "wait";
    const icon = el("span", "status-icon");
    icon.setAttribute("aria-hidden", "true");
    const detail = s.ok ? `${s.ident} · ${s.bus}` : s.error ? `${s.bus} · ${s.error}` : s.bus;
    box.replaceChildren(icon, el("span", "status-text", s.ok ? "Connected" : s.error ? "Not responding" : "Starting"), el("span", "detail", detail));
  }
}

function duration(hours) {
  return hours < 1 ? `${Math.round(hours * 60)} min` : `${hours.toFixed(1)} h`;
}

function updateBattery(b) {
  const meter = document.getElementById("soc-meter");
  if (!b) {
    ["soc", "bat-v", "bat-rate", "bat-dir", "bat-alert", "bat-thr", "bat-flags"].forEach((id) => setText(id, null));
    meter.firstElementChild.style.width = "0";
    meter.removeAttribute("aria-valuenow");
    return;
  }
  setText("soc", `${fmt(b.soc, 1)} %`);
  const soc = Math.min(100, Math.max(0, b.soc));
  meter.firstElementChild.style.width = `${soc}%`;
  meter.setAttribute("aria-valuenow", soc.toFixed(1));
  meter.dataset.level = soc <= 10 ? "critical" : soc <= 20 ? "warning" : "";
  setText("bat-v", `${fmt(b.voltage, 3)} V`);
  setText("bat-rate", `${b.crate > 0 ? "+" : ""}${fmt(b.crate, 1)} %/h`);
  let dir = "Idle";
  if (b.crate > 0.5) dir = `Charging · about ${duration((100 - b.soc) / b.crate)} to full`;
  else if (b.crate < -0.5) dir = `Discharging · about ${duration(b.soc / -b.crate)} left`;
  setText("bat-dir", dir);
  setText("bat-alert", b.alert ? "Active" : "Clear");
  setText("bat-thr", `Triggers below ${b.alert_threshold} %`);
  setText("bat-flags", b.flags.length ? b.flags.map((f) => FLAG_LABELS[f] || f).join(", ") : "None");
}

// ---------- fan & LED outputs

const outputRows = {};   // name -> row state
let boardSettings = null;   // board.py settings (fan normal speed etc.), from /api/board
const SEND_EVERY_MS = 150;   // slider updates are throttled to this

async function postOutput(name, level) {
  const res = await fetch("/api/outputs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, level }),
  });
  if (!res.ok) throw new Error((await res.text()).trim() || `HTTP ${res.status}`);
  return res.json();
}

function renderOutput(r) {
  const { item } = r;
  const on = item.level > 0;
  if (r.sw) {
    r.sw.setAttribute("aria-checked", String(on));
    r.sw.disabled = item.error != null;
  }
  if (r.slider) {
    r.slider.disabled = item.error != null;
    // The fan's slider is its normal (automatic) speed, not its current level.
    const value = item.auto ? boardSettings && boardSettings.fan_normal_pct : item.level;
    if (!r.dragging && value != null) r.slider.value = value;
  }
  r.value.textContent = item.dimmable ? `${item.level} %` : on ? "On" : "Off";
  if (r.note) r.note.textContent = item.note || "";
  r.error.textContent = r.sendError || item.error || "";
}

async function sendOutput(r, level) {
  r.inflight++;
  try {
    applyOutputs(await postOutput(r.item.name, level), r);
    r.sendError = null;
  } catch (e) {
    r.sendError = `Not applied: ${e.message}`;
  } finally {
    r.inflight--;
    renderOutput(r);
  }
}

function setOutput(r, level, throttled) {
  r.item.level = level;
  if (level > 0) r.lastOn = level;
  renderOutput(r);
  if (!throttled) {
    clearTimeout(r.timer);
    r.timer = null;
    r.pending = null;
    sendOutput(r, level);
  } else if (r.timer) {
    r.pending = level;
  } else {
    sendOutput(r, level);
    r.timer = setTimeout(function flush() {
      r.timer = null;
      if (r.pending != null) {
        const next = r.pending;
        r.pending = null;
        sendOutput(r, next);
        r.timer = setTimeout(flush, SEND_EVERY_MS);
      }
    }, SEND_EVERY_MS);
  }
}

function buildOutputs(items) {
  const box = document.getElementById("outputs");
  box.replaceChildren();
  for (const key of Object.keys(outputRows)) delete outputRows[key];
  for (const item of items) {
    const r = { item: { ...item }, lastOn: item.level || 100, dragging: false, inflight: 0, timer: null, pending: null, sendError: null };
    const text = el("div", "output-text");
    text.append(el("div", "output-label", item.label), el("div", "output-detail", item.detail));
    const controls = el("div", "output-controls");
    if (item.auto) {
      const badge = el("span", "badge", item.kernel ? "Kernel" : "Automatic");
      badge.dataset.state = "ok";
      controls.append(badge);
      if (item.name === "fan" && item.dimmable) {
        r.slider = el("input", "level");
        Object.assign(r.slider, { type: "range", min: 0, max: 100, step: 5 });
        r.slider.setAttribute("aria-label", "Fan normal speed");
        r.slider.title = "Normal speed (full speed when the CPU is hot)";
        r.slider.addEventListener("pointerdown", () => { r.dragging = true; });
        for (const ev of ["pointerup", "pointercancel"]) r.slider.addEventListener(ev, () => { r.dragging = false; });
        r.slider.addEventListener("change", async () => {
          r.dragging = false;
          try {
            applyBoard(await postJson("/api/board", { fan_normal_pct: Number(r.slider.value) }));
            r.sendError = null;
          } catch (e) {
            r.sendError = `Not applied: ${e.message}`;
          }
          renderOutput(r);
        });
        controls.append(r.slider);
      }
    } else {
      r.sw = el("button", "switch");
      r.sw.type = "button";
      r.sw.setAttribute("role", "switch");
      r.sw.setAttribute("aria-label", item.label);
      r.sw.addEventListener("click", () => setOutput(r, r.item.level > 0 ? 0 : r.item.dimmable ? r.lastOn : 100));
      controls.append(r.sw);
    }
    if (item.dimmable && !item.auto) {
      r.slider = el("input", "level");
      Object.assign(r.slider, { type: "range", min: 0, max: 100, step: 5 });
      r.slider.setAttribute("aria-label", `${item.label} level`);
      r.slider.addEventListener("pointerdown", () => { r.dragging = true; });
      for (const ev of ["pointerup", "pointercancel"]) r.slider.addEventListener(ev, () => { r.dragging = false; });
      r.slider.addEventListener("input", () => setOutput(r, Number(r.slider.value), true));
      r.slider.addEventListener("change", () => { r.dragging = false; setOutput(r, Number(r.slider.value)); });
      controls.append(r.slider);
    }
    r.value = el("span", "output-value");
    controls.append(r.value);
    r.note = el("div", "output-note");
    r.error = el("div", "output-error");
    const row = el("div", "output");
    row.append(text, controls, r.note, r.error);
    box.append(row);
    outputRows[item.name] = r;
    renderOutput(r);
  }
  const waiting = items.filter((i) => i.pwm_capable && !i.dimmable).map((i) => i.label);
  document.getElementById("outputs-note").textContent = waiting.length
    ? `${waiting.join(" and ")}: on/off until the next reboot, then hardware-PWM speed/brightness.`
    : "";
}

// origin: the row whose POST reply this is (null for state pushed over the stream)
function applyOutputs(items, origin) {
  const shape = items.map((i) => `${i.name}:${i.dimmable}:${i.auto}:${i.kernel}`).join();
  if (shape !== applyOutputs.shape) {
    applyOutputs.shape = shape;
    buildOutputs(items);
    return;
  }
  for (const item of items) {
    const r = outputRows[item.name];
    // While the user is dragging or one of the row's own requests is still out,
    // incoming state is older than what the screen already shows.
    if (!r || r.dragging || r.pending != null || r.inflight > (r === origin ? 1 : 0)) continue;
    r.item = { ...item };
    renderOutput(r);
  }
}

// ---------- camera

const camImg = document.getElementById("cam-img");
const camToggle = document.getElementById("cam-toggle");
const camPlaceholder = document.getElementById("cam-placeholder");
const BLANK_GIF = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
let camWanted = false;   // the user asked for the live view
let camAttachedAt = 0;   // when the current stream was opened (0 = not attached)

function camAttach(on) {
  // Replacing src is what actually closes an MJPEG connection; the server then stops
  // the camera a few seconds after its last viewer leaves.
  camImg.src = on ? `/api/camera.mjpg?t=${Date.now()}` : BLANK_GIF;
  camAttachedAt = on ? performance.now() : 0;
  camImg.hidden = !on;
  camPlaceholder.hidden = on;
}

function camSetWanted(on, note) {
  camWanted = on;
  camToggle.textContent = on ? "Stop live view" : "Start live view";
  camToggle.setAttribute("aria-pressed", String(on));
  camPlaceholder.textContent = note || "Live view is off";
  camAttach(on && !document.hidden);
}

camToggle.addEventListener("click", () => camSetWanted(!camWanted));
// Background tabs don't need frames; dropping the stream also lets the camera stop.
document.addEventListener("visibilitychange", () => { if (camWanted) camAttach(!document.hidden); });
camImg.addEventListener("error", () => { if (camAttachedAt) camSetWanted(false, "Live view stopped: the stream closed"); });

// Native AR0234 readouts (2-lane CSI); 720p and 1080p are centre crops, 960x600 is 2x2 binned.
// max = highest frame rate the Zero 2W records without dropping frames (camera_worker.RECORD_MAX_FPS).
const CAM_MODES = [
  { w: 1920, h: 1200, max: 25, label: "1920×1200 · full sensor (MJPEG video)" },
  { w: 1920, h: 1080, max: 30, label: "1920×1080 · 1080p crop" },
  { w: 1280, h: 720, max: 60, label: "1280×720 · 720p crop" },
  { w: 960, h: 600, max: 100, label: "960×600 · binned, full view" },
];
const CAM_FPS = [5, 10, 15, 24, 25, 30, 50, 60, 90, 100];
const camRes = document.getElementById("cam-res");
const camFpsSelect = document.getElementById("cam-fps-select");
const camApply = document.getElementById("cam-apply");
const camPhoto = document.getElementById("cam-photo");
const camRecord = document.getElementById("cam-record");
const camRaw = document.getElementById("cam-raw");
let camStatus = null;
let camBusy = false;          // a camera command is in flight
let camEditing = false;       // the user changed the mode selectors but has not applied them
let recStartedAt = 0;         // performance.now() at the (estimated) recording start

for (const [i, m] of CAM_MODES.entries()) camRes.append(new Option(m.label, i));

function fillFps(selected) {
  const mode = CAM_MODES[Number(camRes.value)];
  camFpsSelect.replaceChildren();
  for (const f of CAM_FPS.filter((f) => f <= mode.max)) camFpsSelect.append(new Option(`${f} fps`, f));
  const options = [...camFpsSelect.options].map((o) => Number(o.value));
  const best = options.reduce((a, b) => (Math.abs(b - selected) < Math.abs(a - selected) ? b : a), options[0]);
  camFpsSelect.value = best;
}
camRes.addEventListener("change", () => { camEditing = true; fillFps(Number(camFpsSelect.value) || 30); });
camFpsSelect.addEventListener("change", () => { camEditing = true; });

function camMessage(text, isError) {
  const el = document.getElementById("cam-msg");
  el.textContent = text || "";
  el.classList.toggle("error", Boolean(isError));
}

async function camCommand(body, busyText) {
  camBusy = true;
  renderCameraControls();
  if (busyText) camMessage(busyText);
  try {
    const res = await fetch("/api/camera", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.text()).trim() || `HTTP ${res.status}`);
    const reply = await res.json();
    updateCamera(reply.status);
    return reply.result;
  } catch (e) {
    camMessage(e.message, true);
    return null;
  } finally {
    camBusy = false;
    renderCameraControls();
  }
}

camPhoto.addEventListener("click", async () => {
  const r = await camCommand({ action: "still" }, "Taking photo…");
  if (r) {
    camMessage(`Saved ${r.files.join(" + ")} (${r.size[0]}×${r.size[1]})`);
    loadMedia();
  }
});

camRecord.addEventListener("click", async () => {
  if (camStatus && camStatus.recording) {
    const r = await camCommand({ action: "record_stop" }, "Finishing recording…");
    if (r) {
      camMessage(`Saved ${r.file}: ${duration(r.seconds / 3600)}, ${formatBytes(r.bytes)}`);
      loadMedia();
    }
  } else {
    const r = await camCommand({ action: "record_start" }, "Starting recording…");
    if (r) {
      recStartedAt = performance.now();
      camMessage(`Recording ${r.file} at ${fmt(r.bitrate / 1e6, 0)} Mb/s`);
    }
  }
});

camApply.addEventListener("click", async () => {
  const mode = CAM_MODES[Number(camRes.value)];
  const r = await camCommand({ action: "configure", width: mode.w, height: mode.h, fps: Number(camFpsSelect.value) },
    `Switching to ${mode.w}×${mode.h} at ${camFpsSelect.value} fps…`);
  if (r) {
    camEditing = false;
    camMessage(`Camera set to ${r.width}×${r.height} at ${r.fps} fps (${r.sensor_mode} readout)`);
  }
});

camRaw.addEventListener("change", () => camCommand({ action: "settings", raw: camRaw.checked }));

function renderCameraControls() {
  const c = camStatus;
  const present = Boolean(c && c.present);
  const recording = Boolean(c && c.recording);
  camToggle.disabled = !present;
  camPhoto.disabled = !present || camBusy;
  camRecord.disabled = !present || camBusy;
  camRecord.textContent = recording ? "Stop recording" : "Record";
  camRecord.setAttribute("aria-pressed", String(recording));
  camApply.disabled = !present || camBusy || recording;
  camRes.disabled = camFpsSelect.disabled = recording;
  document.getElementById("rec-badge").hidden = !recording;
}

// ---------- image quality (image_tuning.py)

const IMG_EXPOSURES = [[3, "3 ms: sharpest, noisiest, may flicker"], [4, "4 ms: may flicker"], [6, "6 ms: may flicker"],
  [8, "8 ms: may flicker"], [10, "10 ms: recommended, no flicker"], [16, "16 ms"], [20, "20 ms (flicker-free, blurrier)"],
  [33, "33 ms: brightest, blurriest"]];
const IMG_MAINS = [[50, "50 Hz (India, Europe)"], [60, "60 Hz (Americas)"], [0, "Off"]];
const IMG_SHARPNESS = [[0, "Off"], [0.5, "Low"], [1, "Normal"], [1.5, "High"], [2, "Very high"]];
const IMG_METERING = [["matrix", "the whole view"], ["centre-weighted", "the centre"]];
let imgBusy = false;
let evDragging = false, evTimer = null, evPending = null;

function fmtEv(v) {
  return `${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v).toFixed(1)}`;
}

function sendEv(v) {   // live: the camera does not restart
  if (evTimer) { evPending = v; return; }
  postJson("/api/camera", { action: "brightness", ev: v }).catch((e) => {
    const m = document.getElementById("img-msg");
    m.textContent = e.message;
    m.classList.add("error");
  });
  evTimer = setTimeout(() => {
    evTimer = null;
    if (evPending != null) { const next = evPending; evPending = null; sendEv(next); }
  }, 150);
}

function fillSelect(select, options) {
  if (select.options.length) return;
  for (const [value, label] of options) {
    const o = el("option", null, label);
    o.value = String(value);
    select.append(o);
  }
}

function setSelectNear(select, value) {   // the saved value may lie between two listed ones
  const near = [...select.options].reduce((best, o) =>
    (Math.abs(Number(o.value) - value) < Math.abs(Number(best.value) - value) ? o : best), select.options[0]);
  select.value = Number.isNaN(Number(near.value)) ? String(value) : near.value;
}

function renderImage(c) {
  if (!c.image || imgBusy) return;
  const exp = document.getElementById("img-exposure"), sharp = document.getElementById("img-sharpness"),
    meter = document.getElementById("img-metering"), ce = document.getElementById("img-ce"),
    mains = document.getElementById("img-mains");
  fillSelect(exp, IMG_EXPOSURES);
  fillSelect(mains, IMG_MAINS);
  if (document.activeElement !== mains) mains.value = String(c.image.mains_hz);
  fillSelect(sharp, IMG_SHARPNESS);
  fillSelect(meter, IMG_METERING);
  if (document.activeElement !== exp) setSelectNear(exp, c.image.max_exposure_ms);
  if (document.activeElement !== sharp) setSelectNear(sharp, c.image.sharpness);
  if (document.activeElement !== meter) meter.value = c.image.metering;
  if (document.activeElement !== ce) ce.checked = c.image.contrast_enhance;
  if (!evDragging && !evTimer) {
    document.getElementById("img-ev").value = String(c.image.ev);
    document.getElementById("img-ev-value").textContent = fmtEv(c.image.ev);
  }
  const cam = c.camera && c.camera.config;
  document.getElementById("img-help").textContent =
    "A shorter exposure means less motion blur when the head moves; the camera makes up the light with gain, " +
    "which adds a little noise (denoising is always on). The frame rate is not affected" +
    (cam ? `: ${cam.width}×${cam.height} at ${cam.fps} fps.` : ".");
}

async function imageAction(body) {
  const msg = document.getElementById("img-msg");
  imgBusy = true;
  msg.classList.remove("error");
  msg.textContent = "Applying…";
  try {
    const reply = await postJson("/api/camera", body);
    imgBusy = false;
    renderImage(reply.status);
    msg.textContent = "Applied. The camera restarted with the new settings.";
  } catch (e) {
    msg.textContent = e.message;
    msg.classList.add("error");
  } finally {
    imgBusy = false;
  }
}

for (const [id, key, parse] of [["img-exposure", "max_exposure_ms", Number], ["img-sharpness", "sharpness", Number],
  ["img-metering", "metering", String], ["img-mains", "mains_hz", Number]]) {
  document.getElementById(id).addEventListener("change", (ev) => imageAction({ action: "image", [key]: parse(ev.target.value) }));
}
const evSlider = document.getElementById("img-ev");
evSlider.addEventListener("pointerdown", () => { evDragging = true; });
for (const ev of ["pointerup", "pointercancel", "blur"]) evSlider.addEventListener(ev, () => { evDragging = false; });
evSlider.addEventListener("input", (e) => {
  const v = Number(e.target.value);
  document.getElementById("img-ev-value").textContent = fmtEv(v);
  sendEv(v);
});
evSlider.addEventListener("dblclick", () => {
  evSlider.value = "0";
  document.getElementById("img-ev-value").textContent = fmtEv(0);
  sendEv(0);
});
document.getElementById("img-ce").addEventListener("change", (ev) => imageAction({ action: "image", contrast_enhance: ev.target.checked }));
document.getElementById("img-reset").addEventListener("click", () => imageAction({ action: "image_reset" }));

// ---------- lens shading correction (lens_shading.py)

let lensBusy = false;

function lensSummary(st) {
  return `edges were up to ${fmt(st.red_shift_pct, 0)} % off in red and ${fmt(st.blue_shift_pct, 0)} % in blue, ` +
    `corners at ${fmt(st.corner_brightness_pct, 0)} % of the centre's brightness` +
    (st.dark_area_pct != null ? `; ${fmt(st.dark_area_pct, 0)} % of the picture is still darker than can be lifted` : "");
}

function renderLens(lens) {
  if (!lens || lensBusy) return;
  const when = lens.time ? new Date(lens.time.replace(/([+-]\d\d)(\d\d)$/, "$1:$2")) : null;
  document.getElementById("lens-status").textContent = lens.calibrated
    ? `Calibrated${when && !isNaN(when) ? ` ${when.toLocaleString()}` : ""}: ${lensSummary(lens.stats)}, now corrected.`
    : "Not calibrated: the edges of the picture are darker and tinted.";
  document.getElementById("lens-reset").hidden = !lens.calibrated;
  document.getElementById("lens-calibrate").textContent = lens.calibrated ? "Calibrate again" : "Calibrate";
}

async function lensAction(action) {
  const msg = document.getElementById("lens-msg");
  const buttons = [document.getElementById("lens-calibrate"), document.getElementById("lens-reset")];
  lensBusy = true;
  buttons.forEach((b) => { b.disabled = true; });
  msg.classList.remove("error");
  msg.textContent = action === "lens_calibrate" ? "Measuring… keep the diffuser over the lens." : "Removing…";
  try {
    const reply = await postJson("/api/camera", { action });
    const r = reply.result;
    if (r.problems) {
      msg.textContent = `Not saved: ${r.problems.join(" ")}`;
      msg.classList.add("error");
    } else if (r.saved) {
      msg.textContent = `Saved: ${lensSummary(r.stats)}. ${(r.warnings || []).join(" ")}`;
    } else {
      msg.textContent = "Removed: the camera is back to no lens shading correction.";
    }
    lensBusy = false;
    renderLens(reply.status.lens);
  } catch (e) {
    msg.textContent = e.message;
    msg.classList.add("error");
  } finally {
    lensBusy = false;
    buttons.forEach((b) => { b.disabled = false; });
  }
}

document.getElementById("lens-calibrate").addEventListener("click", () => lensAction("lens_calibrate"));
document.getElementById("lens-reset").addEventListener("click", (ev) => {
  if (secondTap(ev.currentTarget, "Tap again to remove")) lensAction("lens_reset");
});

function updateCamera(c) {
  camStatus = c;
  renderLens(c.lens);
  renderImage(c);
  const box = document.getElementById("cam-status");
  const state = !c.present || c.state === "error" ? "error" : c.state === "live" ? "ok" : "wait";
  if (box.dataset.state !== state) box.dataset.state = state;
  box.querySelector(".status-text").textContent = !c.present ? "Driver not loaded"
    : c.recording ? "Recording" : { off: "Idle", starting: "Starting", live: "Running", error: "Error" }[c.state];
  box.querySelector(".detail").textContent = c.error
    || (!c.present ? "Camera chip not detected: restart the Pi once after installing, and check the ribbon cable"
      : c.state === "live" ? `${c.viewers} in use` : "CSI-2 2-lane · I²C-10 @ 0x10");

  const s = c.settings;
  if (!camEditing && s) {
    const i = CAM_MODES.findIndex((m) => m.w === s.width && m.h === s.height);
    camRes.value = i >= 0 ? i : 0;
    fillFps(s.fps);
  }
  if (s) camRaw.checked = Boolean(s.raw);

  const m = c.camera;
  const cfg = m && m.config;
  setText("cam-mode", cfg && `${cfg.width}×${cfg.height} · ${cfg.sensor_mode} · records ${cfg.record_codec.toUpperCase()}`);
  setText("cam-fps", m && m.fps != null ? `${fmt(m.fps, 1)} fps` : null);
  setText("cam-exp", m && m.exposure_ms != null ? `${fmt(m.exposure_ms, 2)} ms` : null);
  setText("cam-gain", m && `${fmt(m.analog_gain, 2)}× analogue · ${fmt(m.digital_gain, 2)}× digital`);
  setText("cam-wb", m && m.colour_temp ? `${m.colour_temp} K` : null);
  if (c.recording) {
    recStartedAt = performance.now() - c.recording.seconds * 1000;   // resync to the camera's clock
  }
  renderCameraControls();
  // A status from before this stream opened may still report an old error; give it time.
  if (camAttachedAt && c.state === "error" && performance.now() - camAttachedAt > 3000) {
    camSetWanted(false, `Live view stopped: ${c.error || "camera error"}`);
  }
}

setInterval(() => {
  if (!camStatus || !camStatus.recording) return;
  const s = Math.floor((performance.now() - recStartedAt) / 1000);
  document.getElementById("rec-time").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}, 250);

// ---------- shutdown

// True on the confirming second tap within 4 s; the first tap shows the prompt on the button.
// (Instead of confirm(), which app WebViews block.)
function secondTap(button, prompt) {
  const now = Date.now();
  if (now - (button.armedAt || 0) <= 4000) {
    button.armedAt = 0;
    return true;
  }
  button.armedAt = now;
  button.dataset.label = button.dataset.label || button.textContent;
  button.textContent = prompt;
  setTimeout(() => {
    if (Date.now() - (button.armedAt || 0) >= 4000) button.textContent = button.dataset.label;
  }, 4100);
  return false;
}

async function postJson(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error((await res.text()).trim() || `HTTP ${res.status}`);
  return res.json();
}

function savedText(saved) {
  if (!saved) return "";
  if (saved.session) return `Recording ${saved.session.id} was saved.`;
  if (saved.file) return `The video was saved as ${saved.file}.`;
  return saved.error ? `Saving the recording failed: ${saved.error}.` : "";
}

async function systemAction(action, button) {
  const label = button.dataset.label || button.textContent;
  const verb = action === "reboot" ? "restart" : "shut down";
  button.disabled = true;
  button.textContent = action === "reboot" ? "Restarting…" : "Shutting down…";
  try {
    const reply = await postJson("/api/system", { action });
    document.getElementById("shutdown-title").textContent =
      action === "reboot" ? "The Pi is restarting." : "The Pi is shutting down.";
    document.getElementById("shutdown-detail").textContent = savedText(reply.saved_recording);
    document.getElementById("shutdown-notice").hidden = false;
    camSetWanted(false, action === "reboot" ? "The Pi is restarting" : "The Pi is shutting down");
  } catch (e) {
    button.disabled = false;
    button.textContent = label;
    camMessage(`${verb} failed: ${e.message}`, true);
  }
}

function systemButton(button, action) {
  button.addEventListener("click", () => {
    const recording = (camStatus && camStatus.recording) || lastSession;
    const verb = action === "reboot" ? "restart" : "shut down";
    if (secondTap(button, recording ? `Tap again: save the recording and ${verb}` : `Tap again to ${verb}`)) {
      systemAction(action, button);
    }
  });
}
let lastSession = null;   // the running recording session, from the stream
systemButton(document.getElementById("shutdown"), "shutdown");
systemButton(document.getElementById("reboot"), "reboot");

// ---------- power & buttons (board.py)

let boardSelectBusy = false;

function fmtMinSec(s) {
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function setSelect(select, value) {
  const v = String(value);
  if (![...select.options].some((o) => o.value === v)) select.append(el("option", null, v));
  select.value = v;
}

function applyBoard(b) {
  if (!b) return;
  const first = !boardSettings;
  boardSettings = b.settings;
  let state = "wait", text = "On battery", detail = "";
  if (b.shutting_down) {
    state = "error"; text = "Shutting down"; detail = `Reason: ${b.shutting_down}.`;
  } else if (!b.inputs_ok) {
    state = "error"; text = "Inputs unavailable"; detail = b.input_error || "";
  } else if (b.external_power) {
    state = "ok"; text = "USB-C power"; detail = "External power is connected.";
  } else if (b.on_battery_s == null) {
    text = "Power removed"; detail = "Checking…";
  } else if (b.shutdown_in_s == null) {
    detail = `Running on battery for ${fmtMinSec(b.on_battery_s)}. Automatic shutdown is off.`;
  } else {
    detail = `Running on battery for ${fmtMinSec(b.on_battery_s)}. Unless power comes back, recordings are saved and the Pi shuts down in ${fmtMinSec(b.shutdown_in_s)}.`;
  }
  const st = document.getElementById("power-status");
  st.dataset.state = state;
  st.querySelector(".status-text").textContent = text;
  setText("power-detail", detail);
  if (!boardSelectBusy) {
    setSelect(document.getElementById("unplug-select"), b.settings.unplug_shutdown_s);
    setSelect(document.getElementById("battery-low-select"), b.settings.battery_low_pct);
  }
  setText("power-buttons", `Record button (GPIO4) pressed ${b.presses.record}×, power button (GPIO22) ${b.presses.power}× since the app started` +
    (b.cpu_temp_c != null ? ` · CPU ${fmt(b.cpu_temp_c, 1)} °C` : ""));
  if (first && outputRows.fan) renderOutput(outputRows.fan);
}

for (const [id, key] of [["unplug-select", "unplug_shutdown_s"], ["battery-low-select", "battery_low_pct"]]) {
  const select = document.getElementById(id);
  select.addEventListener("focus", () => { boardSelectBusy = true; });
  select.addEventListener("blur", () => { boardSelectBusy = false; });
  select.addEventListener("change", async () => {
    const msg = document.getElementById("power-msg");
    try {
      applyBoard(await postJson("/api/board", { [key]: Number(select.value) }));
      msg.textContent = "Saved.";
      msg.classList.remove("error");
    } catch (e) {
      msg.textContent = `Not saved: ${e.message}`;
      msg.classList.add("error");
    }
  });
}
fetch("/api/board").then((r) => r.json()).then(applyBoard, () => {});

// ---------- software update (system/ota.py, run by systemd as root)

const UPDATE_BUSY = new Set(["checking", "downloading", "verifying", "installing"]);
let updateTimer = null;

function renderUpdate(u) {
  const busy = UPDATE_BUSY.has(u.status);
  const states = {
    "up-to-date": ["ok", "Up to date"], available: ["wait", "Update available"], installed: ["ok", "Updated"],
    "rolled-back": ["error", "Update failed, rolled back"], failed: ["error", "Check failed"],
    "not-configured": ["wait", "No update source"], "never-checked": ["wait", "Not checked yet"],
  };
  const [state, text] = busy ? ["wait", `${u.status[0].toUpperCase()}${u.status.slice(1)}…`] : (states[u.status] || ["wait", u.status]);
  const st = document.getElementById("update-status");
  st.dataset.state = state;
  st.querySelector(".status-text").textContent = text;
  const checked = u.checked_at ? ` · checked ${new Date(u.checked_at * 1000).toLocaleString()}` : "";
  setText("update-summary", `Installed: ${u.version}` + (u.repo ? ` · source: github.com/${u.repo}` : "") +
    (u.repo ? ` · automatic install ${u.auto_install ? "on" : "off"}` : "") + checked);
  const m = document.getElementById("update-message");
  m.textContent = u.message || "";
  m.classList.toggle("error", state === "error");
  const notes = document.getElementById("update-notes");
  notes.hidden = !(u.latest && u.latest.notes);
  if (u.latest) {
    setText("update-notes-title", `Release notes: ${u.latest.name || u.latest.tag}` +
      (u.latest.published ? ` (${new Date(u.latest.published).toLocaleDateString()})` : ""));
    setText("update-notes-text", u.latest.notes || "");
  }
  const install = document.getElementById("update-install");
  install.hidden = !(u.status === "available" && u.latest);
  if (!install.hidden && Date.now() - (install.armedAt || 0) > 4000) install.textContent = `Install ${u.latest.tag}`;
  install.dataset.label = install.hidden ? "" : `Install ${u.latest.tag}`;
  install.disabled = busy;
  document.getElementById("update-check").disabled = busy || u.status === "not-configured";
  document.getElementById("update-reboot").hidden = !u.reboot_required;
  const logBox = document.getElementById("update-log");
  logBox.hidden = !(u.log_tail && u.log_tail.length);
  logBox.textContent = (u.log_tail || []).join("\n");
  clearTimeout(updateTimer);
  updateTimer = setTimeout(loadUpdate, busy ? 2000 : 30000);
}

async function loadUpdate() {
  try {
    renderUpdate(await (await fetch("/api/update", { cache: "no-store" })).json());
  } catch (e) {
    clearTimeout(updateTimer);
    updateTimer = setTimeout(loadUpdate, 5000);   // the app restarts while an update installs
  }
}

async function updateAction(action) {
  try {
    renderUpdate(await postJson("/api/update", { action }));
    setTimeout(loadUpdate, 1500);
  } catch (e) {
    const m = document.getElementById("update-message");
    m.textContent = e.message;
    m.classList.add("error");
  }
}

document.getElementById("update-check").addEventListener("click", () => updateAction("check"));
document.getElementById("update-install").addEventListener("click", (ev) => {
  if (secondTap(ev.currentTarget, "Tap again: the app restarts while it installs")) updateAction("install");
});
document.getElementById("update-reboot").addEventListener("click", (ev) => {
  if (secondTap(ev.currentTarget, "Tap again to restart")) systemAction("reboot", ev.currentTarget);
});
loadUpdate();

// ---------- captures

function formatBytes(n) {
  if (n >= 1e9) return `${fmt(n / 1e9, 2)} GB`;
  if (n >= 1e6) return `${fmt(n / 1e6, 1)} MB`;
  return `${fmt(n / 1e3, 0)} KB`;
}

async function loadMedia() {
  let data;
  try {
    data = await (await fetch("/api/media")).json();
  } catch (e) {
    return;
  }
  setText("media-free", `${formatBytes(data.free_bytes)} free on the SD card`);
  const rows = document.getElementById("media-rows");
  rows.replaceChildren();
  if (!data.files.length) {
    const tr = el("tr");
    const td = el("td", "empty", "No photos or recordings yet");
    td.colSpan = 4;
    tr.append(td);
    rows.append(tr);
  }
  for (const f of data.files) {
    const tr = el("tr");
    const link = el("a", null, f.name);
    link.href = `/media/${encodeURIComponent(f.name)}`;
    link.target = "_blank";
    link.rel = "noopener";
    const name = el("td");
    name.append(link);
    const del = el("button", "small", "Delete");
    del.type = "button";
    del.addEventListener("click", async () => {
      if (!secondTap(del, "Tap again")) return;
      const res = await fetch("/api/media/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: f.name }),
      });
      if (!res.ok) camMessage((await res.text()).trim(), true);
      loadMedia();
    });
    const actions = el("td");
    actions.append(del);
    tr.append(name, el("td", null, formatBytes(f.bytes)),
      el("td", null, new Date(f.mtime * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "medium" })), actions);
    rows.append(tr);
  }
}
loadMedia();

function onMessage(msg) {
  const { imu, mag, battery } = msg;
  updateStatus(msg.status);
  lastSession = msg.session || null;
  if (msg.board) applyBoard(msg.board);
  if (msg.outputs) applyOutputs(msg.outputs, null);
  if (msg.camera) updateCamera(msg.camera);

  setText("imu-norm", imu && `${fmt(imu.accel_norm, 3)} g`);
  setText("imu-roll", imu && `${fmt(imu.roll, 1)}°`);
  setText("imu-pitch", imu && `${fmt(imu.pitch, 1)}°`);
  setText("imu-temp", imu && `${fmt(imu.temp, 1)} °C`);
  setText("mag-norm", mag && `${fmt(mag.field_norm, 2)} µT`);
  setText("mag-temp", mag && mag.temp != null ? `${fmt(mag.temp, 1)} °C` : null);
  setText("mag-offset", mag && AXES.map((a, i) => `${a} ${fmt(mag.offset[i], 1)}`).join("  ·  ") + " µT");
  updateBattery(battery);

  if (battery && msg.battery_t != null) lastBattery = [msg.battery_t, battery.voltage, battery.soc];
  if (paused) return;
  if (imu) {
    charts.accel.push(msg.t, imu.accel);
    charts.gyro.push(msg.t, imu.gyro);
  }
  if (mag) charts.mag.push(msg.t, mag.field);
  if (lastBattery) {
    charts.voltage.push(lastBattery[0], [lastBattery[1]]);   // ignored unless it is a new reading
    charts.soc.push(lastBattery[0], [lastBattery[2]]);
  }
}

async function loadBatteryHistory() {
  let rows;
  try {
    rows = await (await fetch("/api/battery-history")).json();
  } catch (e) {
    return;   // the stream keeps filling the charts from here on
  }
  charts.voltage.reset();
  charts.soc.reset();
  if (lastBattery) rows.push(lastBattery);
  for (const [t, v, soc] of rows) {
    charts.voltage.push(t, [v]);
    charts.soc.push(t, [soc]);
  }
}

function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => {
    loadBatteryHistory();
    fetch("/api/outputs").then((r) => r.json()).then((items) => applyOutputs(items, null), () => {});
  };
  es.onerror = () => setConn("error", "Reconnecting");
  es.onmessage = (e) => {
    lastMsgAt = performance.now();
    msgCount++;
    setConn("ok", "Live");
    onMessage(JSON.parse(e.data));
  };
}

setInterval(() => {
  setText("rate", msgCount ? `${msgCount} updates/s` : "");
  msgCount = 0;
  if (lastMsgAt && performance.now() - lastMsgAt > STALE_AFTER_MS) setConn("error", "No data");
}, 1000);

pauseBtn.addEventListener("click", () => {
  paused = !paused;
  pauseBtn.setAttribute("aria-pressed", String(paused));
  pauseBtn.textContent = paused ? "Resume charts" : "Pause charts";
  if (!paused) loadBatteryHistory();   // the server kept the gauge history while we were paused
});

const darkQuery = matchMedia("(prefers-color-scheme: dark)");
function isDark() {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : darkQuery.matches;
}
function applyTheme() {
  readColors();
  themeBtn.textContent = isDark() ? "Light theme" : "Dark theme";
  Object.values(charts).forEach((c) => { c.dirty = true; });
}
themeBtn.addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) {}
  applyTheme();
});
darkQuery.addEventListener("change", applyTheme);

function frame(now) {
  for (const c of Object.values(charts)) {
    if (c.dirty) c.draw();
    if (!c.tableWrap.hidden && c.tableDirty && now - c.tableAt > 500) c.renderTable();
  }
  requestAnimationFrame(frame);
}

document.getElementById("host").textContent = location.host;
applyTheme();
connect();
requestAnimationFrame(frame);
