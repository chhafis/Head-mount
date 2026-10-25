"""Motion pipeline: IMU FIFO -> CLOCK_MONOTONIC timestamps -> calibration -> orientation.

All timestamps are time.monotonic_ns(), the same clock libcamera stamps video frames with,
so IMU, magnetometer and video line up directly.

Orientation is a Mahony filter (earth frame: z up, x towards magnetic north). The
magnetometer only corrects heading, and only while the field looks like the calibrated one
(strength and angle to gravity), so currents near the sensor cannot tilt the estimate.
"""
import collections
import json
import logging
import math
import threading
import time
from pathlib import Path

import numpy as np

from sensors import make_thread_realtime

log = logging.getLogger("dashboard")

DRAIN_HZ = 50                     # FIFO reads per second; at 960 Hz the FIFO holds ~125 ms
DEG = math.pi / 180


class ClockMap:
    """Maps the IMU's timestamp counter onto time.monotonic_ns().

    The counter runs on the IMU's own oscillator, so its period is learned from (counter,
    Pi time) pairs taken right after each FIFO read. Read latency only ever makes the Pi time
    later, so the lower envelope of (Pi time - scaled counter) is the offset.
    """

    def __init__(self, ns_per_count, lag_counts):
        self.scale = ns_per_count
        self.lag_counts = lag_counts   # newest sample handed out is at least one sample old
        self.pairs = collections.deque()
        self.offset = None
        self.next_fit = 0

    def add(self, count, host_ns):
        self.pairs.append((count, host_ns))
        while host_ns - self.pairs[0][1] > 120e9:
            self.pairs.popleft()
        if host_ns >= self.next_fit:
            self.next_fit = host_ns + 1e9
            self._fit()

    def _fit(self):
        pairs = list(self.pairs)
        span = pairs[-1][1] - pairs[0][1]
        if span > 10e9:
            n = max(5, len(pairs) // 10)
            c1, h1 = min(pairs[:n], key=lambda p: p[1] - self.scale * p[0])
            c2, h2 = min(pairs[-n:], key=lambda p: p[1] - self.scale * p[0])
            scale = (h2 - h1) / (c2 - c1)
            if abs(scale / self.scale - 1) < 0.02:
                self.scale = scale
        recent = [p for p in pairs if pairs[-1][1] - p[1] < 2e9]
        self.offset = min(h - self.scale * c for c, h in recent) - self.lag_counts * self.scale

    def to_ns(self, count):
        return int(self.scale * count + self.offset)


class Mahony:
    def __init__(self):
        self.q = [1.0, 0.0, 0.0, 0.0]
        self.bias = [0.0, 0.0, 0.0]     # slow gyro-bias tracking (rad/s) on top of the calibration
        self.age = 0.0                  # seconds since start; converge fast at first
        self.mag_used = False
        self.accel_used = False

    def update(self, gyro, accel, dt, mag=None, mag_ref=None):
        """gyro rad/s (calibrated), accel in g, mag unit vector in the IMU frame or None."""
        q0, q1, q2, q3 = self.q
        self.age += dt
        fast = self.age < 3
        kp_acc = 5.0 if fast else 0.6
        kp_mag = 2.0 if fast else 0.25
        ki = 0.0 if fast else 0.01
        gx, gy, gz = gyro
        ex = ey = ez = 0.0

        # Estimated "up" in the body frame.
        vx = 2 * (q1 * q3 - q0 * q2)
        vy = 2 * (q0 * q1 + q2 * q3)
        vz = q0 * q0 - q1 * q1 - q2 * q2 + q3 * q3

        ax, ay, az = accel
        an = math.sqrt(ax * ax + ay * ay + az * az)
        # Trust the accelerometer for tilt only when it mostly measures gravity.
        weight = max(0.0, 1 - abs(an - 1) / 0.2) if an > 0 else 0.0
        self.accel_used = weight > 0
        if weight:
            ax, ay, az = ax / an, ay / an, az / an
            ex += weight * kp_acc * (ay * vz - az * vy)
            ey += weight * kp_acc * (az * vx - ax * vz)
            ez += weight * kp_acc * (ax * vy - ay * vx)

        self.mag_used = False
        if mag is not None:
            mx, my, mz = mag
            # Gate: the field must look like the calibrated one (angle to gravity).
            dip_ok = mag_ref is None or abs(math.acos(max(-1, min(1, mx * vx + my * vy + mz * vz))) - mag_ref) < 12 * DEG
            if dip_ok:
                q0q0, q0q1, q0q2, q0q3 = q0 * q0, q0 * q1, q0 * q2, q0 * q3
                q1q1, q1q2, q1q3 = q1 * q1, q1 * q2, q1 * q3
                q2q2, q2q3, q3q3 = q2 * q2, q2 * q3, q3 * q3
                hx = 2 * (mx * (0.5 - q2q2 - q3q3) + my * (q1q2 - q0q3) + mz * (q1q3 + q0q2))
                hy = 2 * (mx * (q1q2 + q0q3) + my * (0.5 - q1q1 - q3q3) + mz * (q2q3 - q0q1))
                bx = math.sqrt(hx * hx + hy * hy)
                bz = 2 * (mx * (q1q3 - q0q2) + my * (q2q3 + q0q1) + mz * (0.5 - q1q1 - q2q2))
                wx = 2 * (bx * (0.5 - q2q2 - q3q3) + bz * (q1q3 - q0q2))
                wy = 2 * (bx * (q1q2 - q0q3) + bz * (q0q1 + q2q3))
                wz = 2 * (bx * (q0q2 + q1q3) + bz * (0.5 - q1q1 - q2q2))
                emx, emy, emz = my * wz - mz * wy, mz * wx - mx * wz, mx * wy - my * wx
                # Heading only: keep the component about the vertical axis.
                d = emx * vx + emy * vy + emz * vz
                ex += kp_mag * d * vx
                ey += kp_mag * d * vy
                ez += kp_mag * d * vz
                self.mag_used = True

        if ki:
            self.bias[0] += ki * ex * dt
            self.bias[1] += ki * ey * dt
            self.bias[2] += ki * ez * dt
        gx += ex + self.bias[0]
        gy += ey + self.bias[1]
        gz += ez + self.bias[2]

        h = 0.5 * dt
        q0, q1, q2, q3 = (q0 + h * (-q1 * gx - q2 * gy - q3 * gz),
                          q1 + h * (q0 * gx + q2 * gz - q3 * gy),
                          q2 + h * (q0 * gy - q1 * gz + q3 * gx),
                          q3 + h * (q0 * gz + q1 * gy - q2 * gx))
        n = math.sqrt(q0 * q0 + q1 * q1 + q2 * q2 + q3 * q3)
        self.q = [q0 / n, q1 / n, q2 / n, q3 / n]

    def euler(self):
        q0, q1, q2, q3 = self.q
        roll = math.atan2(2 * (q0 * q1 + q2 * q3), 1 - 2 * (q1 * q1 + q2 * q2))
        pitch = math.asin(max(-1.0, min(1.0, 2 * (q0 * q2 - q3 * q1))))
        yaw = math.atan2(2 * (q0 * q3 + q1 * q2), 1 - 2 * (q2 * q2 + q3 * q3))
        return roll / DEG, pitch / DEG, yaw / DEG


# --- calibration ---------------------------------------------------------------------------

def rotations24():
    """The 24 axis-aligned rotations (signed permutation matrices with determinant +1)."""
    out = []
    for perm in ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)):
        for signs in ((1, 1, 1), (1, 1, -1), (1, -1, 1), (1, -1, -1), (-1, 1, 1), (-1, 1, -1), (-1, -1, 1), (-1, -1, -1)):
            m = np.zeros((3, 3))
            for row, (col, s) in enumerate(zip(perm, signs)):
                m[row, col] = s
            if np.linalg.det(m) > 0:
                out.append(m)
    return out


