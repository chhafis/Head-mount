"""Camera for the dashboard: runs camera_worker.py while the camera is in use and talks to it.

The worker is started for the first viewer or command and kept while someone watches, a
recording runs or a command is pending; IDLE_STOP_S after that it is told to exit (closing
its stdin), which also finishes any recording cleanly.
"""
import itertools
import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import edge_boost
import image_tuning
import lens_shading

HERE = Path(__file__).resolve().parent
MEDIA = HERE / "media"
SETTINGS_FILE = HERE / "camera.json"
DEFAULT_SETTINGS = {"width": 1920, "height": 1080, "fps": 30, "raw": False, "front_axis": "+x", "rotate180": False,
                    "bitrate_mbps": None, "image": {}}
IDLE_STOP_S = 5.0
START_TIMEOUT_S = 20.0
# The worker's libcamera uses a copy of the sensor's tuning file (camera-tuning.json) instead of
# the stock one: the stock file plus the image settings (image_tuning.py: exposure limit,
# contrast enhancement) plus the lens shading calibration (lens_shading.py), if there is one.
LENS_FILE = HERE / "lens-shading.json"
TUNING_BASE = Path("/usr/local/share/libcamera/ipa/rpi/vc4/ar0234.json")
TUNING_FILE = HERE / "camera-tuning.json"
# Kurokesu's libcamera 0.7.2 (with AR0234 support) is installed in /usr/local next to the
# system's 0.5; only the camera worker is pointed at its Python bindings.
LIBCAMERA_PYTHONPATH = "/usr/local/lib/python3/dist-packages"
MEDIA_TYPES = {".jpg": "image/jpeg", ".dng": "image/x-adobe-dng",
               ".mp4": "video/mp4", ".mkv": "video/x-matroska"}

log = logging.getLogger("dashboard")


def write_tuning(image, rotate180=False):
    """Build camera-tuning.json for the image settings `image` (a full settings dict) and the
    lens calibration, if any. -> its path."""
    tuning = json.loads(TUNING_BASE.read_text())
    image_tuning.apply_to_tuning(tuning, image)
    try:
        lens = json.loads(LENS_FILE.read_text())
    except FileNotFoundError:
        lens = None
    lens = lens_shading.oriented(lens, rotate180)
    if lens:
        for algo in tuning["algorithms"]:
            if "rpi.alsc" in algo:
                algo["rpi.alsc"] = lens["alsc"]
                break
        else:
            tuning["algorithms"].append({"rpi.alsc": lens["alsc"]})
    tmp = TUNING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tuning, indent=1))
    os.replace(tmp, TUNING_FILE)
    return TUNING_FILE


def lens_status():
    try:
        lens = json.loads(LENS_FILE.read_text())
    except (OSError, ValueError):
        return {"calibrated": False}
    return {"calibrated": True, "time": lens.get("time"), "stats": lens.get("stats"), "warnings": lens.get("warnings", []),
            "edge_map": bool(lens.get("edge_gain")),
            "edge_max_gain": max(max(row) for row in lens["edge_gain"]) if lens.get("edge_gain") else None}


def sensor_present():
    for node in Path("/sys/class/video4linux").glob("v4l-subdev*"):
        try:
            if (node / "name").read_text().startswith("ar0234"):
                return True
        except OSError:
            pass
    return False


class CameraError(Exception):
    pass


