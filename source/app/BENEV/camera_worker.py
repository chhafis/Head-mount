"""AR0234 camera service: libcamera + the Pi's ISP and hardware encoders, through Picamera2.

server.py runs this as a child process while the camera is in use. It owns the camera and
  - streams preview JPEGs (hardware MJPEG of the low-res stream),
  - records the main stream with the hardware H.264 encoder (MJPEG above 1920x1080, which
    the VideoCore H.264 encoder cannot take),
  - takes stills: full sensor resolution with high-quality denoise, JPEG q95 (+ optional DNG),
  - switches resolution / frame rate on request.

argv:   width height fps
stdin:  one JSON command per line: {"id": n, "cmd": "still" | "record_start" | "record_stop"
        | "configure" | "shading_capture", ...}
stdout: records <u32 meta length><u32 JPEG length><meta JSON><JPEG>. Preview frames carry the
        current status; command replies and events carry an empty JPEG.
"""
import json
import os
import signal
import struct
import sys
import threading
import time
from pathlib import Path

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, MJPEGEncoder
from picamera2.outputs import Output, PyavOutput
from libcamera import Transform, controls

MEDIA = Path(__file__).resolve().parent / "media"
PREVIEW_WIDTH = 640
PREVIEW_FPS = 15                # preview frames sent to the dashboard per second
H264_MAX = (1920, 1080)         # largest frame the VideoCore H.264 encoder accepts
# Highest frame rate each output size records without dropping frames on the Zero 2W
# (measured 2026-10-23, 10-bit sensor modes, Fast denoise, preview running; above these the
# ISP or encoder falls behind: 1200p30 MJPEG -> 28.4 fps, 1080p60 -> 44, 720p100 -> 79).
RECORD_MAX_FPS = {(1920, 1200): 25, (1920, 1080): 30, (1280, 720): 60, (960, 600): 100}
JPEG_QUALITY = 95
SHADING_FRAMES = 8              # raw frames averaged for the lens shading calibration


def image_controls():
    """ISP controls from the image settings (image_tuning.controls, passed by camera.py as JSON):
    exposure mode, metering, exposure compensation, sharpness, contrast, saturation."""
    try:
        wanted = json.loads(os.environ.get("BENEV_IMAGE_CONTROLS") or "{}")
    except ValueError:
        return {}
    enums = {"AeExposureMode": controls.AeExposureModeEnum, "AeMeteringMode": controls.AeMeteringModeEnum,
             "AeConstraintMode": controls.AeConstraintModeEnum, "AeFlickerMode": controls.AeFlickerModeEnum}
    out = {}
    for key, value in wanted.items():
        out[key] = getattr(enums[key], value) if key in enums else value
    return out
TRANSFORM = Transform()   # libcamera already corrects the 180° mounting from the overlay (rotation=180)

out_lock = threading.Lock()


def emit(meta, jpeg=b""):
    data = json.dumps(meta, separators=(",", ":")).encode()
    with out_lock:
        sys.stdout.buffer.write(struct.pack("<II", len(data), len(jpeg)) + data + jpeg)
        sys.stdout.buffer.flush()


def log(msg):
    try:
        print(f"camera_worker: {msg}", file=sys.stderr, flush=True)
    except (BrokenPipeError, ValueError):
        pass   # the server is gone (shutdown); finishing the recording matters more than the log


def even(v):
    return int(v) // 2 * 2


class PreviewOutput(Output):
    """Takes the hardware MJPEG frames and forwards every `every`-th one (about PREVIEW_FPS)."""

    def __init__(self, service):
        super().__init__()
        self.service = service
        self.every = 1
        self.count = 0
        self.last = 0.0
        self.fps = 0.0

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        # Counting frames rather than timing them: frames arriving a hair early must not be dropped.
        self.count += 1
        if self.count % self.every:
            return
        now = time.monotonic()
        if self.last:
            self.fps += 0.2 * (1 / (now - self.last) - self.fps)
        self.last = now
        emit(self.service.status(self.fps), bytes(frame))