def fit_magnetometer(raw):
    """Hard + soft iron from samples covering many directions. Returns (center, W, field, method)."""
    m = np.asarray(raw, dtype=float)
    x, y, z = m.T
    # Sphere fit (robust with patchy coverage): |m - c|^2 = r^2 is linear in (c, r^2 - |c|^2).
    a = np.column_stack([2 * x, 2 * y, 2 * z, np.ones(len(x))])
    sol = np.linalg.lstsq(a, x * x + y * y + z * z, rcond=None)[0]
    center = sol[:3]
    radius = math.sqrt(sol[3] + center @ center)
    result = (center, np.eye(3), radius, "sphere")
    # Ellipsoid fit: a x^2 + b y^2 + c z^2 + 2d xy + 2e xz + 2f yz + 2g x + 2h y + 2i z = 1.
    d = np.column_stack([x * x, y * y, z * z, 2 * x * y, 2 * x * z, 2 * y * z, 2 * x, 2 * y, 2 * z])
    v = np.linalg.lstsq(d, np.ones(len(x)), rcond=None)[0]
    q = np.array([[v[0], v[3], v[4]], [v[3], v[1], v[5]], [v[4], v[5], v[2]]])
    try:
        c = -np.linalg.solve(q, v[6:9])
        k = 1 + c @ q @ c
        evals, evecs = np.linalg.eigh(q / k)
        if np.all(evals > 0):
            radii = 1 / np.sqrt(evals)
            if radii.max() / radii.min() < 1.6:
                field = float(np.prod(radii) ** (1 / 3))
                w = evecs @ np.diag(np.sqrt(evals)) @ evecs.T * field
                result = (c, w, field, "ellipsoid")
    except np.linalg.LinAlgError:
        pass
    return result


