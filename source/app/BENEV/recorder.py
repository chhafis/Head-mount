"""Recording sessions: video, IMU, orientation and magnetometer captured together.

Every session is a folder under media/sessions/<id>/:
  session.json      metadata: start time, camera and IMU settings, counts, rates, gaps
  video.mp4|.mkv    the video (MJPEG .mkv only at 1920x1200)
  frames.csv        frame,t_ns,pts_us,exposure_us,analog_gain,digital_gain,colour_temp_k
                    (pts_us: the frame's presentation time in the video file)
  imu.csv           t_ns,ax_g,ay_g,az_g,gx_dps,gy_dps,gz_dps        (raw, IMU axes, ~952 Hz;
                    gyro bias not removed, it is in calibration.json)
  orientation.csv   t_ns,qw,qx,qy,qz,roll_deg,pitch_deg,yaw_deg      (fused, same rate)
  mag.csv           t_ns,mx_uT,my_uT,mz_uT,mx_cal_uT,my_cal_uT,mz_cal_uT  (~25 Hz; raw sensor axes,
                    and calibrated in IMU axes: equal to raw while there is no calibration)
  calibration.json  the calibration in effect when recording started
All t_ns are time.monotonic_ns() (CLOCK_MONOTONIC), the clock libcamera stamps frames with.
"""
import csv
import json
import logging
import math
import shutil
import threading
import time
import zipfile
from pathlib import Path

log = logging.getLogger("dashboard")

FORMAT_VERSION = 1


