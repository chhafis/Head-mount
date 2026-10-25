"use strict";
// Record page: start/stop sessions, calibration wizard, live orientation cube, sessions and playback.

const $ = (id) => document.getElementById(id);
const BLANK_GIF = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
let last = {};            // latest values of the slow (1 Hz) parts of the stream
let session = null;       // active session status from the server
let sessionSyncedAt = 0;  // performance.now() when session.seconds was received
let busy = false;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function fmtBytes(n) {
  if (n == null) return "–";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  return `${Math.round(n / 1e3)} KB`;
}

function fmtClock(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function fmtMinSec(s) {
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function fmtDuration(s) {
  if (s == null) return "–";
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

async function post(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error((await res.text()).trim() || `HTTP ${res.status}`);
  return res.json();
}

function message(text, isError) {
  const m = $("rec-msg");
  m.textContent = text || "";
  m.classList.toggle("error", Boolean(isError));
}

// ---------- status chips

function chip(state, text) {
  const c = el("span", "chip-status");
  c.dataset.state = state;
  const icon = el("span", "status-icon");
  icon.setAttribute("aria-hidden", "true");
  c.append(icon, el("span", null, text));
  return c;
}

function renderChips(msg) {
  const chips = [];
  const cam = last.camera;
  if (cam) {
    chips.push(chip(!cam.present || cam.state === "error" ? "error" : cam.state === "live" ? "ok" : "wait",
      !cam.present ? "No camera" : cam.state === "live" ? "Camera on" : cam.state === "error" ? "Camera error" : "Camera idle"));
  }
  const o = msg.orientation;
  const imuOk = msg.status && msg.status.imu && msg.status.imu.ok;
  chips.push(chip(imuOk ? "ok" : "error", imuOk && o ? `IMU ${Math.round(o.imu_hz)} Hz` : "IMU not responding"));
  const magOk = msg.status && msg.status.mag && msg.status.mag.ok;
  chips.push(chip(!magOk ? "error" : o && o.calibrated ? "ok" : "wait", !magOk ? "Magnetometer error" : o && o.calibrated ? "Calibrated" : "Not calibrated"));
  const b = msg.battery;
  chips.push(chip(b ? (b.soc < 15 ? "error" : "ok") : "wait", b ? `Battery ${Math.round(b.soc)} %` : "Battery –"));
  // Running on battery counts down to an automatic shutdown: show that first.
  const bd = last.board;
  if (bd && bd.shutting_down) chips.unshift(chip("error", "Shutting down"));
  else if (bd && bd.external_power === false && bd.shutdown_in_s != null) {
    chips.unshift(chip("error", `On battery · off in ${fmtMinSec(bd.shutdown_in_s)}`));
  } else if (bd && bd.external_power === false) chips.push(chip("wait", "On battery"));
  const w = last.wifi;
  if (w && w.available) {
    chips.push(chip(w.mode === "client" ? "ok" : "wait", w.mode === "hotspot" ? `Hotspot ${w.hotspot_ssid || ""}` : w.ssid ? `Wi-Fi ${w.ssid}` : "Wi-Fi…"));
  }
  $("chips").replaceChildren(...chips);
}

// ---------- recording

function renderRecording() {
  const on = Boolean(session);
  const btn = $("rec-button");
  btn.setAttribute("aria-pressed", String(on));
  btn.querySelector(".rec-label").textContent = on ? "Stop recording" : "Start recording";
  btn.disabled = busy || (calRunning() && !on);
  $("rec-badge").hidden = !on;
  document.body.classList.toggle("recording", on);
  const cfg = last.camera && last.camera.camera && last.camera.camera.config;
  const parts = [];
  if (cfg) parts.push(`${cfg.width}×${cfg.height} · ${cfg.fps} fps · ${cfg.record_codec.toUpperCase()}`);
  else if (last.camera && last.camera.settings) parts.push(`${last.camera.settings.width}×${last.camera.settings.height} · ${last.camera.settings.fps} fps`);
  parts.push("IMU 960 Hz · magnetometer 25 Hz");
  if (last.disk) parts.push(`${fmtBytes(last.disk.free_bytes)} free`);
  if (on) parts.push(`${session.imu_samples.toLocaleString()} IMU samples`);
  $("rec-meta").textContent = parts.join(" · ");
}

setInterval(() => {
  const s = session ? session.seconds + (performance.now() - sessionSyncedAt) / 1000 : 0;
  $("rec-timer").textContent = fmtClock(s);
}, 200);

$("rec-button").addEventListener("click", async () => {
  busy = true;
  renderRecording();
  try {
    if (session) {
      message("Saving…");
      const r = await post("/api/session", { action: "stop" });
      const c = r.result;
      message(`Saved ${c.id}: ${fmtDuration(c.duration_s)}, ${c.frames} frames, ${c.imu_samples.toLocaleString()} IMU samples` +
        (c.imu_fifo_overruns ? `, ${c.imu_fifo_overruns} IMU overruns` : ""));
      session = null;
      loadSessions();
    } else {
      message("Starting the camera…");
      const r = await post("/api/session", { action: "start" });
      session = r.session;
      sessionSyncedAt = performance.now();
      message(`Recording ${r.result.id}`);
    }
  } catch (e) {
    message(e.message, true);
  } finally {
    busy = false;
    renderRecording();
  }
});

// ---------- live cube and preview

const liveCube = Cube.create($("live-scene"));
let tareQ = null;

$("tare").addEventListener("click", () => post("/api/calibration", { action: "tare" }).catch((e) => message(e.message, true)));
$("untare").addEventListener("click", () => post("/api/calibration", { action: "reset_tare" }).catch((e) => message(e.message, true)));
$("front-axis").addEventListener("change", (e) => {
  liveCube.setFront(e.target.value);
  playerCube.setFront(e.target.value);
  post("/api/camera", { action: "settings", front_axis: e.target.value }).catch((err) => message(err.message, true));
});

function renderOrientation(o) {
  if (!o) return;
  tareQ = o.tare;
  liveCube.update(o.q, tareQ);
  $("ori-text").textContent = `Roll ${o.roll.toFixed(0)}° · Pitch ${o.pitch.toFixed(0)}° · Heading ${((o.yaw + 360) % 360).toFixed(0)}°`;
  const badge = $("heading-badge");
  badge.textContent = !o.calibrated ? "heading from gyro only (calibrate)" : o.mag_used ? "heading locked to magnetometer" : "magnetic disturbance: gyro only";
  badge.dataset.state = !o.calibrated ? "wait" : o.mag_used ? "ok" : "wait";
}

let previewOn = false;
$("preview-toggle").addEventListener("click", () => {
  previewOn = !previewOn;
  $("preview").src = previewOn ? `/api/camera.mjpg?t=${Date.now()}` : BLANK_GIF;
  $("preview").hidden = !previewOn;
  $("preview-placeholder").hidden = previewOn;
  $("preview-toggle").textContent = previewOn ? "Hide camera" : "Show camera";
  $("preview-toggle").setAttribute("aria-pressed", String(previewOn));
});
document.addEventListener("visibilitychange", () => {
  if (previewOn) $("preview").src = document.hidden ? BLANK_GIF : `/api/camera.mjpg?t=${Date.now()}`;
});

// ---------- calibration

let calRun = null;
const calRunning = () => Boolean(calRun && calRun.running);

$("cal-button").addEventListener("click", async () => {
  try {
    const r = await post("/api/calibration", { action: "start" });
    calRun = r.run;
    renderCalibration();
  } catch (e) {
    message(e.message, true);
  }
});
$("wizard-cancel").addEventListener("click", () => post("/api/calibration", { action: "cancel" }).catch(() => {}));

function renderCalibration() {
  const run = calRun;
  const running = calRunning();
  $("wizard").hidden = !running;
  $("cal-button").disabled = running || Boolean(session);
  if (run) {
    const order = ["still", "camera", "rotate", "compute"];
    const at = order.indexOf(run.step);
    for (const li of $("wizard-steps").children) {
      const i = order.indexOf(li.dataset.step);
      li.dataset.state = i < at ? "done" : i === at ? "current" : "todo";
    }
    $("wizard-message").textContent = run.message;
    $("wizard-progress").style.width = `${Math.round(100 * (run.step === "rotate" ? Math.max(run.progress, run.coverage / 0.75) : run.progress))}%`;
    $("wizard-coverage").textContent = run.step === "rotate" ? `Directions covered: ${Math.round(run.coverage * 100)} % (goal 75 %)` : "";
    const res = $("cal-result");
    if (!running && (run.error || run.result)) {
      res.hidden = false;
      res.dataset.state = run.error ? "error" : "ok";
      if (run.error) {
        res.textContent = `Calibration failed: ${run.error}`;
      } else {
        const q = run.result.quality, m = run.result.mag;
        res.textContent = `Saved. Field ${m.field_uT} µT (${m.method}), ${Math.round(q.coverage * 100)} % of directions, ` +
          `fit ±${q.residual_uT} µT, mounting ${q.alignment_confidence >= 1.5 ? "identified" : "uncertain"}, ` +
          `camera effect ${Math.hypot(...m.camera_delta_uT).toFixed(1)} µT compensated.` +
          (run.result.warnings.length ? ` ${run.result.warnings.join(" ")}` : "");
      }
    }
  }
  const c = last.calibration;
  $("cal-summary").textContent = c ? `Last: ${c.time.replace("T", " ").slice(0, 16)} · ${c.method}` : "Not calibrated yet";
}

// ---------- sessions

async function loadSessions() {
  let data;
  try {
    data = await (await fetch("/api/sessions")).json();
  } catch (e) {
    return;
  }
  $("disk-free").textContent = `${fmtBytes(data.free_bytes)} free`;
  const list = $("sessions");
  list.replaceChildren();
  if (!data.sessions.length) list.append(el("li", "empty", "No recordings yet"));
  for (const s of data.sessions) {
    const li = el("li", "session");
    const info = el("div", "session-info");
    const when = s.start_local ? new Date(s.start_local.replace(/([+-]\d\d)(\d\d)$/, "$1:$2")) : null;
    info.append(el("strong", null, when && !isNaN(when) ? when.toLocaleString([], { dateStyle: "medium", timeStyle: "medium" }) : s.id),
      el("span", "muted", s.recording ? "recording…" : `${fmtDuration(s.duration_s)} · ${fmtBytes(s.bytes)}` +
        (s.counts ? ` · ${s.counts.frames} frames · IMU ${s.counts.imu_rate_hz} Hz` : "")));
    const actions = el("div", "session-actions");
    const play = el("button", null, "Play");
    play.type = "button";
    play.disabled = s.recording || !s.video_file;
    play.addEventListener("click", () => openPlayer(s));
    const dl = el("a", "button-link", "Download");
    dl.href = `/api/sessions/${encodeURIComponent(s.id)}/download`;
    const del = el("button", "danger", "Delete");
    del.type = "button";
    del.disabled = s.recording;
    let armed = 0;   // a second tap confirms; confirm() is blocked in app WebViews
    del.addEventListener("click", async () => {
      if (Date.now() - armed > 4000) {
        armed = Date.now();
        del.textContent = "Tap again to delete";
        setTimeout(() => { if (Date.now() - armed >= 4000) del.textContent = "Delete"; }, 4100);
        return;
      }
      del.disabled = true;
      try {
        await post("/api/sessions/delete", { id: s.id });
      } catch (e) {
        message(e.message, true);
      }
      loadSessions();
    });
    actions.append(play, dl, del);
    li.append(info, actions);
    list.append(li);
  }
}

// ---------- playback

const playerCube = Cube.create($("player-scene"));
let track = null;
let playerRef = null;

async function openPlayer(s) {
  const video = $("player-video");
  $("player").hidden = false;
  $("player-info").textContent = "Loading…";
  track = null;
  const url = `/api/sessions/${encodeURIComponent(s.id)}`;
  if (s.video_file.endsWith(".mkv")) {
    video.removeAttribute("src");
    $("player-info").textContent = "This recording is MJPEG (1920×1200); browsers cannot play it. Use Download.";
  } else {
    video.src = `${url}/files/${encodeURIComponent(s.video_file)}`;
  }
  try {
    track = await (await fetch(`${url}/track?hz=30`)).json();
    playerRef = track.q.length ? track.q[0] : null;   // cube starts "straight ahead"
    $("player-info").textContent = `${s.id} · ${fmtDuration(s.duration_s)} · ${track.t.length} orientation samples shown at 30 Hz`;
  } catch (e) {
    $("player-info").textContent = `Could not load the sensor track: ${e.message}`;
  }
  $("player").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("player-close").addEventListener("click", () => {
  const video = $("player-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("player").hidden = true;
  track = null;
});

function lowerBound(a, v) {
  let lo = 0, hi = a.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (a[m] < v) lo = m + 1; else hi = m;
  }
  return Math.min(lo, a.length - 1);
}

function playerFrame() {
  if (track && track.t.length && !$("player").hidden) {
    const t = $("player-video").currentTime;
    const i = lowerBound(track.t, t);
    playerCube.update(track.q[i], playerRef);
    const a = track.accel[i], g = track.gyro[i];
    $("player-text").textContent = `t ${t.toFixed(2)} s · accel ${Math.hypot(...a).toFixed(2)} g · rotation ${Math.hypot(...g).toFixed(0)} °/s`;
  }
  requestAnimationFrame(playerFrame);
}
requestAnimationFrame(playerFrame);

// ---------- stream

function onMessage(msg) {
  for (const k of ["camera", "calibration", "wifi", "disk", "board"]) if (k in msg) last[k] = msg[k];
  if (msg.camera && msg.camera.settings) {
    const axis = msg.camera.settings.front_axis || "+x";
    if ($("front-axis").value !== axis && document.activeElement !== $("front-axis")) {
      $("front-axis").value = axis;
      liveCube.setFront(axis);
      playerCube.setFront(axis);
    }
  }
  const wasRecording = Boolean(session);
  session = msg.session;
  if (session) sessionSyncedAt = performance.now();
  if (wasRecording && !session) loadSessions();   // stopped elsewhere (another phone, shutdown)
  if ("calibration_run" in msg) calRun = msg.calibration_run;
  renderOrientation(msg.orientation);
  renderChips(msg);
  renderRecording();
  renderCalibration();
}

function connect() {
  const es = new EventSource("/api/stream");
  es.onmessage = (e) => onMessage(JSON.parse(e.data));
  es.onopen = () => loadSessions();
}

connect();