def coverage(unit_vectors):
    """Share of 26 evenly spread directions that have at least two samples near them."""
    dirs = np.array([(i, j, k) for i in (-1, 0, 1) for j in (-1, 0, 1) for k in (-1, 0, 1) if (i, j, k) != (0, 0, 0)], float)
    dirs /= np.linalg.norm(dirs, axis=1)[:, None]
    nearest = np.argmax(np.asarray(unit_vectors) @ dirs.T, axis=1)
    counts = np.bincount(nearest, minlength=len(dirs))
    return float(np.mean(counts >= 2))


def _rodrigues(rv):
    """Rotation matrices (n, 3, 3) for rotation vectors (n, 3), in radians."""
    theta = np.linalg.norm(rv, axis=1)
    k = rv / np.where(theta > 1e-12, theta, 1.0)[:, None]
    K = np.zeros((len(rv), 3, 3))
    K[:, 0, 1], K[:, 0, 2], K[:, 1, 2] = -k[:, 2], k[:, 1], -k[:, 0]
    K[:, 1, 0], K[:, 2, 0], K[:, 2, 1] = k[:, 2], -k[:, 1], k[:, 0]
    s, c = np.sin(theta)[:, None, None], np.cos(theta)[:, None, None]
    return np.eye(3) + s * K + (1 - c) * (K @ K)