class FrameLoggingOutput(PyavOutput):
    """The video file plus frames.csv: one row for every frame actually written.

    picamera2 hands outputs the frame's SensorTimestamp relative to the first encoded frame,
    and PyAV uses that as the presentation time, so video time 0 is the first row's t_ns.
    """

    def __init__(self, path, fmt, encoder, frames_path, metadata):
        super().__init__(str(path), format=fmt)
        self.encoder = encoder
        self.metadata = metadata          # SensorTimestamp (us) -> request metadata, from post_callback
        self.frames = open(frames_path, "w", buffering=1 << 16)
        self.frames.write("frame,t_ns,pts_us,exposure_us,analog_gain,digital_gain,colour_temp_k\n")
        self.n = 0

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        super().outputframe(frame, keyframe, timestamp, packet, audio)
        if audio or timestamp is None or self.frames.closed:
            return
        ts_us = self.encoder.firsttimestamp + timestamp
        md = self.metadata.get(ts_us, {})
        self.frames.write(f"{self.n},{ts_us * 1000},{timestamp},{md.get('ExposureTime', '')},"
                          f"{md.get('AnalogueGain', 0):.3f},{md.get('DigitalGain', 0):.3f},{md.get('ColourTemperature', '')}\n")
        self.n += 1

    def stop(self):
        super().stop()
        self.frames.close()


