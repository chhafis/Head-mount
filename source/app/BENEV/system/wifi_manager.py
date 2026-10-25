#!/usr/bin/env python3
"""Benev Wi-Fi manager: a saved Wi-Fi network when one is in range, otherwise the setup hotspot.

Runs as root (benev-wifi.service, installed to /usr/local/lib/benev/) and drives NetworkManager
through nmcli. The dashboard runs unprivileged and talks to it over a Unix socket.

Behaviour
  - At boot NetworkManager joins a saved network by itself. If none is joined within
    BOOT_WAIT_S (immediately if nothing is saved), the hotspot starts: "Benev AI", open by
    default, at 10.42.0.1. NetworkManager's "shared" mode runs DHCP and DNS for it; DNS answers
    every name with 10.42.0.1 (dnsmasq-shared.d/benev-captive.conf), so phones open the
    dashboard's /wifi page as a captive portal.
  - In client mode, if the network is lost for LOST_WAIT_S, the hotspot starts.
  - While the hotspot is on and no device is connected to it, it scans every RETRY_S (this radio
    can scan in AP mode) and goes back to a saved network as soon as one is in range, unless
    the hotspot was turned on by hand.
  - connect writes a NetworkManager profile and activates it. The reply is sent first and the
    radio switches SWITCH_DELAY_S later, because a phone on the hotspot loses it. If joining
    fails the profile is deleted, and the hotspot comes back with the reason in last_error.

Protocol: one JSON object per line each way, on /run/benev-wifi/wifi.sock (root:netdev 0660).
  {"cmd": "status"}                       -> status (see Manager.status)
  {"cmd": "scan"}                         -> status after a fresh scan
  {"cmd": "connect", "ssid", "password"}  -> {"ok": true, "pending": true, "message"}
      (no password for a saved network: uses the saved one)
  {"cmd": "forget", "ssid"}               -> {"ok": true, "message", "pending"?}
  {"cmd": "hotspot"} | {"cmd": "rejoin"}  -> {"ok": true, "pending": true, "message"}
  errors                                  -> {"ok": false, "error"}

Settings (optional), /etc/benev/wifi.json; restart benev-wifi after editing:
  {"hotspot_ssid": "Benev AI", "hotspot_password": "", "interface": "wlan0"}
  An empty password makes an open hotspot; otherwise 8-63 characters (WPA2).
"""
import grp
import json
import logging
import os
import re
import socket
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path

SOCKET = Path("/run/benev-wifi/wifi.sock")
SETTINGS = Path("/etc/benev/wifi.json")
PROFILE_DIR = Path("/etc/NetworkManager/system-connections")
HOTSPOT_ID = "benev-hotspot"
HOTSPOT_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, "benev:hotspot"))
HOTSPOT_IP = "10.42.0.1"
DEFAULTS = {"hotspot_ssid": "Benev AI", "hotspot_password": "", "interface": "wlan0"}

POLL_S = 3
BOOT_WAIT_S = 45            # NetworkManager's time to join a saved network after boot
NOTHING_SAVED_WAIT_S = 8    # ... when there is no saved network at all
LOST_WAIT_S = 60            # client mode: offline this long -> hotspot
RETRY_S = 90                # hotspot with nobody on it: look for saved networks this often
FAILED_BACKOFF_S = 600      # automatic retries skip a network that just failed for this long
HOTSPOT_RETRY_S = 30        # after the hotspot failed to start
SIGNAL_REFRESH_S = 10
SWITCH_DELAY_S = 2.0        # lets the HTTP reply reach the phone before the radio switches
JOIN_TIMEOUT_S = 40
SCAN_TIMEOUT_S = 30
MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
ENV = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8")   # English messages, UTF-8 SSIDs

log = logging.getLogger("benev-wifi")


class Busy(Exception):
    pass


# --- nmcli and keyfiles ------------------------------------------------------------------------

def nmcli(*args, timeout=20):
    """-> (ok, stdout, first line of stderr). nmcli failures are returned, not raised."""
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, env=ENV, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "", f"nmcli {' '.join(args[:3])} timed out"
    err = next((l for l in r.stderr.splitlines() if l.strip()), "")
    return r.returncode == 0, r.stdout, err.strip()