def best_alignment(mag_samples, imu_samples):
    """Which axis-aligned rotation maps the magnetometer frame onto the IMU frame?

    Between consecutive magnetometer readings the gyro says how the board turned; a fixed
    earth field must turn the opposite way in the sensor frame. The candidate that predicts
    that best is the mounting. Returns (rotation, confidence = 2nd-best error / best error).
    """
    t_imu = np.array([s[0] for s in imu_samples], dtype=float)
    w_imu = np.array([s[2] for s in imu_samples]) * DEG   # calibrated gyro, rad/s
    if len(t_imu) < 2 or len(mag_samples) < 51:
        return np.eye(3), 0.0
    # Rotation between two magnetometer readings (~40 ms, a few degrees): the summed rotation
    # vector of the gyro samples in between, which is as good as composing them one by one.
    dt = np.diff(t_imu, prepend=t_imu[0]) / 1e9
    cum = np.vstack([np.zeros(3), np.cumsum(w_imu * dt[:, None], axis=0)])
    t_mag = np.array([t for t, _ in mag_samples], dtype=float)
    m = np.array([v for _, v in mag_samples], dtype=float)
    i = np.searchsorted(t_imu, t_mag, side="right")   # IMU samples in (t0, t1] are i[k]..i[k+1]-1
    keep = (np.diff(t_mag) <= 0.2e9) & (i[1:] - i[:-1] >= 2)
    if keep.sum() < 50:
        return np.eye(3), 0.0
    rots = _rodrigues((cum[i[1:]] - cum[i[:-1]])[keep])
    m0s, m1s = m[:-1][keep], m[1:][keep]
    errors = []
    for c in rotations24():
        p0 = m0s @ c.T
        p1 = m1s @ c.T
        pred = np.einsum("nji,nj->ni", rots, p0)   # r^T @ p0 for every pair
        errors.append(float(np.mean(np.sum((pred - p1) ** 2, axis=1))))
    order = np.argsort(errors)
    best, second = errors[order[0]], errors[order[1]]
    return rotations24()[order[0]], (second / best if best > 0 else 0.0)


