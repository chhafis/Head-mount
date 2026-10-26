"""BENEV head-mount recorder: web app for the Pi Zero 2 W.

Pages: /        recording (record button, calibration, orientation cube, sessions)
       /device  sensors, fan/LEDs, camera settings, photos, shutdown
       /wifi    Wi-Fi setup (also the captive portal while the hotspot is on)

Live data is pushed over Server-Sent Events from /api/stream. Recording sessions (video +
IMU + orientation + magnetometer on one clock) are under /api/sessions; see recorder.py for
the folder format. POST endpoints take JSON:
  /api/session      {"action": "start" | "stop"}
  /api/sessions/delete  {"id"}
  /api/calibration  {"action": "start" | "cancel" | "tare" | "reset_tare"}
  /api/wifi         {"action": "connect", "ssid", "password"} | "scan" | "forget" | "hotspot" | "rejoin"
  /api/outputs      {"name", "level": 0-100}           (manual outputs only)
  /api/board        {"fan_normal_pct", "fan_full_c", "fan_back_c", "battery_low_pct", "unplug_shutdown_s"}
  /api/camera       {"action": "still" | "record_start" | "record_stop" | "configure" | "settings"
                     | "lens_calibrate" | "lens_reset" | "image" | "image_reset" | "brightness"
                     | "edge_boost", ...}
  /api/system       {"action": "shutdown" | "reboot"}
  /api/update       {"action": "check" | "install"}       (GET: state of the GitHub updater)
  /api/device       {"name"}  rename the head mount (name.local)   (GET: {"name", "address", "version"})
GET /api/status: version, recording, power and battery in one small object.

    python3 server.py [--port 80 --port 8080]
"""
import argparse
import collections
import json
import logging
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import lens_shading
from board import Board
from camera import MEDIA, MEDIA_TYPES, CameraError, CameraFeed, list_media, media_path, sensor_present
from motion import Calibration, CalibrationRun, MotionPipeline
from outputs import Outputs
from recorder import SessionManager
from sensors import MAX17048, LSM6DSV16B, MMC5983MA, make_thread_realtime

RATE_HZ = 25            # magnetometer + dashboard updates; the IMU runs in motion.py at 960 Hz
SLOW_PERIOD_S = 1.0     # fuel gauge, temperatures, camera/session/Wi-Fi status
RETRY_S = 2.0           # wait before re-initialising a sensor that failed
BATTERY_HISTORY_S = 3600
# Exactly these commands are allowed by /etc/sudoers.d/benev-dashboard:
POWEROFF = ["sudo", "-n", "systemctl", "poweroff"]
REBOOT = ["sudo", "-n", "systemctl", "reboot"]
UPDATE_START = {a: ["sudo", "-n", "systemctl", "start", "--no-block", f"benev-update@{a}.service"]
                for a in ("check", "install")}
OTA_STATE = Path("/var/lib/benev/ota/state.json")      # written by system/ota.py (root)
REBOOT_FLAG = Path("/run/benev-reboot-required")        # an update changed config.txt
WIFI_SOCKET = "/run/benev-wifi/wifi.sock"           # benev-wifi.service (runs as root)
HOTSPOT_IP = "10.42.0.1"                             # NetworkManager "shared" mode address


DEVICE_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def device_name():
    """This head mount's name: its host name, which is also its address (name.local)."""
    return socket.gethostname().lower()


def own_hosts():
    """Names the app answers to as itself; any other Host name on the hotspot is a captive-portal probe."""
    name = device_name()
    return {name, f"{name}.local", HOTSPOT_IP, "localhost", "127.0.0.1"}

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
try:
    VERSION = (HERE / "VERSION").read_text().splitlines()[0].strip()
except (OSError, IndexError):
    VERSION = "unknown"
PAGES = {"/": "record.html", "/device": "device.html", "/wifi": "wifi.html"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
SESSION_FILE_TYPES = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".csv": "text/csv; charset=utf-8",
                      ".json": "application/json"}

log = logging.getLogger("dashboard")


