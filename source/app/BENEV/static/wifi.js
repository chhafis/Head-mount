"use strict";
// Wi-Fi page, also the captive portal of the Benev hotspot: status, networks, join and forget.
// Polls /api/wifi (no event stream: phones on the setup hotspot don't need the sensor data).

const $ = (id) => document.getElementById(id);
const POLL_MS = 2000;
let status = null;
let selected = null;   // SSID whose form is open ("" = a hidden network); the list is frozen meanwhile
let pending = null;    // {ssid, wasHotspot} after asking Benev to switch networks
let listKey = "";
let scannedOnLoad = false;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function link(url) {
  const a = el("a", null, url.replace(/\/$/, ""));
  a.href = url;
  return a;
}

async function post(body) {
  const res = await fetch("/api/wifi", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error((await res.text()).trim() || `HTTP ${res.status}`);
  const reply = await res.json();
  if (reply.ok === false) throw new Error(reply.error || "failed");
  return reply;
}

function message(text, isError) {
  const m = $("wifi-msg");
  m.textContent = text || "";
  m.classList.toggle("error", Boolean(isError));
}

function setState(state, text) {
  const s = $("wifi-state");
  s.dataset.state = state;
  s.querySelector(".status-text").textContent = text;
}

// A button that asks for a second tap instead of confirm(), which captive-portal browsers block.
function twoStep(button, prompt, action) {
  let armed = 0;
  const label = button.textContent;
  button.addEventListener("click", async () => {
    if (Date.now() - armed > 4000) {
      armed = Date.now();
      button.textContent = prompt;
      setTimeout(() => { if (Date.now() - armed >= 4000) button.textContent = label; }, 4100);
      return;
    }
    armed = 0;
    button.textContent = label;
    await action();
  });
}

// ---------- status

async function poll() {
  try {
    const res = await fetch("/api/wifi", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    status = await res.json();
    render();
    if (!scannedOnLoad && status.available && !status.busy) {
      scannedOnLoad = true;
      if (status.scan_age_s == null || status.scan_age_s > 30) scan();
    }
  } catch (e) {
    renderLost();
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

function clientsText(n) {
  if (n == null) return "";
  return n === 1 ? "1 device connected" : `${n} devices connected`;
}

function render() {
  const s = status;
  if (!s.available) {
    setState("error", "Wi-Fi manager not running");
    $("wifi-detail").textContent = `${s.error || ""} Check: sudo systemctl status benev-wifi`;
    return;
  }
  const modes = {
    client: ["ok", `Connected to ${s.ssid}`],
    hotspot: ["ok", `Hotspot “${s.hotspot_ssid}” on`],
    connecting: ["wait", "Joining a saved network…"],
    disconnected: ["wait", "Not connected"],
    unavailable: ["error", "Wi-Fi is off or missing"],
    starting: ["wait", "Starting"],
  };
  const [state, text] = s.busy ? ["wait", `${s.busy}…`] : (modes[s.mode] || ["wait", s.mode]);
  setState(state, text);

  let detail = "";
  if (s.mode === "client") {
    detail = `Signal ${s.signal == null ? "–" : `${s.signal} %`} · IP address ${s.ip || "–"}`;
  } else if (s.mode === "hotspot") {
    detail = [`Network “${s.hotspot_ssid}”`, s.hotspot_open ? "open, no password" : "password protected",
      clientsText(s.hotspot_clients)].filter(Boolean).join(" · ") + ". " +
      (s.hold ? "It stays on until you choose a network."
        : "Benev goes back to a saved network by itself when one is in range and no device is on the hotspot.");
  } else if (s.mode !== "unavailable") {
    detail = `If no saved network is found, Benev starts its own hotspot “${s.hotspot_ssid}” within about a minute.`;
  }
  $("wifi-detail").textContent = detail;

  const addr = $("wifi-address");
  const host = `http://${s.hostname || "benev.local"}/`;
  addr.replaceChildren("Recorder: ", link(host));
  if (s.mode === "hotspot") addr.append(" or ", link(`http://${s.hotspot_ip}/`));
  else if (s.mode === "client" && s.ip) addr.append(" or ", link(`http://${s.ip}/`));

  if (s.last_error && !s.busy) message(s.last_error, true);
  else if (!pending) message(s.busy ? "" : s.message);

  $("rejoin-btn").hidden = s.mode !== "hotspot" || !s.saved.length;
  $("hotspot-btn").hidden = s.mode === "hotspot" || s.mode === "unavailable";
  for (const id of ["rejoin-btn", "hotspot-btn", "scan-btn"]) $(id).disabled = Boolean(s.busy);

  const age = s.scan_age_s;
  $("scan-age").textContent = age == null ? "" : age < 5 ? "Scanned just now" : `Scanned ${age < 90 ? `${age} s` : `${Math.round(age / 60)} min`} ago`;

  if (pending && !s.busy) {
    if (pending.ssid && s.mode === "client" && s.ssid === pending.ssid) {
      showSwitch([el("strong", null, `Connected to “${s.ssid}”.`)]);
      pending = null;
      setTimeout(() => { if (!pending) $("wifi-switch").hidden = true; }, 8000);
    } else if (s.last_error || (!pending.ssid && s.mode !== "connecting")) {
      pending = null;
      $("wifi-switch").hidden = true;
    }
  }
  renderNetworks();
}

function renderLost() {
  setState("error", "Lost contact with Benev");
  $("wifi-detail").textContent = pending
    ? "Expected while Benev switches networks; follow the steps below."
    : "Benev may be switching networks, restarting, or out of range. This page keeps trying.";
}

function showSwitch(children) {
  const box = $("wifi-switch");
  box.replaceChildren(...children);
  box.hidden = false;
}

function renderSwitch() {
  const p = pending;
  const target = p.ssid ? `“${p.ssid}”` : "a saved network";
  const steps = el("ol");
  if (p.wasHotspot) {
    steps.append(el("li", null, `Your phone will drop off the “${status.hotspot_ssid}” hotspot in a moment.`));
    steps.append(el("li", null, p.ssid ? `Connect your phone to ${target}.` : "Connect your phone to the same Wi-Fi network as Benev."));
    const open = el("li", null, "Open ");
    open.append(link(`http://${status.hostname || "benev.local"}/`), ".");
    steps.append(open);
    showSwitch([el("strong", null, `Benev is joining ${target}.`), steps,
      el("p", null, `If it can't join (for example because of a wrong password), the “${status.hotspot_ssid}” hotspot comes back within about a minute. Reconnect to it and this page shows the reason.`)]);
  } else {
    const tip = el("p", null, `This page keeps updating if your phone is on ${target} too. Otherwise connect the phone to it and open `);
    tip.append(link(`http://${status.hostname || "benev.local"}/`), ".");
    showSwitch([el("strong", null, `Benev is switching to ${target}.`), tip]);
  }
}

// ---------- networks

function signalBars(signal) {
  const bars = el("span", "signal-bars");
  bars.setAttribute("aria-hidden", "true");
  for (const level of [1, 30, 55, 80]) {
    const b = el("i", signal >= level ? "on" : null);
    bars.append(b);
  }
  return bars;
}

function renderNetworks() {
  if (selected !== null) return;   // keep an open form (and its typed password) intact
  const s = status;
  const key = JSON.stringify([s.networks, s.mode, s.ssid, s.scan_age_s == null]);
  if (key === listKey) return;
  listKey = key;
  const items = s.networks.map(networkItem);
  if (!s.networks.length) {
    items.push(el("li", "empty muted", s.scan_age_s == null ? "Scanning…" : "No networks found. Scan again, or add a hidden network below."));
  }
  items.push(networkItem(null));
  $("networks").replaceChildren(...items);
}

function networkItem(n) {
  const li = el("li");
  li.dataset.ssid = n ? n.ssid : "";
  const b = el("button", "network");
  b.type = "button";
  b.setAttribute("aria-expanded", "false");
  const main = el("span", "net-main");
  main.append(el("span", "net-name", n ? n.ssid : "Other network…"));
  const sub = el("span", "net-sub");
  if (n) {
    if (n.active || n.saved) {
      const badge = el("span", "badge", n.active ? "Connected" : "Saved");
      if (n.active) badge.dataset.state = "ok";
      sub.append(badge);
    }
    sub.append(el("span", null, n.security ? `Secured (${n.security})` : "Open"));
  } else {
    sub.append(el("span", null, "Hidden network: type its name"));
  }
  main.append(sub);
  b.append(main);
  if (n) {
    const sig = el("span", "signal");
    sig.append(signalBars(n.signal), ` ${n.signal} %`);
    b.append(sig);
  }
  b.addEventListener("click", () => (selected === li.dataset.ssid ? closeForm() : openForm(n)));
  li.append(b);
  return li;
}

function closeForm() {
  selected = null;
  listKey = "";
  if (status) renderNetworks();
}

function field(labelText, input) {
  const label = el("label", null, labelText);
  label.append(input);
  return label;
}

function textInput(type, attrs) {
  const input = el("input");
  input.type = type;
  Object.assign(input, { autocomplete: "off", spellcheck: false }, attrs);
  input.setAttribute("autocapitalize", "none");
  input.setAttribute("autocorrect", "off");
  return input;
}

function openForm(n) {
  if (selected !== null) closeForm();   // re-renders the list, so look the item up afterwards
  selected = n ? n.ssid : "";
  const li = [...$("networks").children].find((x) => x.dataset.ssid === selected);
  li.querySelector("button.network").setAttribute("aria-expanded", "true");

  const form = el("form", "connect-form");
  const err = el("p", "camera-msg error");
  err.hidden = true;
  let ssidInput = null;
  if (!n) {
    ssidInput = textInput("text", { maxLength: 32, required: true, placeholder: "Network name" });
    form.append(field("Network name", ssidInput));
  }
  const secured = !n || Boolean(n.security);
  const pw = textInput("password", { maxLength: 64, placeholder: n && n.saved ? "Saved password" : "Password" });
  const pwLabel = field(n ? "Password" : "Password (leave empty for an open network)", pw);
  const show = el("label", "check");
  const showBox = el("input");
  showBox.type = "checkbox";
  showBox.addEventListener("change", () => { pw.type = showBox.checked ? "text" : "password"; });
  show.append(showBox, " Show password");

  if (n && n.saved) {
    // Saved: connect with the stored password, or type a new one.
    const change = el("button", null, "Change password");
    change.type = "button";
    pwLabel.hidden = show.hidden = true;
    change.addEventListener("click", () => { pwLabel.hidden = show.hidden = false; change.hidden = true; pw.required = secured; pw.focus(); });
    form.append(pwLabel, show, change);
  } else if (secured) {
    pw.required = Boolean(n);
    pw.minLength = n ? 8 : 0;
    form.append(pwLabel, show);
  } else {
    form.append(el("p", "muted", "Open network: no password needed."));
  }

  const row = el("div", "row");
  const go = el("button", "primary", n && n.active ? "Reconnect" : "Connect");
  go.type = "submit";
  const cancel = el("button", null, "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", closeForm);
  row.append(go);
  if (n && n.saved) {
    const forget = el("button", "danger", "Forget");
    forget.type = "button";
    twoStep(forget, "Tap again to forget", async () => {
      try {
        const r = await post({ action: "forget", ssid: n.ssid });
        if (r.pending) { pending = { ssid: null, wasHotspot: false }; renderSwitch(); }
        message(r.message);
        closeForm();
      } catch (e) {
        err.textContent = e.message;
        err.hidden = false;
      }
    });
    row.append(forget);
  }
  row.append(cancel);
  form.append(err, row);

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const ssid = n ? n.ssid : ssidInput.value;
    const password = pwLabel.hidden ? "" : pw.value;
    if (password && (new TextEncoder().encode(password).length < 8 || password.length > 64)) {
      err.textContent = "Wi-Fi passwords are 8 to 63 characters.";
      err.hidden = false;
      return;
    }
    go.disabled = true;
    try {
      const r = await post({ action: "connect", ssid, password });
      pending = { ssid, wasHotspot: Boolean(r.was_hotspot) };
      message("");
      renderSwitch();
      closeForm();
      $("wifi-switch").scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
      go.disabled = false;
    }
  });
  li.append(form);
  (ssidInput || (pwLabel.hidden ? go : pw)).focus();
}

// ---------- actions

async function scan() {
  const b = $("scan-btn");
  b.disabled = true;
  b.textContent = "Scanning…";
  try {
    status = { available: true, ...(await post({ action: "scan" })) };
    render();
  } catch (e) {
    message(e.message, true);
  } finally {
    b.textContent = "Scan again";
    b.disabled = Boolean(status && status.busy);
  }
}

$("scan-btn").addEventListener("click", scan);

twoStep($("hotspot-btn"), "Tap again: this phone may lose the page", async () => {
  try {
    const r = await post({ action: "hotspot" });
    message(r.message);
    const steps = el("ol");
    steps.append(el("li", null, `Connect your phone to “${status.hotspot_ssid}”${status.hotspot_open ? " (no password)" : ""}.`));
    const open = el("li", null, "Open ");
    open.append(link(`http://${status.hotspot_ip}/`), " or ", link(`http://${status.hostname || "benev.local"}/`), ".");
    steps.append(open);
    showSwitch([el("strong", null, "Benev is turning on its hotspot."), steps]);
  } catch (e) {
    message(e.message, true);
  }
});

$("rejoin-btn").addEventListener("click", async () => {
  try {
    const r = await post({ action: "rejoin" });
    pending = { ssid: null, wasHotspot: true };
    message(r.message);
    renderSwitch();
  } catch (e) {
    message(e.message, true);
  }
});

poll();