class Calibration:
    """Calibration values in effect; persisted as calibration.json."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = None
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            pass
        self._load()

    def _load(self):
        d = self.data or {}
        mag = d.get("mag") or {}
        self.gyro_bias = np.array(d.get("gyro_bias_dps", [0, 0, 0]), float)
        self.mag_center = np.array(mag.get("offset_uT", [0, 0, 0]), float)
        self.mag_w = np.array(mag.get("soft_iron", np.eye(3).tolist()), float)
        self.mag_rot = np.array(mag.get("rotation_to_imu", np.eye(3).tolist()), float)
        self.camera_delta = np.array(mag.get("camera_delta_uT", [0, 0, 0]), float)
        self.field = mag.get("field_uT")
        self.dip = mag.get("angle_to_up_deg")
        self.dip_rad = None if self.dip is None else self.dip * DEG

    def save(self, data):
        self.data = data
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)
        self._load()

    @property
    def done(self):
        return self.data is not None

    def mag_to_imu(self, raw, camera_on):
        """Raw magnetometer uT -> calibrated field in the IMU frame (uT)."""
        center = self.mag_center if camera_on else self.mag_center - self.camera_delta
        return self.mag_rot @ (self.mag_w @ (np.asarray(raw, float) - center))

    def summary(self):
        if not self.data:
            return None
        d = self.data
        return {k: d.get(k) for k in ("time", "quality", "warnings")} | {
            "field_uT": self.field, "method": (d.get("mag") or {}).get("method")}


class MotionPipeline(threading.Thread):
    """Owns the IMU: drains its FIFO, timestamps, runs the filter, feeds logs and calibration."""

    def __init__(self, slot, calibration):
        super().__init__(daemon=True, name="motion")
        self.slot = slot
        self.cal = calibration
        self.ahrs = Mahony()
        self.clock = None
        self.lock = threading.Lock()
        self.latest = None            # newest sample for the dashboard
        self.mag_queue = collections.deque()
        self.mag_latest = None
        self.log = None               # recorder.SessionLog while recording
        self.collector = None         # list while calibration collects
        self.mag_collector = None
        self.rate = 0.0
        self.tare = None              # orientation picked as "straight ahead" for the cube, or None
        self.temperature = None
        self._last_count = None
        self._clock_dev = None
        self._rate_n = 0
        self._rate_t = time.monotonic()

    def run(self):
        make_thread_realtime("IMU reader")
        period = 1 / DRAIN_HZ
        next_t = time.monotonic()
        loops = 0
        while True:
            loops += 1
            if loops % DRAIN_HZ == 0:   # this thread owns the IMU, so it reads the temperature too
                self.temperature = self.slot.call(lambda d: d.read_temp())
            samples = self.slot.call(lambda d: d.read_fifo())
            if samples:
                dev = self.slot.dev
                if dev is not self._clock_dev:   # (re)opened: its counter starts over
                    self.clock = ClockMap(dev.ns_per_count, dev.COUNTS_PER_SAMPLE)
                    self._clock_dev, self._last_count = dev, None
                self.clock.add(samples[-1][0], time.monotonic_ns())
                for s in samples:
                    self._process(*s)
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()

    def _process(self, count, accel, gyro):
        t = self.clock.to_ns(count)
        if self._last_count is None or count <= self._last_count:
            dt = self.clock.lag_counts * self.clock.scale / 1e9   # one sample period
        else:
            dt = min(0.1, (count - self._last_count) * self.clock.scale / 1e9)
        self._last_count = count
        b = self.cal.gyro_bias
        g = (gyro[0] - b[0], gyro[1] - b[1], gyro[2] - b[2])
        mag = None
        while self.mag_queue and self.mag_queue[0][0] <= t:
            mag = self.mag_queue.popleft()[1]
        self.ahrs.update((float(g[0]) * DEG, float(g[1]) * DEG, float(g[2]) * DEG), accel, dt, mag, self.cal.dip_rad)
        q = self.ahrs.q
        if self.log is not None:
            self.log.imu(t, accel, gyro, q)
        if self.collector is not None:
            self.collector.append((t, accel, tuple(g)))
        self._rate_n += 1
        now = time.monotonic()
        if now - self._rate_t >= 1:
            self.rate = self._rate_n / (now - self._rate_t)
            self._rate_n, self._rate_t = 0, now
        self.latest = (t, accel, gyro)

    def add_mag(self, t_ns, raw, camera_on):
        """Called by the sampler with every magnetometer reading."""
        cal = self.cal.mag_to_imu(raw, camera_on)
        n = float(np.linalg.norm(cal))
        usable = n > 0 and (self.cal.field is None or abs(n / self.cal.field - 1) < 0.15)
        if usable and self.cal.done:
            self.mag_queue.append((t_ns, tuple(float(v) for v in cal / n)))
        self.mag_latest = (t_ns, raw, cal, usable)
        if self.log is not None:
            self.log.mag(t_ns, raw, cal)
        if self.mag_collector is not None:
            self.mag_collector.append((t_ns, tuple(raw)))

    def set_tare(self):
        """Make the current heading the cube's 'straight ahead'."""
        self.tare = list(self.ahrs.q)

    def state(self):
        latest = self.latest
        if latest is None:
            return None
        t, a, g = latest
        roll, pitch, yaw = self.ahrs.euler()
        an = math.sqrt(sum(v * v for v in a))
        mag = self.mag_latest
        return {
            "t_ns": t,
            "accel": [round(v, 4) for v in a],
            "gyro": [round(v, 3) for v in g],
            "gyro_cal": [round(v, 3) for v in np.subtract(g, self.cal.gyro_bias)],
            "accel_norm": round(an, 4),
            "q": [round(v, 5) for v in self.ahrs.q],
            "tare": None if self.tare is None else [round(v, 5) for v in self.tare],
            "roll": round(roll, 2), "pitch": round(pitch, 2), "yaw": round(yaw, 2),
            "mag_used": self.ahrs.mag_used, "accel_used": self.ahrs.accel_used,
            "mag_cal": None if mag is None else [round(float(v), 2) for v in mag[2]],
            "mag_ok": None if mag is None else mag[3],
            "imu_hz": round(self.rate, 1),
            "overruns": getattr(self.slot.dev, "overruns", 0),
            "calibrated": self.cal.done,
        }