class SessionLog:
    """Open CSV writers for one session; written from the motion and sampler threads."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.imu_f = open(self.folder / "imu.csv", "w", buffering=1 << 20)
        self.ori_f = open(self.folder / "orientation.csv", "w", buffering=1 << 20)
        self.mag_f = open(self.folder / "mag.csv", "w", buffering=1 << 16)
        self.imu_f.write("t_ns,ax_g,ay_g,az_g,gx_dps,gy_dps,gz_dps\n")
        self.ori_f.write("t_ns,qw,qx,qy,qz,roll_deg,pitch_deg,yaw_deg\n")
        self.mag_f.write("t_ns,mx_uT,my_uT,mz_uT,mx_cal_uT,my_cal_uT,mz_cal_uT\n")
        self.imu_n = self.mag_n = 0
        self.imu_first = self.imu_last = None
        self.max_gap_ns = 0
        self.closed = False

    def imu(self, t, a, g, q):
        if self.closed:
            return
        if self.imu_last is not None:
            self.max_gap_ns = max(self.max_gap_ns, t - self.imu_last)
        else:
            self.imu_first = t
        self.imu_last = t
        self.imu_n += 1
        self.imu_f.write(f"{t},{a[0]:.5f},{a[1]:.5f},{a[2]:.5f},{g[0]:.4f},{g[1]:.4f},{g[2]:.4f}\n")
        q0, q1, q2, q3 = q
        roll = math.degrees(math.atan2(2 * (q0 * q1 + q2 * q3), 1 - 2 * (q1 * q1 + q2 * q2)))
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (q0 * q2 - q3 * q1)))))
        yaw = math.degrees(math.atan2(2 * (q0 * q3 + q1 * q2), 1 - 2 * (q2 * q2 + q3 * q3)))
        self.ori_f.write(f"{t},{q0:.6f},{q1:.6f},{q2:.6f},{q3:.6f},{roll:.3f},{pitch:.3f},{yaw:.3f}\n")

    def mag(self, t, raw, cal):
        if self.closed:
            return
        self.mag_n += 1
        self.mag_f.write(f"{t},{raw[0]:.3f},{raw[1]:.3f},{raw[2]:.3f},{cal[0]:.3f},{cal[1]:.3f},{cal[2]:.3f}\n")

    def close(self):
        self.closed = True
        for f in (self.imu_f, self.ori_f, self.mag_f):
            f.close()


class SessionManager:
    def __init__(self, media_dir, motion, camera, imu_slot, mag_slot):
        self.root = Path(media_dir) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.motion = motion
        self.camera = camera
        self.imu_slot = imu_slot
        self.mag_slot = mag_slot
        self.lock = threading.Lock()
        self.active = None   # {"id", "folder", "log", "t0", "meta"}
        self.busy = None     # "starting" | "stopping" while start() / stop() run

    def status(self):
        a = self.active
        if a is None:
            return None
        lg = a["log"]
        return {"id": a["id"], "seconds": round(time.monotonic() - a["t0"], 1),
                "imu_samples": lg.imu_n, "mag_samples": lg.mag_n,
                "video": (self.camera.status().get("recording") or {}).get("file")}

    def start(self):
        with self.lock:
            if self.active:
                raise RuntimeError("already recording")
            self.busy = "starting"
            try:
                return self._start()
            finally:
                self.busy = None

    def _start(self):
        """Called with the lock held."""
        if shutil.disk_usage(self.root).free < 300e6:
            raise RuntimeError("less than 300 MB free on the SD card")
        # Hold the camera for the whole session and wait until it streams, so the sensor
        # logs start just before the first frame instead of during the camera's start-up.
        self.camera.acquire()
        try:
            deadline = time.monotonic() + 25
            while self.camera.state != "live":
                if time.monotonic() > deadline or self.camera.state == "error":
                    raise RuntimeError(f"camera did not start: {self.camera.error or 'timeout'}")
                time.sleep(0.1)
            return self._start_logging()
        except Exception:
            self.camera.release()
            raise

    def _start_logging(self):
        """Called with the lock held and the camera streaming."""
        sid = time.strftime("%Y%m%d-%H%M%S")
        folder = self.root / sid
        folder.mkdir()
        meta = {
            "format_version": FORMAT_VERSION,
            "id": sid,
            "start_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "clock": "all t_ns are CLOCK_MONOTONIC nanoseconds (time.monotonic_ns, libcamera SensorTimestamp)",
            "start_monotonic_ns": time.monotonic_ns(),
            "start_unix_ns": time.time_ns(),
            "imu": {"device": self.imu_slot.ident, **getattr(self.imu_slot.dev, "config", {})},
            "magnetometer": {"device": self.mag_slot.ident, "method": "SET/RESET differenced one-shot, ~25 Hz"},
            "orientation": "Mahony filter; earth frame z up, x magnetic north; q rotates IMU frame into earth frame",
            "calibration": self.motion.cal.summary(),
        }
        if self.motion.cal.data:
            (folder / "calibration.json").write_text(json.dumps(self.motion.cal.data, indent=2))
        lg = SessionLog(folder)
        self.motion.log = lg               # sensors first, so they cover the first frames
        try:
            video = self.camera.command({"cmd": "record_start", "dir": str(folder)})
        except Exception:
            self.motion.log = None
            lg.close()
            shutil.rmtree(folder, ignore_errors=True)
            raise
        meta["camera"] = (self.camera.status().get("camera") or {}).get("config")
        meta["camera_front_axis"] = self.camera.settings.get("front_axis", "+x")   # which IMU axis the lens looks along
        meta["video_file"] = video["file"]
        self.active = {"id": sid, "folder": folder, "log": lg, "t0": time.monotonic(), "meta": meta,
                       "overruns0": getattr(self.imu_slot.dev, "overruns", 0),
                       "errors0": dict(getattr(self.imu_slot.dev, "errors", {}))}
        self._write_meta(folder, meta)
        log.info("session %s started", sid)
        return {"id": sid, "video": video}

    def stop(self):
        with self.lock:
            if self.active is None:
                raise RuntimeError("not recording")
            self.busy = "stopping"
            try:
                return self._stop()
            finally:
                self.busy = None

    def _stop(self):
        """Called with the lock held."""
        a = self.active
        try:
            video = self.camera.command({"cmd": "record_stop"}, timeout=60)
        except Exception as e:
            video = {"error": str(e)}
        self.motion.log = None             # sensors last, so they cover the last frames
        lg = a["log"]
        lg.close()
        self.active = None
        self.camera.release()
        meta = a["meta"]
        meta["stop_monotonic_ns"] = time.monotonic_ns()
        meta["duration_s"] = round((meta["stop_monotonic_ns"] - meta["start_monotonic_ns"]) / 1e9, 2)
        meta["video"] = video
        frames = _count_lines(a["folder"] / "frames.csv")
        span = (lg.imu_last - lg.imu_first) / 1e9 if lg.imu_n > 1 else 0
        meta["counts"] = {
            "frames": frames, "imu_samples": lg.imu_n, "mag_samples": lg.mag_n,
            "imu_rate_hz": round((lg.imu_n - 1) / span, 2) if span else None,
            "imu_max_gap_ms": round(lg.max_gap_ns / 1e6, 1),
            "imu_fifo_overruns": getattr(self.imu_slot.dev, "overruns", 0) - a["overruns0"],
            "imu_fifo_errors": {k: v - a["errors0"].get(k, 0)
                                for k, v in getattr(self.imu_slot.dev, "errors", {}).items()},
        }
        self._write_meta(a["folder"], meta)
        log.info("session %s saved: %.1f s, %s frames, %s IMU samples", a["id"], meta["duration_s"], frames, lg.imu_n)
        return {"id": a["id"], **meta["counts"], "duration_s": meta["duration_s"]}

    @staticmethod
    def _write_meta(folder, meta):
        tmp = folder / "session.json.tmp"
        tmp.write_text(json.dumps(meta, indent=2))
        tmp.replace(folder / "session.json")

    # --- browsing ----------------------------------------------------------------------------

    def folder(self, sid):
        if not sid or "/" in sid or "\\" in sid or sid.startswith("."):
            return None
        f = self.root / sid
        return f if (f / "session.json").is_file() else None

    def list(self):
        out = []
        for f in sorted(self.root.iterdir(), reverse=True):
            try:
                meta = json.loads((f / "session.json").read_text())
            except (OSError, ValueError):
                continue
            size = sum(p.stat().st_size for p in f.iterdir() if p.is_file())
            out.append({"id": meta["id"], "start_local": meta.get("start_local"),
                        "duration_s": meta.get("duration_s"), "bytes": size,
                        "video_file": meta.get("video_file"), "camera": meta.get("camera"),
                        "counts": meta.get("counts"),
                        "recording": self.active is not None and self.active["id"] == meta["id"]})
        return out

    def delete(self, sid):
        if self.active and self.active["id"] == sid:
            raise RuntimeError("that session is still recording")
        f = self.folder(sid)
        if f is None:
            raise KeyError(sid)
        shutil.rmtree(f)

    def track(self, sid, hz=30):
        """Orientation and IMU, decimated, aligned to the video: t is seconds after the first frame."""
        f = self.folder(sid)
        if f is None:
            raise KeyError(sid)
        t0 = None
        with open(f / "frames.csv") as fh:
            rows = csv.reader(fh)
            next(rows, None)
            first = next(rows, None)
            if first:
                t0 = int(first[1])
        if t0 is None:
            meta = json.loads((f / "session.json").read_text())
            t0 = meta["start_monotonic_ns"]
        step = 1e9 / hz
        out_t, out_q, out_a, out_g = [], [], [], []
        next_t = None
        # One row per IMU sample in both files. At ~952 Hz most rows are skipped, so only the
        # timestamp of each row is parsed until one is kept.
        with open(f / "orientation.csv") as fo, open(f / "imu.csv") as fi:
            next(fo, None)
            next(fi, None)
            for o, i in zip(fo, fi):
                try:
                    t = int(o[:o.index(",")])
                except ValueError:
                    break   # a partly written last line
                if next_t is not None and t < next_t:
                    continue
                next_t = t + step
                ov, iv = o.split(","), i.split(",")
                if len(ov) < 5 or len(iv) < 7:
                    break
                out_t.append(round((t - t0) / 1e9, 4))
                out_q.append([float(v) for v in ov[1:5]])
                out_a.append([float(v) for v in iv[1:4]])
                out_g.append([float(v) for v in iv[4:7]])
        return {"t": out_t, "q": out_q, "accel": out_a, "gyro": out_g, "hz": hz}

    def write_zip(self, sid, wfile):
        """Stream the session folder as an (uncompressed) zip to a socket file."""
        f = self.folder(sid)
        if f is None:
            raise KeyError(sid)
        with zipfile.ZipFile(_Unseekable(wfile), "w", zipfile.ZIP_STORED) as z:
            for p in sorted(f.iterdir()):
                if p.is_file():
                    z.write(p, f"{sid}/{p.name}")


class _Unseekable:
    """zipfile writes data descriptors instead of seeking back when tell()/seek() fail."""

    def __init__(self, raw):
        self.raw = raw

    def write(self, b):
        self.raw.write(b)
        return len(b)

    def flush(self):
        pass


def _count_lines(path):
    try:
        with open(path) as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0
