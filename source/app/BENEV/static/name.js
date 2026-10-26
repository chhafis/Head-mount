"use strict";
// Shows this head mount's name (its host name, see /api/device) as the page heading and tab title.
(async () => {
  try {
    const d = await (await fetch("/api/device", { cache: "no-store" })).json();
    const h1 = document.querySelector("header h1");
    if (h1) h1.textContent = d.name;
    const page = document.body.dataset.page;
    document.title = page ? `${d.name} · ${page}` : d.name;
    window.deviceName = d.name;
    document.dispatchEvent(new CustomEvent("devicename", { detail: d }));
  } catch (e) { /* keep the default heading */ }
})();