class CalibrationRun(threading.Thread):
    """The calibration wizard, run in the background and reported through status()."""

    STILL_OFF_S, SETTLE_S, STILL_ON_S, ROTATE_S, ROTATE_MIN_S = 2.5, 3.0, 3.0, 45.0, 15.0
    ENOUGH_COVERAGE = 0.75   # of 26 directions; finish the rotation phase early once reached

    def __init__(self, motion, camera, camera_present):
        super().__init__(daemon=True, name="calibration")
        self.motion = motion
        self.camera = camera
        self.camera_present = camera_present
        self.step = "starting"
        self.message = "Starting"
        self.progress = 0.0
        self.coverage = 0.0
        self.result = None
        self.error = None
        self.cancelled = False
        self.finished = False

    def status(self):
        return {"step": self.step, "message": self.message, "progress": round(self.progress, 2),
                "coverage": round(self.coverage, 2), "result": self.result, "error": self.error,
                "running": not self.finished}

    def cancel(self):
        self.cancelled = True

    def _collect(self, seconds, mag=True, imu=True, on_tick=None):
        m = self.motion
        m.collector = [] if imu else None
        m.mag_collector = [] if mag else None
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < seconds:
                if self.cancelled:
                    raise RuntimeError("cancelled")
                self.progress = (time.monotonic() - t0) / seconds
                if on_tick and on_tick(time.monotonic() - t0):
                    break
                time.sleep(0.1)
            return list(m.collector or []), list(m.mag_collector or [])
        finally:
            m.collector = m.mag_collector = None

    def run(self):
        acquired = False
        try:
            camera_was_on = self.camera.status()["state"] == "live"
            mag_off = None
            if not camera_was_on:
                self.step, self.message = "still", "Put the head mount down and don't touch it"
                time.sleep(1.0)
                _, mags = self._collect(self.STILL_OFF_S, imu=False)
                if len(mags) >= 20:
                    mag_off = np.mean([m for _, m in mags], axis=0)
            if self.camera_present:
                # From here on the camera runs, exactly as while recording, so its current is in the calibration.
                self.camera.acquire()
                acquired = True
                self.step, self.message = "camera", "Starting the camera; keep it still"
                t0 = time.monotonic()
                while self.camera.status()["state"] != "live":
                    if self.cancelled:
                        raise RuntimeError("cancelled")
                    if time.monotonic() - t0 > 25:
                        raise RuntimeError("camera did not start: " + str(self.camera.status()["error"]))
                    time.sleep(0.2)
                time.sleep(self.SETTLE_S)
            self.step, self.message = "still", "Keep it still (measuring gyro bias)"
            imu_still, mag_still = self._collect(self.STILL_ON_S)
            if len(imu_still) < 300 or len(mag_still) < 20:
                raise RuntimeError("not enough sensor data; are the IMU and magnetometer connected?")
            raw_g = np.array([s[2] for s in imu_still]) + self.motion.cal.gyro_bias
            acc = np.array([s[1] for s in imu_still])
            if raw_g.std(axis=0).max() > 1.0 or acc.std(axis=0).max() > 0.02:
                raise RuntimeError("the head mount moved during the still phase; put it down and try again")
            gyro_bias = raw_g.mean(axis=0)
            accel_still = acc.mean(axis=0)
            mag_on = np.mean([m for _, m in mag_still], axis=0)
            camera_delta = (mag_on - mag_off) if mag_off is not None and self.camera_present else None

            # Rotation phase uses the new gyro bias for the alignment search.
            old_bias = self.motion.cal.gyro_bias
            self.motion.cal.gyro_bias = gyro_bias
            self.step, self.message = "rotate", "Pick it up and slowly turn it in every direction"
            def tick(elapsed):
                mags = self.motion.mag_collector
                if mags and len(mags) > 30:
                    pts = np.array([m for _, m in mags])
                    u = pts - fit_magnetometer(pts[-400:])[0]
                    u /= np.linalg.norm(u, axis=1)[:, None]
                    self.coverage = coverage(u)
                return elapsed > self.ROTATE_MIN_S and self.coverage >= self.ENOUGH_COVERAGE

            try:
                imu_rot, mag_rot = self._collect(self.ROTATE_S, on_tick=tick)
            finally:
                self.motion.cal.gyro_bias = old_bias
            if len(mag_rot) < 200:
                raise RuntimeError("not enough magnetometer data while rotating")
            self.step, self.message, self.progress = "compute", "Computing", 1.0
            raw = np.array([m for _, m in mag_rot])
            center, w, field, method = fit_magnetometer(raw)
            corrected = (raw - center) @ w.T
            norms = np.linalg.norm(corrected, axis=1)
            cov = coverage(corrected / norms[:, None])
            if cov < 0.35:
                raise RuntimeError(f"only {cov:.0%} of directions covered; turn it through more orientations")
            residual = float(np.std(norms))
            rot, confidence = best_alignment([(t, c) for (t, _), c in zip(mag_rot, corrected)],
                                             [(t, a, g) for t, a, g in imu_rot])
            # Angle between the field and "up" while still, for the disturbance gate.
            m_still = rot @ (w @ (mag_on - center))
            up = accel_still / np.linalg.norm(accel_still)
            angle_to_up = math.degrees(math.acos(float(np.clip(m_still @ up / np.linalg.norm(m_still), -1, 1))))
            warnings = []
            if not self.camera_present:
                warnings.append("No camera, so its effect on the magnetometer could not be included.")
            elif camera_delta is None:
                warnings.append("The camera was already on, so its effect on the magnetometer was not measured; "
                                "the previous value is kept.")
            if camera_delta is None:
                prev = (self.motion.cal.data or {}).get("mag", {}).get("camera_delta_uT")
                camera_delta = np.array(prev if prev else [0, 0, 0], float)
            if cov < 0.6:
                warnings.append(f"Only {cov:.0%} of directions were covered; heading is less accurate in the missed ones.")
            if confidence < 1.5:
                warnings.append("The magnetometer-to-IMU mounting was hard to tell apart; rotate more on the next calibration.")
            if residual > 0.05 * field:
                warnings.append(f"The fit is loose ({residual:.1f} uT); keep away from metal and magnets while calibrating.")
            data = {
                "version": 1,
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "gyro_bias_dps": [round(float(v), 4) for v in gyro_bias],
                "accel_still_g": [round(float(v), 4) for v in accel_still],
                "mag": {
                    "method": method,
                    "offset_uT": [round(float(v), 3) for v in center],
                    "soft_iron": [[round(float(v), 5) for v in row] for row in w],
                    "rotation_to_imu": rot.astype(int).tolist(),
                    "camera_delta_uT": [round(float(v), 3) for v in camera_delta],
                    "field_uT": round(field, 2),
                    "angle_to_up_deg": round(angle_to_up, 2),
                    "camera_mode": (self.camera.status().get("camera") or {}).get("config"),
                },
                "quality": {"coverage": round(cov, 2), "residual_uT": round(residual, 2),
                            "alignment_confidence": round(confidence, 2), "samples": len(mag_rot)},
                "warnings": warnings,
                "frames": {"imu": "LSM6DSV16B axes", "mag_raw": "MMC5983MA axes; calibrated = rotation_to_imu @ soft_iron @ (raw - offset)",
                           "offset_note": "offset_uT is with the camera running; camera off: offset_uT - camera_delta_uT"},
            }
            self.motion.cal.save(data)
            self.motion.ahrs.age = 0.0   # re-converge quickly with the new values
            self.result = data
            self.step, self.message = "done", "Calibration saved"
            log.info("calibration saved: %s, coverage %.0f%%, field %.1f uT, residual %.2f, alignment x%.1f",
                     method, cov * 100, field, residual, confidence)
        except Exception as e:
            self.error = str(e)
            self.step, self.message = "failed", f"Calibration failed: {e}"
            log.warning("calibration failed: %s", e)
        finally:
            if acquired:
                self.camera.release()
            self.finished = True