class Slot:
    """One sensor: opens the driver lazily, and after any error closes it and retries later."""

    def __init__(self, factory, bus):
        self.factory = factory
        self.bus = bus
        self.dev = None
        self.ident = None
        self.error = None
        self.retry_at = 0.0

    def call(self, fn):
        if self.dev is None:
            if time.monotonic() < self.retry_at:
                return None
            try:
                self.dev = self.factory()
                self.ident = self.dev.ident
                self.error = None
                log.info("%s ready on %s", self.ident, self.bus)
            except Exception as e:
                self._fail(e)
                return None
        try:
            return fn(self.dev)
        except Exception as e:
            self._fail(e)
            return None

    def _fail(self, e):
        error = f"{type(e).__name__}: {e}"
        if error != self.error:
            log.warning("%s on %s: %s", self.ident or self.factory.__name__, self.bus, error)
        self.error = error
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
        self.retry_at = time.monotonic() + RETRY_S

    def status(self):
        return {"ok": self.dev is not None, "ident": self.ident, "bus": self.bus, "error": self.error}


class Hub:
    """Latest message plus a sequence number that streaming clients wait on."""

    def __init__(self):
        self._cond = threading.Condition()
        self._seq = 0
        self._msg = None

    def publish(self, msg):
        with self._cond:
            self._seq += 1
            self._msg = msg
            self._cond.notify_all()

    def wait(self, seq, timeout):
        with self._cond:
            self._cond.wait_for(lambda: self._seq != seq, timeout)
            return self._seq, self._msg


class WifiClient:
    """Asks the root Wi-Fi manager (benev-wifi.service) over its Unix socket; status is cached."""

    def __init__(self):
        self.cached = {"available": False}
        threading.Thread(target=self._poll, daemon=True, name="wifi-status").start()

    def request(self, msg, timeout=8):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(WIFI_SOCKET)
            s.sendall(json.dumps(msg).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data or b"{}")
        if reply.get("error") and not reply.get("ok", True):
            raise RuntimeError(reply["error"])
        return reply

    def _poll(self):
        while True:
            try:
                self.cached = {"available": True, **self.request({"cmd": "status"}, timeout=4)}
            except (OSError, ValueError, RuntimeError) as e:
                self.cached = {"available": False, "error": f"Wi-Fi manager not reachable ({e})"}
            time.sleep(2)

    @property
    def hotspot(self):
        return self.cached.get("mode") == "hotspot"


class Sampler(threading.Thread):
    def __init__(self, hub, outputs, camera, motion, mag_slot, sessions, app):
        super().__init__(daemon=True, name="sampler")
        self.hub = hub
        self.outputs = outputs
        self.camera = camera
        self.motion = motion
        self.mag = mag_slot
        self.sessions = sessions
        self.app = app
        self.battery = Slot(MAX17048, "I2C-1 @ 0x36")
        self.battery_history = collections.deque(maxlen=int(BATTERY_HISTORY_S / SLOW_PERIOD_S))
        self.lock = threading.Lock()
        self.battery_latest = None   # (monotonic time, reading) of the last good fuel-gauge read

    def run(self):
        make_thread_realtime("magnetometer reader")   # shares the SPI bus with the IMU
        battery, battery_t, mag_temp, imu_temp = None, None, None, None
        outputs_rev = None
        next_tick = next_slow = time.monotonic()
        while True:
            now = time.monotonic()
            t_start = time.monotonic_ns()
            mag = self.mag.call(lambda d: d.read())
            if mag is not None:
                # Midpoint of the SET and RESET measurements; the camera changes the offset.
                t_mag = (t_start + time.monotonic_ns()) // 2
                self.motion.add_mag(t_mag, mag["field"], self.camera.state == "live")
            slow = now >= next_slow
            if slow:
                next_slow = now + SLOW_PERIOD_S
                mag_temp = self.mag.call(lambda d: d.read_temp())
                imu_temp = self.motion.temperature
                battery = self.battery.call(lambda d: d.read())
                if battery is not None:
                    self.battery_latest = (time.monotonic(), battery)
                    battery_t = time.time()
                    with self.lock:
                        self.battery_history.append([round(battery_t, 1), battery["voltage"], battery["soc"]])
            motion = self.motion.state()
            if mag is not None:
                mag["temp"] = mag_temp
                if motion and motion["mag_cal"] is not None:
                    mag["cal"] = motion["mag_cal"]
            imu = None
            if motion is not None:
                imu = {k: motion[k] for k in ("accel", "gyro", "accel_norm", "roll", "pitch")} | {"temp": imu_temp}
            msg = {
                "t": round(time.time(), 3),
                "imu": imu,
                "orientation": None if motion is None else {
                    k: motion[k] for k in ("q", "tare", "roll", "pitch", "yaw", "mag_used", "mag_ok",
                                           "accel_used", "imu_hz", "overruns", "calibrated")},
                "mag": mag,
                "battery": battery if self.battery.dev is not None else None,
                "battery_t": battery_t,
                "status": {"imu": self.motion.slot.status(), "mag": self.mag.status(), "battery": self.battery.status()},
                "session": self.sessions.status(),
            }
            run = self.app.calibration_run
            if run is not None and (not run.finished or slow):
                msg["calibration_run"] = run.status()
            # Output state only changes on request, so send it once a second and right after a change.
            if slow or self.outputs.rev != outputs_rev:
                outputs_rev = self.outputs.rev
                msg["outputs"] = self.outputs.state()
            if slow:
                msg["camera"] = self.camera.status()
                msg["calibration"] = self.motion.cal.summary()
                msg["wifi"] = self.app.wifi.cached
                msg["board"] = self.app.board.status()
                usage = shutil.disk_usage(MEDIA if MEDIA.exists() else HERE)
                msg["disk"] = {"free_bytes": usage.free, "total_bytes": usage.total}
            self.hub.publish(json.dumps(msg, separators=(",", ":")))

            next_tick += 1 / RATE_HZ
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()  # fell behind: skip ahead rather than burst

    def history(self):
        with self.lock:
            return list(self.battery_history)

    def battery_soc(self, max_age_s=60):
        """State of charge (%) from a reading at most max_age_s old, else None."""
        latest = self.battery_latest
        if latest is None or time.monotonic() - latest[0] > max_age_s:
            return None
        return latest[1].get("soc")