class CameraService:
    def __init__(self, width, height, fps):
        # The lens shading calibration travels in a copy of the sensor's tuning file (camera.py).
        # Picamera2 ignores the LIBCAMERA_RPI_TUNING_FILE variable, so it is passed here.
        self.picam2 = Picamera2(tuning=os.environ.get("BENEV_TUNING_FILE") or None)
        self.modes = sorted({(m["size"], round(m["fps"], 1), m["bit_depth"]) for m in self.picam2.sensor_modes},
                            key=lambda m: m[0][0] * m[0][1])
        self.full_size = max((m[0] for m in self.modes), key=lambda s: s[0] * s[1])
        self.metadata = {}
        self.recent_metadata = {}         # SensorTimestamp in us -> metadata, last few frames
        self.picam2.post_callback = self._on_request
        self.preview = PreviewOutput(self)
        self.preview_encoder = None
        self.recording = None
        self.config = None
        self.configure(width, height, fps)

    # --- configuration

    def _sensor_mode_for(self, size, fps):
        """Smallest native mode that covers the size at the frame rate; else the fastest that covers it."""
        covering = [m for m in self.modes if m[0][0] >= size[0] and m[0][1] >= size[1]]
        if not covering:
            raise ValueError(f"{size[0]}x{size[1]} is larger than the sensor")
        fast = [m for m in covering if m[1] >= fps]
        return min(fast, key=lambda m: m[0][0] * m[0][1]) if fast else max(covering, key=lambda m: m[1])

    def configure(self, width, height, fps):
        size = (even(width), even(height))
        mode = self._sensor_mode_for(size, fps)
        fps = min(float(fps), mode[1], RECORD_MAX_FPS.get(size, mode[1]))
        preview = (even(PREVIEW_WIDTH), even(PREVIEW_WIDTH * size[1] / size[0]))
        frame_us = int(1e6 / fps)
        cfg = self.picam2.create_video_configuration(
            main={"size": size, "format": "YUV420"},
            lores={"size": preview, "format": "YUV420"},
            raw={"size": mode[0]},
            sensor={"output_size": mode[0], "bit_depth": mode[2]},
            transform=TRANSFORM,
            buffer_count=6,
            # HighQuality denoise caps the VideoCore ISP at ~16 fps for 1080p; stills still get it.
            controls={"FrameDurationLimits": (frame_us, frame_us),
                      "NoiseReductionMode": controls.draft.NoiseReductionModeEnum.Fast,
                      **image_controls()})
        self._stop_preview()
        if self.picam2.started:
            self.picam2.stop()
        self.picam2.configure(cfg)
        self.picam2.start()
        self.config = {"width": size[0], "height": size[1], "fps": round(fps, 2),
                       "sensor_mode": f"{mode[0][0]}x{mode[0][1]} {mode[2]}-bit",
                       "record_codec": "h264" if size[0] <= H264_MAX[0] and size[1] <= H264_MAX[1] else "mjpeg"}
        self.preview.every = max(1, round(fps / PREVIEW_FPS))
        self._start_preview()
        log(f"configured {self.config}")

    def _start_preview(self):
        self.preview_encoder = MJPEGEncoder(bitrate=8_000_000)
        self.picam2.start_encoder(self.preview_encoder, self.preview, name="lores")

    def _stop_preview(self):
        if self.preview_encoder is not None:
            self.picam2.stop_encoder(self.preview_encoder)
            self.preview_encoder = None

    # --- status

    def _on_request(self, request):
        self.metadata = md = request.get_metadata()
        ts = md.get("SensorTimestamp")
        if ts is not None:
            recent = self.recent_metadata
            recent[ts // 1000] = md
            if len(recent) > 60:
                for k in sorted(recent)[:-30]:
                    del recent[k]

    def status(self, preview_fps=None):
        md = self.metadata
        frame_us = md.get("FrameDuration")
        rec = self.recording
        return {
            "type": "frame",
            "fps": round(1e6 / frame_us, 1) if frame_us else None,
            "preview_fps": round(preview_fps, 1) if preview_fps else None,
            "exposure_ms": round(md["ExposureTime"] / 1000, 2) if "ExposureTime" in md else None,
            "analog_gain": round(md.get("AnalogueGain", 0), 2),
            "digital_gain": round(md.get("DigitalGain", 0), 2),
            "colour_gains": [round(g, 2) for g in md.get("ColourGains", ())],
            "colour_temp": md.get("ColourTemperature"),
            "lux": round(md["Lux"]) if "Lux" in md else None,
            "config": self.config,
            "modes": [{"size": list(m[0]), "fps": m[1], "bit_depth": m[2]} for m in self.modes],
            "recording": None if rec is None else {"file": rec["file"], "codec": rec["codec"],
                                                   "seconds": round(time.monotonic() - rec["t0"], 1)},
        }

    # --- commands

    def record_start(self, folder=None):
        """Record into media/ (single video) or into a session folder (video + frames.csv)."""
        if self.recording:
            raise RuntimeError("already recording")
        MEDIA.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        w, h, fps = self.config["width"], self.config["height"], self.config["fps"]
        if self.config["record_codec"] == "h264":
            # ~0.4 bit per pixel is visually transparent for H.264; 25 Mb/s is the encoder's comfortable ceiling.
            bitrate = int(min(25e6, max(8e6, w * h * fps * 0.4)))
            encoder = H264Encoder(bitrate=bitrate, repeat=True, iperiod=max(1, round(fps)), profile="high")
            ext, fmt = "mp4", "mp4"
        else:
            bitrate = int(min(80e6, w * h * fps * 1.5))
            encoder = MJPEGEncoder(bitrate=bitrate)
            ext, fmt = "mkv", "matroska"
        if folder:
            path = Path(folder) / f"video.{ext}"
            output = FrameLoggingOutput(path, fmt, encoder, Path(folder) / "frames.csv", self.recent_metadata)
        else:
            path = MEDIA / f"video-{stamp}.{ext}"
            output = PyavOutput(str(path), format=fmt)
        self.picam2.start_encoder(encoder, output, name="main")
        self.recording = {"encoder": encoder, "path": path, "file": path.name, "codec": self.config["record_codec"],
                          "bitrate": bitrate, "t0": time.monotonic()}
        log(f"recording {path} ({self.config['record_codec']}, {bitrate / 1e6:.1f} Mb/s)")
        return {"file": path.name, "bitrate": bitrate, "codec": self.config["record_codec"]}

    def record_stop(self):
        if not self.recording:
            raise RuntimeError("not recording")
        rec, self.recording = self.recording, None
        self.picam2.stop_encoder(rec["encoder"])
        seconds = round(time.monotonic() - rec["t0"], 1)
        size = rec["path"].stat().st_size
        log(f"saved {rec['path']}: {seconds} s, {size / 1e6:.1f} MB")
        return {"file": rec["file"], "seconds": seconds, "bytes": size}

    def still(self, raw):
        MEDIA.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        files = [f"photo-{stamp}.jpg"] + ([f"photo-{stamp}.dng"] if raw else [])
        self.picam2.options["quality"] = JPEG_QUALITY
        if self.recording:
            # The video keeps running, so the photo comes from the main stream at video size.
            request = self.picam2.capture_request()
            size = (self.config["width"], self.config["height"])
        else:
            # Full sensor readout, high-quality denoise, then straight back to video mode.
            cfg = self.picam2.create_still_configuration(
                main={"size": self.full_size, "format": "BGR888"}, raw={"size": self.full_size},
                transform=TRANSFORM, buffer_count=2, controls=image_controls())
            self._stop_preview()
            try:
                request = self.picam2.switch_mode_and_capture_request(cfg, delay=2)
            finally:
                self._start_preview()
            size = self.full_size
        try:
            if self.recording:
                save_yuv420_jpeg(request.make_array("main"), size, MEDIA / files[0])
            else:
                request.save("main", str(MEDIA / files[0]))
            if raw:
                request.save_dng(str(MEDIA / files[1]))
        finally:
            request.release()
        log(f"saved {', '.join(files)} at {size[0]}x{size[1]}")
        return {"files": files, "size": list(size)}

    def shading_capture(self):
        """Average raw flat-field frames at full sensor size -> lens shading tables (lens_shading.py).
        Full size so the tables cover the whole sensor; libcamera crops them for smaller modes."""
        if self.recording:
            raise RuntimeError("stop the recording first")
        import numpy as np
        import lens_shading
        cfg = self.picam2.create_still_configuration(
            main={"size": (640, 400), "format": "YUV420"}, raw={"size": self.full_size},
            transform=TRANSFORM, buffer_count=2)
        self._stop_preview()
        self.picam2.stop()
        acc, mds = None, []
        try:
            self.picam2.configure(cfg)
            self.picam2.start()
            for _ in range(20):   # let exposure and white balance settle on the flat field
                self.picam2.capture_metadata()
            raw_cfg = self.picam2.camera_config["raw"]
            fmt, (w, h) = raw_cfg["format"], raw_cfg["size"]
            for _ in range(SHADING_FRAMES):
                request = self.picam2.capture_request()
                try:
                    img, depth = lens_shading.unpack_raw(request.make_array("raw"), fmt, w, h)
                    mds.append(request.get_metadata())
                finally:
                    request.release()
                acc = img.astype(np.float32) if acc is None else acc + img
        finally:
            c = self.config
            self.configure(c["width"], c["height"], c["fps"])   # back to video, preview restarts
        mean = acc / len(mds)
        top = 2 ** depth - 1
        md = mds[-1]
        black = [b / 2 ** (16 - depth) for b in md.get("SensorBlackLevels") or [2688] * 4]
        planes = lens_shading.split_bayer(mean, fmt)
        alsc, stats = lens_shading.compute_alsc(planes, black, md.get("ColourTemperature"))
        g = (lens_shading.grid_means(planes["Gr"]) + lens_shading.grid_means(planes["Gb"])) / 2
        centre = g[5:7, 7:9].mean()
        stats.update({
            "frames": len(mds), "raw_format": str(fmt), "size": [w, h],
            "exposure_ms": round(md.get("ExposureTime", 0) / 1000, 2), "analog_gain": round(md.get("AnalogueGain", 0), 2),
            "centre_level_pct": round(float((centre - black[1]) / (top - black[1]) * 100), 1),
            "saturated_pct": round(float((mean >= 0.97 * top).mean() * 100), 2),
        })
        log(f"lens shading measured: {stats}")
        return {"alsc": alsc, "stats": stats}


def save_yuv420_jpeg(array, size, path):
    import simplejpeg
    w, h = size
    stride = array.shape[1]
    y = array[:h, :w]
    u = array[h:h + h // 4].reshape(h // 2, stride // 2)[:, :w // 2]
    v = array[h + h // 4:h + h // 2].reshape(h // 2, stride // 2)[:, :w // 2]
    path.write_bytes(simplejpeg.encode_jpeg_yuv_planes(y.copy(), u.copy(), v.copy(), quality=JPEG_QUALITY))


def main():
    width, height, fps = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
    service = CameraService(width, height, fps)
    # systemd sends SIGTERM when the service stops or the Pi shuts down; turning it into
    # SystemExit runs the finally below, so an open recording still gets its MP4 index.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    try:
        emit({"type": "ready", **service.status()})
        for line in sys.stdin:
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            reply = {"type": "reply", "id": cmd.get("id")}
            try:
                name = cmd.get("cmd")
                if name == "still":
                    reply["result"] = service.still(bool(cmd.get("raw")))
                elif name == "record_start":
                    reply["result"] = service.record_start(cmd.get("dir"))
                elif name == "record_stop":
                    reply["result"] = service.record_stop()
                elif name == "shading_capture":
                    reply["result"] = service.shading_capture()
                elif name == "configure":
                    if service.recording:
                        raise RuntimeError("stop the recording before changing resolution or frame rate")
                    service.configure(cmd["width"], cmd["height"], cmd["fps"])
                    reply["result"] = service.config
                else:
                    raise ValueError(f"unknown command {name!r}")
                reply["ok"] = True
            except Exception as e:
                reply.update(ok=False, error=f"{type(e).__name__}: {e}")
                log(f"{cmd.get('cmd')} failed: {reply['error']}")
            emit(reply)
    finally:
        # stdin closed or SIGTERM: finish a recording cleanly before going. When systemd stops
        # the service both happen at once, and a SIGTERM landing during the cleanup would abort
        # it halfway and leave an unplayable file, so ignore it from here on.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if service.recording:
            service.record_stop()
        service.picam2.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"error: {type(e).__name__}: {e}")
        sys.exit(1)