class CameraFeed:
    def __init__(self):
        self.cond = threading.Condition()
        self.proc = None
        self.log_thread = None
        self.seq = 0
        self.jpeg = None
        self.meta = None          # latest status from the worker
        self.ready = False
        self.replies = {}
        self.ids = itertools.count(1)
        self.users = 0            # viewers + commands in flight
        self.idle_since = time.monotonic()
        self.state = "off"        # off | starting | live | error
        self.error = None
        self.stopping = None      # the worker we told to exit (it may still send a few frames)
        self.closed = False       # set by shutdown(): the service is stopping
        self.lens = lens_status()
        try:
            self.settings = {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text())}
        except (OSError, ValueError):
            self.settings = dict(DEFAULT_SETTINGS)
        threading.Thread(target=self._reaper, daemon=True, name="camera-reaper").start()

    # --- users

    def acquire(self):
        with self.cond:
            self.users += 1
            if self.proc is None and not self.closed:
                self._start()

    def release(self):
        with self.cond:
            self.users -= 1
            if self.users == 0:
                self.idle_since = time.monotonic()

    def wait_frame(self, seq, timeout):
        """Newest (seq, jpeg, running) once a frame newer than seq exists or the worker stopped."""
        with self.cond:
            # seq keeps counting across worker sessions while jpeg is cleared between them,
            # so a fresh viewer must wait for an actual frame, not just a different number.
            self.cond.wait_for(lambda: (self.seq != seq and self.jpeg is not None) or self.proc is None, timeout)
            return self.seq, self.jpeg, self.proc is not None

    def command(self, cmd, timeout=30.0):
        """Send one command to the worker (starting it if needed) and return its result."""
        if not sensor_present():
            raise CameraError("AR0234 driver not loaded")
        self.acquire()
        try:
            with self.cond:
                if not self.cond.wait_for(lambda: self.ready or self.proc is None, START_TIMEOUT_S) or not self.ready:
                    raise CameraError(self.error or "camera did not start")
                cid = next(self.ids)
                self.proc.stdin.write((json.dumps({**cmd, "id": cid}) + "\n").encode())
                self.proc.stdin.flush()
                if not self.cond.wait_for(lambda: cid in self.replies or self.proc is None, timeout):
                    raise CameraError(f"{cmd['cmd']}: no answer within {timeout:.0f} s")
                reply = self.replies.pop(cid, None)
            if reply is None:
                raise CameraError(self.error or "camera stopped")
            if not reply.get("ok"):
                raise CameraError(reply.get("error", "failed"))
            return reply.get("result")
        finally:
            self.release()

    def configure(self, width, height, fps):
        result = self.command({"cmd": "configure", "width": width, "height": height, "fps": fps})
        self.update_settings(width=result["width"], height=result["height"], fps=result["fps"])
        return result

    def update_settings(self, **changes):
        with self.cond:
            self.settings.update(changes)
            SETTINGS_FILE.write_text(json.dumps(self.settings))

    def image_settings(self):
        """The image settings in effect: the defaults plus what is saved in camera.json."""
        return image_tuning.merged(self.settings.get("image"))

    def set_image(self, changes):
        """Save changed image settings (None = back to the defaults) and restart the worker."""
        if changes is None:
            saved = {}
        else:
            saved = {**(self.settings.get("image") or {}), **image_tuning.validate(changes)}
        self.update_settings(image=saved)
        self.restart()

    def set_brightness(self, ev):
        """Exposure compensation in stops (-2..+2). Saved, and applied at once if the camera runs."""
        ev = image_tuning.validate({"ev": ev})["ev"]
        self.update_settings(image={**(self.settings.get("image") or {}), "ev": ev})
        with self.cond:
            running = self.ready and self.proc is not None and self.proc is not self.stopping
        if running:   # otherwise the saved value is used when the camera next starts
            try:
                self.command({"cmd": "live", "controls": {"ExposureValue": ev}}, timeout=10)
            except CameraError as e:
                log.warning("brightness not applied live: %s", e)
        return ev

    def set_edge_boost(self, strength):
        """Edge brightening strength 0..1. Saved, and applied at once if the camera runs."""
        strength = image_tuning.validate({"edge_boost": strength})["edge_boost"]
        self.update_settings(image={**(self.settings.get("image") or {}), "edge_boost": strength})
        with self.cond:
            running = self.ready and self.proc is not None and self.proc is not self.stopping
        if running:
            try:
                self.command({"cmd": "live", "controls": {"EdgeBoost": strength}}, timeout=20)
            except CameraError as e:
                log.warning("edge brightening not applied live: %s", e)
        return strength

    def set_edge_gain(self, grid):
        """Store the edge gain map next to the lens calibration and make the running camera use it."""
        lens = json.loads(LENS_FILE.read_text())
        if bool(lens.get("rotated")) != bool(self.settings.get("rotate180")):   # store it as the lens file is oriented
            grid = [list(reversed(row)) for row in reversed(grid)]
        lens["edge_gain"] = grid
        tmp = LENS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(lens, indent=1))
        os.replace(tmp, LENS_FILE)
        self.lens = lens_status()
        self.command({"cmd": "edge_reload"}, timeout=30)

    def set_lens(self, data):
        """Save (or with None remove) the lens calibration and restart the worker with it."""
        if data is None:
            LENS_FILE.unlink(missing_ok=True)
        else:
            data = dict(data, rotated=bool(self.settings.get("rotate180")))   # the orientation it was measured in
            tmp = LENS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            os.replace(tmp, LENS_FILE)
        self.lens = lens_status()
        self.restart()

    def set_rotation(self, rotated):
        """Turn the picture by 180 degrees (camera mounted upside down). Saved; the worker restarts with it."""
        rotated = bool(rotated)
        if rotated == bool(self.settings.get("rotate180")):
            return
        self.update_settings(rotate180=rotated)
        self.restart()

    def restart(self):
        """End the worker; it comes straight back if someone is using the camera (new tuning)."""
        with self.cond:
            proc = self.proc
            if proc is None or proc is self.stopping or (self.meta and self.meta.get("recording")):
                return
            self.state, self.ready, self.stopping = "off", False, proc
            try:
                proc.stdin.close()
            except OSError:
                proc.terminate()
            threading.Timer(10, lambda p=proc: p.poll() is None and p.terminate()).start()

    def shutdown(self, timeout=20):
        """Service stop: end the worker and wait for it; it finishes a running recording first."""
        with self.cond:
            self.closed = True   # no restarts from here on
            proc = self.proc
            if proc is None:
                return
            self.state, self.ready, self.stopping = "off", False, proc
            try:
                proc.stdin.close()   # the worker exits cleanly when its input ends
            except OSError:
                pass
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            log.warning("camera worker did not exit within %d s; terminating it", timeout)
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def status(self):
        with self.cond:
            meta = self.meta if self.state == "live" else None
            return {"present": sensor_present(), "state": self.state, "error": self.error,
                    "viewers": self.users, "settings": dict(self.settings), "lens": self.lens,
                    "image": self.image_settings(), "image_defaults": image_tuning.DEFAULTS,
                    "edge_available": edge_boost.can_build(),
                    "camera": meta, "recording": meta and meta.get("recording")}

    # --- worker process

    def _start(self):
        self.state, self.error, self.meta, self.ready = "starting", None, None, False
        if not (Path(LIBCAMERA_PYTHONPATH) / "libcamera").is_dir():
            # Seen on a Pi whose installer was interrupted during the libcamera build.
            self.state = "error"
            self.error = ("libcamera is not installed (the installer did not finish): "
                          "run  sudo bash ~/benev-install.sh  on the Pi")
            return
        s = self.settings
        env = {**os.environ, "PYTHONPATH": LIBCAMERA_PYTHONPATH, "LIBCAMERA_LOG_LEVELS": "*:WARN"}
        image = self.image_settings()
        try:
            tuning = write_tuning(image, bool(s.get("rotate180")))
        except (OSError, ValueError, KeyError) as e:
            log.warning("image tuning and lens calibration not used: %s", e)
            tuning = None
        ctl = image_tuning.controls(image)
        if tuning:   # not LIBCAMERA_RPI_TUNING_FILE: Picamera2 clears that; the worker passes it on
            env["BENEV_TUNING_FILE"] = str(tuning)
        else:        # stock tuning has no custom exposure mode: its short mode is the nearest
            ctl["AeExposureMode"] = "Short"
        env["BENEV_IMAGE_CONTROLS"] = json.dumps(ctl)
        env["BENEV_EDGE_BOOST"] = str(image["edge_boost"])
        env["BENEV_ROTATE180"] = "1" if s.get("rotate180") else "0"
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "camera_worker.py"), str(s["width"]), str(s["height"]), str(s["fps"])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.log_thread = threading.Thread(target=self._read_log, args=(self.proc,), daemon=True)
        self.log_thread.start()
        threading.Thread(target=self._read_records, args=(self.proc,), daemon=True).start()

    def _read_log(self, proc):
        for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            if text:
                log.info("%s", text)
                if text.startswith("camera_worker: error:"):
                    self.error = text.split("error:", 1)[1].strip()

    def _read_records(self, proc):
        out = proc.stdout
        try:
            while True:
                head = out.read(8)
                if len(head) < 8:
                    break
                meta_len, jpeg_len = struct.unpack("<II", head)
                meta = json.loads(out.read(meta_len))
                jpeg = out.read(jpeg_len) if jpeg_len else None
                with self.cond:
                    if meta.get("type") == "reply":
                        self.replies[meta.get("id")] = meta
                    elif proc is not self.stopping:
                        self.meta = meta
                        self.ready = True
                        self.state = "live"
                        if jpeg:
                            self.seq += 1
                            self.jpeg = jpeg
                    self.cond.notify_all()
        except (OSError, ValueError) as e:
            self.error = f"bad record from camera worker: {e}"
        finally:
            rc = proc.wait()
            self.log_thread.join(timeout=2)   # let the worker's last error line arrive
            with self.cond:
                if self.proc is proc:
                    self.proc, self.ready, self.jpeg = None, False, None
                    if proc is self.stopping:
                        self.state = "off"
                        if self.users > 0 and not self.closed:
                            self._start()   # someone arrived while we were stopping
                    else:
                        self.state = "error"
                        self.error = self.error or f"camera worker exited with status {rc}"
                self.cond.notify_all()

    def _reaper(self):
        while True:
            time.sleep(1)
            with self.cond:
                recording = bool(self.meta and self.meta.get("recording"))
                if (self.proc is not None and self.proc is not self.stopping and self.users == 0
                        and not recording and time.monotonic() - self.idle_since > IDLE_STOP_S):
                    self.state, self.ready, self.stopping = "off", False, self.proc
                    try:
                        self.proc.stdin.close()   # the worker exits cleanly when its input ends
                    except OSError:
                        self.proc.terminate()
                    threading.Timer(10, lambda p=self.proc: p.poll() is None and p.terminate()).start()


def list_media():
    MEDIA.mkdir(exist_ok=True)
    files = []
    for p in MEDIA.iterdir():
        if p.is_file() and p.suffix.lower() in MEDIA_TYPES:
            st = p.stat()
            files.append({"name": p.name, "bytes": st.st_size, "mtime": round(st.st_mtime)})
    return sorted(files, key=lambda f: f["mtime"], reverse=True)


def media_path(name):
    """Resolve a media file name from a request; None unless it names an existing media file."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    path = MEDIA / name
    return path if path.suffix.lower() in MEDIA_TYPES and path.is_file() else None