class App:
    """Everything the request handlers need."""

    def __init__(self):
        self.outputs = Outputs(HERE / "outputs.json")
        for item in self.outputs.state():
            if item["error"]:
                log.warning("%s (GPIO%d) unavailable: %s", item["label"], item["gpio"], item["error"])
        self.camera = CameraFeed()
        self.calibration = Calibration(HERE / "calibration.json")
        self.motion = MotionPipeline(Slot(LSM6DSV16B, "SPI0.0 (CE0, GPIO8)"), self.calibration)
        self.mag_slot = Slot(MMC5983MA, "SPI0.1 (CE1, GPIO7)")
        self.sessions = SessionManager(MEDIA, self.motion, self.camera, self.motion.slot, self.mag_slot)
        self.calibration_run = None
        self.wifi = WifiClient()
        self.hub = Hub()
        self.sampler = Sampler(self.hub, self.outputs, self.camera, self.motion, self.mag_slot, self.sessions, self)
        self.shutdown_reason = None
        self._shutdown_lock = threading.Lock()
        self.board = Board(self.outputs, HERE / "board.json", self)

    def start(self):
        self.motion.start()
        self.sampler.start()
        self.board.start()

    def stop_recordings(self):
        """Save a running recording (session, or a Device-page video). -> what was saved, or None."""
        if self.sessions.active:
            return {"session": self.sessions.stop()}
        if self.camera.status()["recording"]:
            # Stopping the encoder writes the MP4 index; without it the file would not play.
            return self.camera.command({"cmd": "record_stop"}, timeout=60)
        return None

    def recording(self):
        return bool(self.sessions.active) or bool(self.camera.status()["recording"])

    def status(self):
        """Compact device state (for apps, and the updater's safety and health checks)."""
        return {"version": VERSION, "recording": self.recording(), "session": self.sessions.status(),
                "session_busy": self.sessions.busy, "board": self.board.status(),
                "battery_soc": self.sampler.battery_soc(), "calibrated": self.calibration.done,
                "wifi_mode": self.wifi.cached.get("mode"), "name": device_name()}

    def shutdown(self, reason, reboot=False):
        """Save recordings, then power off (web button, power button, or unplugged too long)."""
        with self._shutdown_lock:
            if self.shutdown_reason:
                return None
            self.shutdown_reason = reason
        log.warning("shutting down: %s", reason)
        try:
            saved = self.stop_recordings()
        except Exception as e:
            log.error("saving the recording before shutdown failed: %s", e)
            saved = {"error": str(e)}
        # A moment's delay lets a web request answer first, so the page can show what happens.
        threading.Timer(1.0, subprocess.run, args=(REBOOT if reboot else POWEROFF,)).start()
        return saved

    def close(self):
        """Service stop (also during shutdown): save recordings, stop the camera, outputs off."""
        try:
            self.stop_recordings()
        except Exception as e:
            log.error("saving the recording on stop failed: %s", e)
        self.camera.shutdown()
        self.outputs.off()   # the board stays powered after the Pi halts: no fan or LEDs left on
        log.info("stopped")