def split_terse(line):
    """Fields of one `nmcli -t` table row: ':' separated, with '\\' escaping ':' and '\\'."""
    fields, cur, chars = [], [], iter(line)
    for c in chars:
        if c == "\\":
            cur.append(next(chars, ""))
        elif c == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    fields.append("".join(cur))
    return fields


def key_values(out):
    """`nmcli -t -f ... show` multi-line output (values unescaped) -> dict; [n] suffixes dropped."""
    info = {}
    for line in out.splitlines():
        key, _, value = line.partition(":")
        info.setdefault(key.split("[")[0], value)
    return info


def kf(value):
    """Escape a string for a NetworkManager keyfile (GLib key-file syntax)."""
    s = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return "\\s" + s[1:] if s.startswith(" ") else s


def ssid_bytes(ssid):
    """SSID as a keyfile byte list, so any name round-trips."""
    return "".join(f"{b};" for b in ssid.encode())


def write_profile(path, sections):
    """Write a keyfile (root-only, as NetworkManager requires). -> True if the content changed."""
    text = "".join(f"[{name}]\n" + "".join(f"{k}={v}\n" for k, v in items.items()) + "\n"
                   for name, items in sections.items())
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return True


def check_ssid(ssid):
    if not 0 < len(ssid.encode()) <= 32 or any(ord(c) < 32 for c in ssid):
        raise ValueError("a network name is 1 to 32 bytes, without control characters")


def check_password(password):
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ValueError("the password contains control characters")
    if not (8 <= len(password.encode()) <= 63 or re.fullmatch(r"[0-9A-Fa-f]{64}", password)):
        raise ValueError("Wi-Fi passwords are 8 to 63 characters")


def key_mgmt_for(security):
    """nmcli's SECURITY column -> keyfile key-mgmt; None for an open network."""
    sec = security.replace("--", "").strip()
    if "802.1X" in sec:
        raise ValueError("enterprise (802.1X) networks need a user name and certificate; not supported here")
    if "WEP" in sec:
        raise ValueError("WEP networks are not supported")
    if not sec or sec == "OWE":
        return None
    if "WPA3" in sec and "WPA1" not in sec and "WPA2" not in sec:
        return "sae"
    return "wpa-psk"


def explain(err):
    """Short reason for a failed `nmcli connection up`."""
    e = err.lower()
    if "secrets were required" in e or "no-secrets" in e or "no secrets" in e:
        return "the password was not accepted"
    if "not found" in e or "could not be found" in e or "access point not found" in e:
        return "the network is not in range"
    if "timeout" in e or "timed out" in e:
        return "it timed out (check the password and the signal)"
    return re.sub(r"^Error: (Connection activation failed: )?", "", err) or "unknown error"