class Handler(BaseHTTPRequestHandler):
    server_version = "Benev/2.0"

    @property
    def app(self):
        return self.server.app

    # --- GET ---------------------------------------------------------------------------------

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        if self._captive_redirect():
            return
        if path == "/api/stream":
            self._stream()
        elif path == "/api/latest":
            self._send(200, "application/json", (self.app.hub.wait(0, 0)[1] or "null").encode())
        elif path == "/api/outputs":
            self._send_json(self.app.outputs.state())
        elif path == "/api/battery-history":
            self._send_json(self.app.sampler.history())
        elif path == "/api/camera.mjpg":
            self._camera_stream()
        elif path == "/api/camera.jpg":
            self._camera_snapshot()
        elif path == "/api/media":
            usage = shutil.disk_usage(MEDIA if MEDIA.exists() else HERE)
            self._send_json({"files": list_media(), "free_bytes": usage.free, "total_bytes": usage.total})
        elif path.startswith("/media/"):
            p = media_path(unquote(path[len("/media/"):]))
            self._send_file(p, MEDIA_TYPES.get(p.suffix.lower()) if p else None)
        elif path == "/api/sessions":
            usage = shutil.disk_usage(self.app.sessions.root)
            self._send_json({"sessions": self.app.sessions.list(), "free_bytes": usage.free})
        elif path.startswith("/api/sessions/"):
            self._get_session(path[len("/api/sessions/"):], parse_qs(url.query))
        elif path == "/api/calibration":
            run = self.app.calibration_run
            self._send_json({"calibration": self.app.calibration.data,
                             "run": run.status() if run else None})
        elif path == "/api/board":
            self._send_json(self.app.board.status())
        elif path == "/api/status":
            self._send_json(self.app.status())
        elif path == "/api/device":
            self._send_json({"name": device_name(), "address": f"{device_name()}.local", "version": VERSION})
        elif path == "/api/update":
            self._send_json(self._update_state())
        elif path == "/api/wifi":
            self._send_json(self.app.wifi.cached)
        elif path in PAGES:
            self._send_static(PAGES[path])
        elif path == "/favicon.ico":
            self._send_static("icon.svg")
        elif re.fullmatch(r"/[a-z0-9_-]+\.(js|css|svg)", path):
            self._send_static(path[1:])
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def _captive_redirect(self):
        """While the hotspot is on, any other host name (phones' connectivity checks) -> /wifi."""
        if not self.app.wifi.hotspot:
            return False
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host in own_hosts() or self.server.server_address[1] != 80:
            return False
        self.send_response(302)
        self.send_header("Location", f"http://{HOTSPOT_IP}/wifi")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return True

    def _get_session(self, rest, query):
        sessions = self.app.sessions
        parts = rest.split("/")
        folder = sessions.folder(unquote(parts[0]))
        if folder is None:
            return self._send(404, "text/plain; charset=utf-8", b"no such session\n")
        sid = folder.name
        if len(parts) == 2 and parts[1] == "track":
            hz = min(60, max(1, int(query.get("hz", ["30"])[0])))
            return self._send_json(sessions.track(sid, hz))
        if len(parts) == 2 and parts[1] == "download":
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="benev-{sid}.zip"')
            self.end_headers()
            try:
                sessions.write_zip(sid, self.wfile)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            return None
        if len(parts) == 2 and parts[1] == "session.json":
            return self._send_file(folder / "session.json", "application/json")
        if len(parts) == 3 and parts[1] == "files":
            name = unquote(parts[2])
            p = folder / name
            if "/" in name or name.startswith(".") or not p.is_file():
                return self._send(404, "text/plain; charset=utf-8", b"no such file\n")
            return self._send_file(p, SESSION_FILE_TYPES.get(p.suffix.lower(), "application/octet-stream"))
        return self._send(404, "text/plain; charset=utf-8", b"not found\n")

    # --- POST --------------------------------------------------------------------------------

    def do_POST(self):
        handlers = {"/api/outputs": self._post_outputs, "/api/camera": self._post_camera,
                    "/api/media/delete": self._post_media_delete, "/api/system": self._post_system,
                    "/api/session": self._post_session, "/api/sessions/delete": self._post_session_delete,
                    "/api/calibration": self._post_calibration, "/api/wifi": self._post_wifi,
                    "/api/board": self._post_board, "/api/update": self._post_update,
                    "/api/device": self._post_device}
        handler = handlers.get(self.path.split("?", 1)[0])
        if handler is None:
            return self._send(404, "text/plain; charset=utf-8", b"not found\n")
        # Requiring a JSON content type makes browsers preflight cross-site requests,
        # which this server never approves, so other web pages cannot drive the board.
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._send(415, "text/plain; charset=utf-8", b"expected application/json\n")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 2048:
            return self._send(400, "text/plain; charset=utf-8", b"bad body length\n")
        body = None
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            handler(body)
        except KeyError as e:
            self._send(400, "text/plain; charset=utf-8", f"unknown or missing {e}\n".encode())
        except (ValueError, TypeError) as e:
            self._send(400, "text/plain; charset=utf-8", f"{e}\n".encode())
        except (CameraError, RuntimeError) as e:
            self._send(409, "text/plain; charset=utf-8", f"{e}\n".encode())
        except Exception as e:
            log.warning("POST %s failed: %s", self.path, e)
            self._send(500, "text/plain; charset=utf-8", f"{type(e).__name__}: {e}\n".encode())

    def _post_session(self, body):
        if body["action"] == "start":
            result = self.app.sessions.start()
        elif body["action"] == "stop":
            result = self.app.sessions.stop()
        else:
            raise ValueError(f"unknown action {body['action']!r}")
        self._send_json({"ok": True, "result": result, "session": self.app.sessions.status()})

    def _post_session_delete(self, body):
        self.app.sessions.delete(str(body["id"]))
        self._send_json({"ok": True})

    def _post_calibration(self, body):
        app, action = self.app, body["action"]
        if action == "start":
            if app.sessions.active:
                raise RuntimeError("stop the recording before calibrating")
            if app.calibration_run and not app.calibration_run.finished:
                raise RuntimeError("calibration already running")
            app.calibration_run = CalibrationRun(app.motion, app.camera, sensor_present())
            app.calibration_run.start()
        elif action == "cancel":
            if app.calibration_run:
                app.calibration_run.cancel()
        elif action == "tare":
            app.motion.set_tare()
        elif action == "reset_tare":
            app.motion.tare = None
        else:
            raise ValueError(f"unknown action {action!r}")
        run = app.calibration_run
        self._send_json({"ok": True, "run": run.status() if run else None})

    def _post_wifi(self, body):
        # The Wi-Fi manager validates names and passwords; its errors come back as RuntimeError.
        action = body["action"]
        if action == "connect":
            msg = {"cmd": "connect", "ssid": str(body["ssid"]), "password": str(body.get("password") or "")}
        elif action == "forget":
            msg = {"cmd": "forget", "ssid": str(body["ssid"])}
        elif action in ("scan", "hotspot", "rejoin"):
            msg = {"cmd": action}
        else:
            raise ValueError(f"unknown action {action!r}")
        try:
            reply = self.app.wifi.request(msg, timeout=45 if action == "scan" else 10)
        except OSError as e:
            raise RuntimeError(f"the Wi-Fi manager (benev-wifi.service) is not reachable: {e}") from e
        self._send_json(reply)

    def _update_state(self):
        try:
            state = json.loads(OTA_STATE.read_text())
        except FileNotFoundError:
            state = {"status": "never-checked", "message": "Not checked for updates yet"}
        except (OSError, ValueError) as e:
            state = {"status": "unknown", "message": f"cannot read the update state: {e}"}
        return {**state, "version": VERSION, "reboot_required": REBOOT_FLAG.exists()}

    def _post_update(self, body):
        action = body["action"]
        if action not in UPDATE_START:
            raise ValueError(f"unknown action {action!r}")
        if action == "install" and self.app.recording():
            raise RuntimeError("stop the recording before updating")
        r = subprocess.run(UPDATE_START[action], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError(f"could not start the updater: {(r.stderr or r.stdout).strip() or r.returncode}")
        self._send_json({"ok": True, **self._update_state()})

    def _post_device(self, body):
        """Rename the head mount; the root Wi-Fi manager applies the name (see its hostname command)."""
        name = body["name"]
        if not isinstance(name, str):
            raise ValueError("the name must be text")
        if not DEVICE_NAME_RE.fullmatch(name.strip().lower()):   # the manager checks again, as root
            raise ValueError("a device name is 1 to 63 letters, digits or hyphens, "
                             "and cannot start or end with a hyphen")
        try:
            reply = self.app.wifi.request({"cmd": "hostname", "name": name}, timeout=30)
        except OSError as e:
            raise RuntimeError(f"the Wi-Fi manager (benev-wifi.service) is not reachable: {e}") from e
        self._send_json({**reply, "name": device_name()})

    def _post_board(self, body):
        self._send_json(self.app.board.update_settings(body))

    def _post_outputs(self, body):
        self.app.outputs.set(body["name"], body["level"])
        self._send_json(self.app.outputs.state())

    def _post_camera(self, body):
        camera, action = self.app.camera, body["action"]
        if action == "still":
            result = camera.command({"cmd": "still", "raw": bool(camera.settings["raw"])}, timeout=60)
        elif action == "record_start":
            if self.app.sessions.active:
                raise RuntimeError("a recording session is running")
            result = camera.command({"cmd": "record_start"})
        elif action == "record_stop":
            if self.app.sessions.active:
                raise RuntimeError("stop the session from the Record page")
            result = camera.command({"cmd": "record_stop"}, timeout=60)
        elif action == "configure":
            width, height, fps = int(body["width"]), int(body["height"]), float(body["fps"])
            if not (64 <= width <= 1920 and 64 <= height <= 1200 and 1 <= fps <= 120):
                raise ValueError("resolution or frame rate out of range")
            result = camera.configure(width, height, fps)
        elif action == "settings":
            changes = {}
            if "raw" in body:
                changes["raw"] = bool(body["raw"])
            if "front_axis" in body:
                if body["front_axis"] not in ("+x", "-x", "+y", "-y", "+z", "-z"):
                    raise ValueError("front_axis must be one of +x -x +y -y +z -z")
                changes["front_axis"] = body["front_axis"]
            camera.update_settings(**changes)
            result = camera.settings
        elif action == "lens_calibrate":
            if self.app.recording():
                raise RuntimeError("stop the recording first")
            measured = camera.command({"cmd": "shading_capture"}, timeout=120)
            problems, warnings = lens_shading.assess(measured["stats"])
            if problems:   # nothing saved: the old correction (if any) stays
                result = {"saved": False, "problems": problems, "stats": measured["stats"],
                          "grids": measured.get("grids")}
            else:
                camera.set_lens({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "stats": measured["stats"],
                                 "warnings": warnings, "alsc": measured["alsc"]})
                edge = None
                try:   # what is still dark with the correction on: the map for edge brightening
                    em = camera.command({"cmd": "edge_map"}, timeout=120)
                    camera.set_edge_gain(em["grid"])
                    edge = {"max_gain": round(em["max_gain"], 1), "boosted_pct": em["boosted_pct"]}
                except (CameraError, OSError, KeyError, ValueError) as e:
                    warnings.append(f"The map for edge brightening could not be measured ({e}); "
                                    "the lens correction itself is saved.")
                result = {"saved": True, "warnings": warnings, "stats": measured["stats"],
                          "grids": measured.get("grids"), "edge": edge}
        elif action == "brightness":   # live, also while recording: nothing restarts
            result = {"ev": camera.set_brightness(body["ev"])}
        elif action == "edge_boost":   # live; the gain map comes from the lens calibration
            if self.app.recording():       # building the maps takes CPU; keep a recording undisturbed
                raise RuntimeError("stop the recording before changing edge brightening")
            result = {"strength": camera.set_edge_boost(body["strength"])}
        elif action in ("image", "image_reset"):
            if self.app.recording():
                raise RuntimeError("stop the recording first")
            camera.set_image(None if action == "image_reset" else {k: v for k, v in body.items() if k != "action"})
            result = camera.image_settings()
        elif action == "lens_reset":
            if self.app.recording():
                raise RuntimeError("stop the recording first")
            camera.set_lens(None)
            result = {"saved": False, "removed": True}
        else:
            raise ValueError(f"unknown action {action!r}")
        self._send_json({"ok": True, "result": result, "status": camera.status()})

    def _post_system(self, body):
        if body["action"] not in ("shutdown", "reboot"):
            raise ValueError(f"unknown action {body['action']!r}")
        reboot = body["action"] == "reboot"
        saved = self.app.shutdown(f"{'Restart' if reboot else 'Shut down'} button (from {self.client_address[0]})",
                                  reboot=reboot)
        self._send_json({"ok": True, "saved_recording": saved})

    def _post_media_delete(self, body):
        path = media_path(body["name"])
        if path is None:
            return self._send(404, "text/plain; charset=utf-8", b"no such file\n")
        recording = self.app.camera.status()["recording"]
        if recording and recording["file"] == path.name:
            raise CameraError("that file is still being recorded")
        path.unlink()
        self._send_json({"ok": True, "files": list_media()})

    # --- helpers -----------------------------------------------------------------------------

    def _send_static(self, name):
        p = STATIC / name
        if not p.is_file():
            return self._send(404, "text/plain; charset=utf-8", b"not found\n")
        self._send(200, TYPES.get(p.suffix, "application/octet-stream"), p.read_bytes())

    def _send_file(self, path, ctype):
        """A file with single byte-range support, so videos can seek."""
        if path is None or ctype is None or not path.is_file():
            return self._send(404, "text/plain; charset=utf-8", b"no such file\n")
        size = path.stat().st_size
        start, end = 0, size - 1
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if match and size and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            else:   # suffix range: the last N bytes
                start = max(0, size - int(match.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return None
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(left, 256 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        return None

    def _send_json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seq = 0
        try:
            while True:
                new_seq, msg = self.app.hub.wait(seq, timeout=5)
                if new_seq == seq:
                    self.wfile.write(b": keepalive\n\n")  # sampler stalled; keep the socket alive
                else:
                    # A slow client skips straight to the newest message instead of queueing.
                    seq = new_seq
                    self.wfile.write(b"data: " + msg.encode() + b"\n\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _camera_stream(self):
        camera = self.app.camera
        if not sensor_present():
            return self._send(503, "text/plain; charset=utf-8", b"AR0234 driver not loaded\n")
        camera.acquire()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = 0
            while True:
                new_seq, jpeg, running = camera.wait_frame(seq, timeout=10)
                if not running:
                    break   # worker stopped or failed; the page shows the reason from the status
                if new_seq == seq:
                    continue   # still starting
                seq = new_seq
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                 % len(jpeg) + jpeg + b"\r\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            camera.release()
        return None

    def _camera_snapshot(self):
        camera = self.app.camera
        if not sensor_present():
            return self._send(503, "text/plain; charset=utf-8", b"AR0234 driver not loaded\n")
        camera.acquire()
        try:
            seq, jpeg, running = camera.wait_frame(0, timeout=10)
        finally:
            camera.release()
        if jpeg is None:
            return self._send(503, "text/plain; charset=utf-8",
                              f"no frame: {camera.status()['error'] or 'camera still starting'}\n".encode())
        return self._send(200, "image/jpeg", jpeg)

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, action="append", help="may be given more than once (default 8080)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = App()
    app.start()
    servers = []
    for port in args.port or [8080]:
        try:
            httpd = Server((args.host, port), Handler)
        except OSError as e:   # e.g. port 80 taken, or no CAP_NET_BIND_SERVICE: keep the others
            log.error("cannot listen on port %d: %s", port, e)
            continue
        httpd.app = app
        servers.append(httpd)
        log.info("serving on http://%s:%d", args.host, port)
    if not servers:
        raise SystemExit("no port to listen on")
    for httpd in servers[1:]:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    def on_term(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_term)   # systemd stop (KillMode=mixed: only this process)
    try:
        servers[0].serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        app.close()


if __name__ == "__main__":
    main()