def load_settings():
    settings = dict(DEFAULTS)
    try:
        data = json.loads(SETTINGS.read_text())
        settings.update({k: str(v) for k, v in data.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as e:
        log.warning("ignoring %s: %s", SETTINGS, e)
    try:
        check_ssid(settings["hotspot_ssid"])
    except ValueError as e:
        log.warning("hotspot_ssid: %s; using %r", e, DEFAULTS["hotspot_ssid"])
        settings["hotspot_ssid"] = DEFAULTS["hotspot_ssid"]
    if settings["hotspot_password"]:
        try:
            check_password(settings["hotspot_password"])
        except ValueError as e:
            log.warning("hotspot_password: %s; the hotspot will be open", e)
            settings["hotspot_password"] = ""
    return settings


# --- the manager ------------------------------------------------------------------------------

class Manager:
    def __init__(self):
        self.settings = load_settings()
        self.iface = self.settings["interface"]
        self.job_lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.job = None                # what is being changed right now, or None
        self.mode = "starting"         # client | hotspot | connecting | disconnected | unavailable
        self.ssid = self.ip = self.signal = None
        self.clients = None            # devices on the hotspot (None = unknown)
        self.networks = []             # last scan, strongest first
        self.scanned_at = self.signal_at = 0.0
        self.saved = []                # [{"name", "uuid", "ssid"}] client profiles
        self.saved_at = 0.0
        self.message = ""
        self.last_error = None
        self.hold = False              # hotspot turned on by hand: don't leave it automatically
        self.ever_connected = False
        self.offline_since = None
        self.last_retry = 0.0
        self.hotspot_failed_at = -HOTSPOT_RETRY_S
        self.failed = {}               # ssid -> time of the last failed join

    # ----- reading NetworkManager

    def read_state(self):
        ok, out, _ = nmcli("-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", self.iface)
        info = key_values(out) if ok else {}
        m = re.match(r"\d+", info.get("GENERAL.STATE", ""))
        state = int(m.group()) if m else 0
        conn = info.get("GENERAL.CONNECTION", "").strip()
        conn = None if conn in ("", "--") else conn
        if state == 100 and conn == HOTSPOT_ID:
            mode = "hotspot"
        elif state == 100:
            mode = "client"
        elif 40 <= state < 100:
            mode = "connecting"
        elif state in (0, 10, 20):
            mode = "unavailable"
        else:
            mode = "disconnected"
        ip = info.get("IP4.ADDRESS", "").split("/")[0] or None
        ssid = None
        if mode == "hotspot":
            ssid = self.settings["hotspot_ssid"]
            self.clients = self.count_clients()
        elif conn:
            ssid = next((p["ssid"] for p in self.saved if p["name"] == conn), conn)
        if mode != "client":
            self.signal = None
        elif time.monotonic() - self.signal_at > SIGNAL_REFRESH_S or ssid != self.ssid:
            self.signal_at = time.monotonic()
            self.scan(rescan=False)
        self.mode, self.ssid, self.ip = mode, ssid, ip

    def count_clients(self):
        try:
            r = subprocess.run(["wpa_cli", "-p", "/run/wpa_supplicant", "-i", self.iface, "all_sta"],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return sum(1 for line in r.stdout.splitlines() if MAC.fullmatch(line.strip()))

    def scan(self, rescan=True):
        """Refresh the network list (rescan=False: NetworkManager's cached results only)."""
        with self.scan_lock:
            ok, out, err = nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "device", "wifi", "list",
                                 "ifname", self.iface, "--rescan", "yes" if rescan else "no",
                                 timeout=SCAN_TIMEOUT_S)
            if not ok:
                log.warning("scan failed: %s", err)
                return
            best = {}
            for line in out.splitlines():
                f = split_terse(line)
                if len(f) < 5 or not f[1] or f[1] == self.settings["hotspot_ssid"]:
                    continue   # hidden networks, and our own hotspot
                net = {"ssid": f[1], "signal": int(f[2]) if f[2].isdigit() else 0,
                       "security": f[3].replace("--", "").strip(), "channel": f[4], "active": f[0] == "*"}
                old = best.get(net["ssid"])
                if old is None or net["active"] or (net["signal"] > old["signal"] and not old["active"]):
                    best[net["ssid"]] = net
                if net["active"]:
                    self.signal = net["signal"]
            self.networks = sorted(best.values(), key=lambda n: -n["signal"])
            if rescan:
                self.scanned_at = time.monotonic()

    def refresh_saved(self):
        ok, out, _ = nmcli("-t", "-f", "NAME,UUID,TYPE", "connection", "show")
        saved = []
        for line in out.splitlines() if ok else []:
            name, u, typ = (split_terse(line) + ["", "", ""])[:3]
            if typ != "802-11-wireless" or u == HOTSPOT_UUID or name == HOTSPOT_ID:
                continue
            ok, detail, _ = nmcli("-t", "-f", "802-11-wireless.ssid,802-11-wireless.mode", "connection", "show", "uuid", u)
            d = key_values(detail) if ok else {}
            if d.get("802-11-wireless.mode", "") not in ("", "infrastructure"):
                continue   # other access-point / ad-hoc profiles
            saved.append({"name": name, "uuid": u, "ssid": d.get("802-11-wireless.ssid", name)})
        self.saved = saved
        self.saved_at = time.monotonic()

    def saved_in_range(self, automatic=False, exclude=None):
        """Saved profiles whose network is in the last scan, strongest first."""
        signal = {n["ssid"]: n["signal"] for n in self.networks}
        now = time.monotonic()
        found = [p for p in self.saved if p["ssid"] in signal and p["ssid"] != exclude
                 and not (automatic and now - self.failed.get(p["ssid"], -FAILED_BACKOFF_S) < FAILED_BACKOFF_S)]
        return sorted(found, key=lambda p: -signal[p["ssid"]])

    # ----- monitor

    def run(self):
        ok, out, _ = nmcli("radio", "wifi")
        if ok and out.strip() != "enabled":
            log.info("Wi-Fi radio was off; turning it on")
            subprocess.run(["rfkill", "unblock", "wifi"], capture_output=True)
            nmcli("radio", "wifi", "on")
        self.write_hotspot_profile()
        self.refresh_saved()
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("monitor")
            time.sleep(POLL_S)

    def tick(self):
        now = time.monotonic()
        if now - self.saved_at > 60:
            self.refresh_saved()
        self.read_state()
        if self.job:
            return
        if self.mode == "client":
            self.offline_since = None
            self.ever_connected = True
        elif self.mode == "hotspot":
            self.offline_since = None
            if not self.hold and not self.clients and now - self.last_retry > RETRY_S:
                self.last_retry = now
                self.scan()
                targets = self.saved_in_range(automatic=True)
                if targets:
                    log.info("saved network %s is in range; leaving the hotspot", targets[0]["ssid"])
                    self.start_job(f"Joining {targets[0]['ssid']}", self._rejoin, targets, False, delay=0)
        else:
            if self.offline_since is None:
                self.offline_since = now
            wait = LOST_WAIT_S if self.ever_connected else BOOT_WAIT_S if self.saved else NOTHING_SAVED_WAIT_S
            if now - self.offline_since > wait and now - self.hotspot_failed_at > HOTSPOT_RETRY_S:
                reason = "the Wi-Fi network was lost" if self.ever_connected else "no saved network in range"
                self.start_job("Starting the hotspot", self._hotspot, reason, delay=0)

    # ----- changes (one at a time, in a background thread)

    def start_job(self, label, fn, *args, delay=SWITCH_DELAY_S):
        with self.job_lock:
            if self.job:
                raise Busy(f"busy: {self.job}")
            self.job = label
        self.message = label

        def run():
            try:
                time.sleep(delay)
                fn(*args)
            except Exception as e:
                log.exception(label)
                self.last_error = str(e)
            finally:
                self.refresh_saved()
                self.read_state()
                with self.job_lock:
                    self.job = None
        threading.Thread(target=run, daemon=True, name="wifi-job").start()

    def write_hotspot_profile(self):
        s = self.settings
        sections = {
            "connection": {"id": HOTSPOT_ID, "uuid": HOTSPOT_UUID, "type": "wifi",
                           "interface-name": self.iface, "autoconnect": "false"},
            "wifi": {"mode": "ap", "ssid": ssid_bytes(s["hotspot_ssid"]), "band": "bg", "channel": "6"},
        }
        if s["hotspot_password"]:
            sections["wifi-security"] = {"key-mgmt": "wpa-psk", "proto": "rsn", "pairwise": "ccmp",
                                         "group": "ccmp", "psk": kf(s["hotspot_password"])}
        sections["ipv4"] = {"method": "shared", "address1": f"{HOTSPOT_IP}/24"}
        sections["ipv6"] = {"method": "disabled"}
        path = PROFILE_DIR / f"{HOTSPOT_ID}.nmconnection"
        if write_profile(path, sections):
            ok, _, err = nmcli("connection", "load", str(path))
            log.info("hotspot profile written (%s)", "loaded" if ok else err)

    def _activate(self, profile_uuid):
        ok, _, err = nmcli("--wait", str(JOIN_TIMEOUT_S), "connection", "up", "uuid", profile_uuid,
                           timeout=JOIN_TIMEOUT_S + 15)
        return ok, err

    def _hotspot(self, reason=None):
        if self.mode != "hotspot":
            self.scan()   # fresh list for the setup page
        self.write_hotspot_profile()
        ok, _, err = nmcli("--wait", "30", "connection", "up", "uuid", HOTSPOT_UUID, timeout=45)
        if not ok:
            self.hotspot_failed_at = time.monotonic()
            raise RuntimeError(f"Could not start the hotspot: {err}")
        self.last_retry = time.monotonic()
        self.message = f"Hotspot {self.settings['hotspot_ssid']} is on" + (f": {reason}" if reason else "")
        log.info(self.message)

    def _rejoin(self, candidates=None, requested=True, exclude=None):
        """Join the strongest saved network in range (or the given ones); otherwise the hotspot."""
        if candidates is None:
            self.scan()
            candidates = self.saved_in_range(automatic=not requested, exclude=exclude)
        for p in candidates:
            ok, err = self._activate(p["uuid"])
            if ok:
                self._joined(p["ssid"])
                return
            self.failed[p["ssid"]] = time.monotonic()
            self.last_error = f"Could not join {p['ssid']}: {explain(err)}"
            log.warning("%s (%s)", self.last_error, err)
        if not candidates and requested:
            self.last_error = "None of the saved networks is in range"
        self._hotspot("no saved network could be joined")

    def _connect_new(self, ssid, password, key_mgmt, hidden, was_hotspot):
        u = str(uuid.uuid4())
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", ssid)[:24]
        path = PROFILE_DIR / f"benev-{safe}-{u[:8]}.nmconnection"
        sections = {
            "connection": {"id": kf(ssid), "uuid": u, "type": "wifi", "autoconnect": "true"},
            "wifi": {"mode": "infrastructure", "ssid": ssid_bytes(ssid), **({"hidden": "true"} if hidden else {})},
        }
        if key_mgmt:
            sections["wifi-security"] = {"key-mgmt": key_mgmt, "psk": kf(password)}
        sections["ipv4"] = {"method": "auto"}
        sections["ipv6"] = {"addr-gen-mode": "default", "method": "auto"}
        write_profile(path, sections)
        ok, _, err = nmcli("connection", "load", str(path))
        if ok:
            ok, err = self._activate(u)
        if ok:
            for p in self.saved:   # the new profile replaces older ones for the same network
                if p["ssid"] == ssid and p["uuid"] != u:
                    nmcli("connection", "delete", "uuid", p["uuid"])
            self._joined(ssid)
            return
        nmcli("connection", "delete", "uuid", u)
        path.unlink(missing_ok=True)
        self.failed[ssid] = time.monotonic()
        self.last_error = f"Could not join {ssid}: {explain(err)}"
        log.warning("%s (%s)", self.last_error, err)
        self._recover(was_hotspot, ssid)

    def _recover(self, was_hotspot, failed_ssid):
        """After a requested join failed: back to the hotspot if the user is on it, else any saved network."""
        if was_hotspot:
            self._hotspot(f"joining {failed_ssid} failed")
            return
        time.sleep(5)   # NetworkManager often rejoins the previous network by itself
        self.read_state()
        if self.mode != "client":
            error = self.last_error
            self._rejoin(requested=False, exclude=failed_ssid)
            self.last_error = error

    def _joined(self, ssid):
        self.failed.pop(ssid, None)
        self.hold = False
        self.last_error = None
        self.ever_connected = True
        self.message = f"Connected to {ssid}"
        log.info(self.message)

    def _forget_active(self, profiles):
        for p in profiles:
            nmcli("connection", "delete", "uuid", p["uuid"])
        self.refresh_saved()
        self._rejoin(requested=False)

    # ----- requests from the dashboard

    def handle(self, req):
        cmd = req.get("cmd")
        if cmd == "status":
            return self.status()
        if cmd == "scan":
            if not self.job:
                self.scan()
            return self.status()
        if cmd == "connect":
            return self.request_connect(str(req.get("ssid") or ""), str(req.get("password") or ""))
        if cmd == "forget":
            return self.request_forget(str(req.get("ssid") or ""))
        if cmd == "hotspot":
            self.start_job("Starting the hotspot", self._hotspot, "turned on from the dashboard")
            self.hold = True
            return {"ok": True, "pending": True, "message": self.message}
        if cmd == "rejoin":
            self.start_job("Looking for saved networks", self._rejoin)
            self.hold = False
            return {"ok": True, "pending": True, "message": self.message}
        raise ValueError(f"unknown command {cmd!r}")

    def request_connect(self, ssid, password):
        check_ssid(ssid)
        if password:
            check_password(password)
        was_hotspot = self.mode == "hotspot"
        saved = [p for p in self.saved if p["ssid"] == ssid]
        if saved and not password:
            self.start_job(f"Joining {ssid}", self._join_saved, saved[0], was_hotspot)
        else:
            net = next((n for n in self.networks if n["ssid"] == ssid), None)
            if net:
                key_mgmt = key_mgmt_for(net["security"])
            else:   # not in the scan: a hidden network
                key_mgmt = "wpa-psk" if password else None
            if key_mgmt and not password:
                raise ValueError(f"{ssid} needs a password")
            self.start_job(f"Joining {ssid}", self._connect_new, ssid, password if key_mgmt else "",
                           key_mgmt, net is None, was_hotspot)
        self.last_error = None
        return {"ok": True, "pending": True, "message": self.message, "was_hotspot": was_hotspot}

    def _join_saved(self, profile, was_hotspot):
        ok, err = self._activate(profile["uuid"])
        if ok:
            self._joined(profile["ssid"])
            return
        self.failed[profile["ssid"]] = time.monotonic()
        self.last_error = f"Could not join {profile['ssid']}: {explain(err)}"
        log.warning("%s (%s)", self.last_error, err)
        self._recover(was_hotspot, profile["ssid"])

    def request_forget(self, ssid):
        profiles = [p for p in self.saved if p["ssid"] == ssid]
        if not profiles:
            raise ValueError(f"{ssid} is not a saved network")
        if self.mode == "client" and self.ssid == ssid:
            # Deleting the active profile drops the link, so reply first.
            self.start_job(f"Forgetting {ssid}", self._forget_active, profiles)
            return {"ok": True, "pending": True, "message": self.message}
        for p in profiles:
            nmcli("connection", "delete", "uuid", p["uuid"])
        self.refresh_saved()
        self.message = f"Forgot {ssid}"
        return {"ok": True, "message": self.message}

    def status(self):
        s = self.settings
        saved = {p["ssid"] for p in self.saved}
        return {
            "ok": True,
            "mode": self.mode, "ssid": self.ssid, "ip": self.ip, "signal": self.signal,
            "hostname": f"{socket.gethostname().lower()}.local",
            "hotspot_ssid": s["hotspot_ssid"], "hotspot_open": not s["hotspot_password"],
            "hotspot_ip": HOTSPOT_IP, "hotspot_clients": self.clients, "hold": self.hold,
            "networks": [dict(n, saved=n["ssid"] in saved) for n in self.networks],
            "scan_age_s": round(time.monotonic() - self.scanned_at) if self.scanned_at else None,
            "saved": sorted(saved),
            "busy": self.job, "message": self.message, "last_error": self.last_error,
        }


# --- socket server ------------------------------------------------------------------------------

class Handler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self):
        try:
            req = json.loads(self.rfile.readline(65536))
            if not isinstance(req, dict):
                raise ValueError("expected a JSON object")
            reply = self.server.manager.handle(req)
        except (ValueError, Busy) as e:
            reply = {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("request")
            reply = {"ok": False, "error": f"internal error: {e}"}
        self.wfile.write(json.dumps(reply).encode() + b"\n")


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    manager = Manager()
    threading.Thread(target=manager.run, daemon=True, name="monitor").start()
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    server = Server(str(SOCKET), Handler)
    server.manager = manager
    try:
        gid = grp.getgrnam("netdev").gr_gid
    except KeyError:
        gid = 0
    os.chown(SOCKET, 0, gid)
    os.chmod(SOCKET, 0o660)
    log.info("listening on %s", SOCKET)
    server.serve_forever()


if __name__ == "__main__":
    main()
