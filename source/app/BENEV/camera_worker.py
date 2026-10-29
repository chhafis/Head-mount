"""AR0234 camera service: libcamera + the Pi's ISP and hardware encoders, through Picamera2.

server.py runs this as a child process while the camera is in use. It owns the camera and
  - streams preview JPEGs (hardware MJPEG of the low-res stream),
  - records the main stream with the hardware H.264 encoder (MJPEG above 1920x1080, which
    the VideoCore H.264 encoder cannot take),
  - takes stills: full sensor resolution with high-quality denoise, JPEG q95 (+ optional DNG),
  - switches resolution / frame rate on request.

argv:   width height fps
stdin:  one JSON command per line: {"id": n, "cmd": "still" | "record_start" | "record_stop"
        | "configure" | "shading_capture" | "edge_map" | "edge_reload" | "live", ...}
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
from fractions import Fraction
from pathlib import Path

import numpy as np
from picamera2 import MappedArray, Picamera2
from picamera2.encoders import H264Encoder, MJPEGEncoder
from picamera2.outputs import Output, PyavOutput
from libcamera import ColorSpace, Transform, controls

import edge_boost
import lens_shading
from writebehind import WriteBehind

MEDIA = Path(__file__).resolve().parent / "media"
PREVIEW_WIDTH = 640
PREVIEW_FPS = 15                # preview frames sent to the dashboard per second
H264_MAX = (1920, 1080)         # largest frame the VideoCore H.264 encoder accepts
# Highest frame rate each output size records without dropping frames on the Zero 2W
# (measured 2026-10-23, 10-bit sensor modes, Fast denoise, preview running; above these the
# ISP or encoder falls behind: 1200p30 MJPEG -> 28.4 fps, 1080p60 -> 44, 720p100 -> 79).
RECORD_MAX_FPS = {(1920, 1200): 25, (1920, 1080): 30, (1280, 720): 60, (960, 600): 100}
# Camera 1 (picture not rotated): take the cut of a shorter picture off the top, not both ends.
CROP_FROM_TOP = os.environ.get("BENEV_ROTATE180") != "1"
# Whether that is the start (y = 0) of the sensor's coordinates; the overlay's 180-degree mounting flips them.
SENSOR_CUT_AT_START = os.environ.get("BENEV_CUT_AT_START", "0") == "1"
VIDEO_QUEUE_BYTES = 48 << 20     # encoded video waiting for the SD card; ~50 s at 8 Mb/s before frames are shed
BITS_PER_PIXEL = 0.13            # default H.264 bitrate: 1080p30 -> 8.1 Mb/s (the proposal says 6-8)
BITRATE_LIMITS_MBPS = (2.0, 25.0)   # 25 Mb/s is the hardware encoder's comfortable ceiling
JPEG_QUALITY = 95
SHADING_FRAMES = 8              # raw frames averaged for the lens shading calibration
LENS_FILE = Path(__file__).resolve().parent / "lens-shading.json"


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
# libcamera already corrects the 180° mounting from the overlay (rotation=180); BENEV_ROTATE180 turns the
# picture by a further 180° for a head mount worn the other way up.
TRANSFORM = Transform(hflip=1, vflip=1) if os.environ.get("BENEV_ROTATE180") == "1" else Transform()

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
    """Takes the hardware MJPEG frames and forwards every `every`-th one (about PREVIEW_FPS).

    The frame goes to a sender thread through a one-place mailbox: if the pipe to the server is
    slow (it is read by a busy Python process) the old frame is replaced by the newer one. Writing
    to the pipe here would stall the encoder's thread, which also hands the camera its buffers
    back, and a slow preview would then cost video frames.
    """

    def __init__(self, service):
        super().__init__()
        self.service = service
        self.every = 1
        self.count = 0
        self.last = 0.0
        self.fps = 0.0
        self._cond = threading.Condition()
        self._mail = None
        threading.Thread(target=self._send, daemon=True, name="preview-sender").start()

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        # Counting frames rather than timing them: frames arriving a hair early must not be dropped.
        self.count += 1
        if self.count % self.every:
            return
        now = time.monotonic()
        if self.last:
            self.fps += 0.2 * (1 / (now - self.last) - self.fps)
        self.last = now
        with self._cond:
            self._mail = (self.fps, bytes(frame))
            self._cond.notify()

    def _send(self):
        while True:
            with self._cond:
                while self._mail is None:
                    self._cond.wait()
                fps, jpeg = self._mail
                self._mail = None
            try:
                emit(self.service.status(fps), jpeg)
            except (BrokenPipeError, ValueError, OSError):
                return   # the server is gone


class MJPEGRateEncoder(MJPEGEncoder):
    """MJPEGEncoder that writes the real frame rate into the file (picamera2 hard-codes 30)."""

    def __init__(self, bitrate, fps):
        super().__init__(bitrate)
        self.fps = fps

    def _send_streams(self, output):
        output._add_stream("video", "mjpeg", rate=Fraction(self.fps).limit_denominator(1000),
                           width=self.width, height=self.height)


class VideoOutput(PyavOutput):
    """The video file, and for a session also frames.csv: one row for every frame written.

    picamera2 hands outputs the frame's SensorTimestamp relative to the first encoded frame,
    and PyAV uses that as the presentation time, so video time 0 is the first row's t_ns.

    The encoder's polling thread calls outputframe() and, in between, hands the camera its buffers
    back. If it waited for the SD card here (stalls of 240-580 ms were measured) the camera would
    run out of buffers and drop whole frames. So outputframe() only queues the frame; a writer
    thread does the muxing and the file writes (writebehind.py). Only when the card has stopped for
    so long that the queue is full are frames shed, and then from the next keyframe on, so the
    video stays decodable; the number is reported.
    """

    def __init__(self, path, fmt, encoder=None, frames_path=None, metadata=None):
        super().__init__(str(path), format=fmt)
        self.encoder = encoder
        self.metadata = metadata          # SensorTimestamp (us) -> request metadata, from post_callback
        self.frames = None
        if frames_path is not None:
            self.frames = open(frames_path, "w", buffering=1 << 16)
            self.frames.write("frame,t_ns,pts_us,exposure_us,analog_gain,digital_gain,colour_temp_k\n")
        self.n = 0
        self.writer = WriteBehind("video-writer", max_bytes=VIDEO_QUEUE_BYTES)
        self.shed = 0                     # frames not written because the queue was full
        self._shedding = False
        self.error_callback = lambda e: log(f"video write failed: {e}")
        self.stats = {}

    def start(self):
        super().start()
        self.writer.start()

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        if not self.recording:
            return
        if audio or packet is not None:      # not used by the hardware encoders: keep it simple and in order
            self.writer.put(PyavOutput.outputframe, self, frame, keyframe, timestamp, packet, audio, size=len(frame))
            return
        if self.writer.full:
            self._shedding = True
        if self._shedding:
            if not keyframe or self.writer.full:
                self.shed += 1
                return
            self._shedding = False
        row = None
        if self.frames is not None and timestamp is not None:
            ts_us = self.encoder.firsttimestamp + timestamp
            md = self.metadata.get(ts_us, {})   # looked up now: the table only keeps the last few frames
            row = (f"{self.n},{ts_us * 1000},{timestamp},{md.get('ExposureTime', '')},"
                   f"{md.get('AnalogueGain', 0):.3f},{md.get('DigitalGain', 0):.3f},{md.get('ColourTemperature', '')}\n")
            self.n += 1
        self.writer.put(self._write, frame, keyframe, timestamp, row, size=len(frame))

    def _write(self, frame, keyframe, timestamp, row):
        PyavOutput.outputframe(self, frame, keyframe, timestamp, None, False)
        if row is not None and not self.frames.closed:
            self.frames.write(row)

    def stop(self):
        # The encoder has stopped delivering frames; write what is queued while the file is still open.
        self.stats = {**self.writer.close(), "frames_shed": self.shed}
        super().stop()
        if self.frames is not None:
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
        self.live_controls = {}           # changed while running (brightness); kept across configure()
        # Edge brightening (edge_boost.py): extra per-pixel gain on top of the ISP's lens shading
        self.edge_strength = float(os.environ.get("BENEV_EDGE_BOOST") or 0)
        self.edge_grid = self._load_edge_grid()
        self.boost = None                 # {"main": (maps, size), "lores": (maps, size)} or None
        self.boost_paused = False         # during stills and measurements, which use other pictures
        self.boost_error = None
        self._edge_lock = threading.Lock()
        self._edge_thread = None
        self._edge_dirty = self._edge_rebuild = False
        self._edge_ln = {}                # stream name -> edge_boost.LogGain
        self._edge_sizes = {}
        self._edge_crop = (0, 0) + edge_boost.FULL_FRAME
        self.limited_range = True
        self.mode_info = {}
        self.edge_avg_ms = self.edge_peak_ms = 0.0
        self.picam2.pre_callback = self._boost_frame
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
        # Camera 1: its lens lights the lower part of the sensor, so a picture shorter than the sensor is cut
        # from the full-height readout with the cut taken off the top (the sensor's own 1080-line mode would
        # take 60 lines off both ends).
        self.picture_crop = None
        scaler_crop = {}
        if CROP_FROM_TOP and size[0] == self.full_size[0] and size[1] < self.full_size[1]:
            mode = max((m for m in self.modes if m[0] == self.full_size), key=lambda m: m[2])
            cut = self.full_size[1] - size[1]
            self.picture_crop = (0, cut, size[0], size[1])      # in the finished 1920x1200 picture
            limits = next((m["crop_limits"] for m in self.picam2.sensor_modes
                           if tuple(m["size"]) == tuple(mode[0]) and m["bit_depth"] == mode[2]), None)
            if limits:
                x0, y0 = limits[0], limits[1]
                scaler_crop = {"ScalerCrop": (x0, y0 + (0 if SENSOR_CUT_AT_START else cut), size[0], size[1])}
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
                      **scaler_crop, **image_controls(), **self.live_controls})
        self._stop_preview()
        if self.picam2.started:
            self.picam2.stop()
        self.picam2.configure(cfg)
        self.mode_info = next((m for m in self.picam2.sensor_modes
                               if tuple(m["size"]) == tuple(mode[0]) and m["bit_depth"] == mode[2]), {})
        try:
            self.limited_range = self.picam2.camera_config["colour_space"].range != ColorSpace.Range.Full
        except (KeyError, AttributeError):
            self.limited_range = True
        self.picam2.start()
        self.config = {"width": size[0], "height": size[1], "fps": round(fps, 2),
                       "sensor_mode": f"{mode[0][0]}x{mode[0][1]} {mode[2]}-bit",
                       "record_codec": "h264" if size[0] <= H264_MAX[0] and size[1] <= H264_MAX[1] else "mjpeg"}
        if self.picture_crop:
            self.config["crop"] = list(self.picture_crop)
        self.preview.every = max(1, round(fps / PREVIEW_FPS))
        self._start_preview()
        self._build_boost()
        log(f"configured {self.config}")

    def _start_preview(self):
        self.preview_encoder = MJPEGEncoder(bitrate=8_000_000)
        self.picam2.start_encoder(self.preview_encoder, self.preview, name="lores")

    def _stop_preview(self):
        if self.preview_encoder is not None:
            self.picam2.stop_encoder(self.preview_encoder)
            self.preview_encoder = None

    # --- edge brightening

    @staticmethod
    def _load_edge_grid():
        try:
            lens = lens_shading.oriented(json.loads(LENS_FILE.read_text()), TRANSFORM.hflip)
            grid = lens.get("edge_gain")
            return np.array(grid, dtype=np.float32) if grid else None
        except (OSError, ValueError, TypeError):
            return None

    def _build_boost(self):
        """New picture sizes (or a new map): drop the old gain maps and make new ones in the background."""
        self.boost = None
        with self._edge_lock:
            # Only the small preview picture: brightening the full 1080p video frame costs 35-40 ms per
            # frame in the running camera (measured), more than the 33 ms a 30 fps frame has.
            self._edge_sizes = {"lores": tuple(self.picam2.camera_config["lores"]["size"])}
            self._edge_crop = self.picture_crop or self.mode_info.get("crop_limits") or (0, 0) + edge_boost.FULL_FRAME
        self._schedule_boost(rebuild=True)

    def _schedule_boost(self, rebuild=False):
        """Make the gain maps for the current strength on a thread of their own: compiling the
        helper and building the maps take seconds, and the frame loop must not wait for them. It
        keeps using the previous maps (or none) until the new ones are swapped in."""
        with self._edge_lock:
            self._edge_dirty = True
            self._edge_rebuild = self._edge_rebuild or rebuild
            if self._edge_thread is not None and self._edge_thread.is_alive():
                return
            self._edge_thread = threading.Thread(target=self._edge_job, daemon=True, name="edge-maps")
            self._edge_thread.start()

    def _edge_job(self):
        while True:
            with self._edge_lock:
                if not self._edge_dirty:
                    return
                self._edge_dirty = False
                rebuild, self._edge_rebuild = self._edge_rebuild, False
                strength, grid, sizes, crop = self.edge_strength, self.edge_grid, dict(self._edge_sizes), self._edge_crop
            try:
                if strength <= 0 or grid is None:
                    self.boost, self.boost_error = None, None
                    continue
                if edge_boost.load() is None:
                    self.boost, self.boost_error = None, edge_boost.last_error
                    continue
                if rebuild or set(self._edge_ln) != set(sizes):
                    self._edge_ln = {name: edge_boost.build_log_gain(grid, crop, size) for name, size in sizes.items()}
                boost = {}
                for name, size in sizes.items():
                    maps = edge_boost.maps_for(self._edge_ln[name], strength)
                    if maps is not None:
                        boost[name] = (maps, size)
                self.boost, self.boost_error = boost or None, None
                if boost:
                    log("edge brightening on: strength %.0f %%, up to x%.1f" % (
                        100 * strength, max(m.max_gain for m, _ in boost.values())))
            except Exception as e:
                self.boost, self.boost_error = None, f"{type(e).__name__}: {e}"
                log(f"edge brightening failed: {self.boost_error}")

    def _boost_frame(self, request):
        """Runs before the encoders see a frame: brighten the edges of the preview picture (not the video)."""
        boost = self.boost
        if boost is None or self.boost_paused:
            return
        t0 = time.perf_counter()
        try:
            for name, (maps, size) in boost.items():
                with MappedArray(request, name) as m:
                    edge_boost.apply(maps, m.array, size, m.array.shape[1], self.limited_range)
        except Exception as e:   # never let the frame loop die over a cosmetic filter
            self.boost, self.boost_error = None, f"{type(e).__name__}: {e}"
            log(f"edge brightening stopped: {self.boost_error}")
            return
        ms = (time.perf_counter() - t0) * 1000
        self.edge_avg_ms += 0.05 * (ms - self.edge_avg_ms)
        self.edge_peak_ms = max(ms, self.edge_peak_ms * 0.995)

    def edge_map(self):
        """With the lens shading correction active, measure what is still dark in a flat field
        (the white cover) and return the extra gain map (lens_shading.edge_gain_grid)."""
        if self.recording:
            raise RuntimeError("stop the recording first")
        cfg = self.picam2.create_still_configuration(
            main={"size": self.full_size, "format": "YUV420"}, transform=TRANSFORM, buffer_count=2,
            controls=image_controls())
        self.boost_paused = True
        self._stop_preview()
        self.picam2.stop()
        acc = None
        try:
            self.picam2.configure(cfg)
            self.picam2.start()
            for _ in range(20):   # let exposure and white balance settle on the flat field
                self.picam2.capture_metadata()
            w, h = self.full_size
            for _ in range(SHADING_FRAMES):
                y = self.picam2.capture_array("main")[:h, :w].astype(np.float32)
                acc = y if acc is None else acc + y
        finally:
            self.boost_paused = False
            c = self.config
            self.configure(c["width"], c["height"], c["fps"])   # back to video; the preview restarts
        grid = lens_shading.edge_gain_grid(acc / SHADING_FRAMES)
        return {"grid": grid.tolist(), "max_gain": float(grid.max()),
                "boosted_pct": round(float((grid > 1.1).mean() * 100), 1)}

    def reload_edge(self):
        """The map in lens-shading.json changed (a new calibration): use it."""
        self.edge_grid = self._load_edge_grid()
        self._schedule_boost(rebuild=True)
        return {"map": self.edge_grid is not None}

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
            "edge": {"strength": self.edge_strength, "map": self.edge_grid is not None, "active": self.boost is not None,
                     "max_gain": round(max(m.max_gain for m, _ in self.boost.values()), 1) if self.boost else None,
                     "ms_avg": round(self.edge_avg_ms, 1), "ms_peak": round(self.edge_peak_ms, 1), "error": self.boost_error},
            "modes": [{"size": list(m[0]), "fps": m[1], "bit_depth": m[2]} for m in self.modes],
            "recording": None if rec is None else {"file": rec["file"], "codec": rec["codec"],
                                                   "seconds": round(time.monotonic() - rec["t0"], 1)},
        }

    # --- commands

    LIVE_CONTROLS = ("ExposureValue",)

    def set_live(self, wanted):
        """Change ISP controls (and the edge brightening strength) while the camera runs: no restart."""
        changes = {k: float(v) for k, v in wanted.items() if k in self.LIVE_CONTROLS}
        if "EdgeBoost" in wanted:
            self.edge_strength = min(1.0, max(0.0, float(wanted["EdgeBoost"])))
            self._schedule_boost()      # in the background; this returns at once
            changes["EdgeBoost"] = self.edge_strength
        elif not changes:
            raise ValueError("nothing to change")
        isp = {k: v for k, v in changes.items() if k in self.LIVE_CONTROLS}
        if isp:
            self.live_controls.update(isp)
            self.picam2.set_controls(isp)
        return changes

    def record_start(self, folder=None, bitrate_mbps=None):
        """Record into media/ (single video) or into a session folder (video + frames.csv)."""
        if self.recording:
            raise RuntimeError("already recording")
        MEDIA.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        w, h, fps = self.config["width"], self.config["height"], self.config["fps"]
        if self.config["record_codec"] == "h264":
            if bitrate_mbps:
                mbps = min(BITRATE_LIMITS_MBPS[1], max(BITRATE_LIMITS_MBPS[0], float(bitrate_mbps)))
            else:   # automatic: about 0.13 bit per pixel (8 Mb/s for 1080p30), within the encoder's limits
                mbps = min(BITRATE_LIMITS_MBPS[1], max(BITRATE_LIMITS_MBPS[0], w * h * fps * BITS_PER_PIXEL / 1e6))
            bitrate = int(mbps * 1e6)
            encoder = H264Encoder(bitrate=bitrate, repeat=True, iperiod=max(1, round(fps)), profile="high")
            ext, fmt = "mp4", "mp4"
        else:
            bitrate = int(min(80e6, w * h * fps * 1.5))
            encoder = MJPEGRateEncoder(bitrate, fps)
            ext, fmt = "mkv", "matroska"
        if folder:
            path = Path(folder) / f"video.{ext}"
            output = VideoOutput(path, fmt, encoder, Path(folder) / "frames.csv", self.recent_metadata)
        else:
            path = MEDIA / f"video-{stamp}.{ext}"
            output = VideoOutput(path, fmt)
        self.picam2.start_encoder(encoder, output, name="main")
        self.recording = {"encoder": encoder, "output": output, "path": path, "file": path.name,
                          "codec": self.config["record_codec"], "bitrate": bitrate, "t0": time.monotonic()}
        log(f"recording {path} ({self.config['record_codec']}, {bitrate / 1e6:.1f} Mb/s)")
        return {"file": path.name, "bitrate": bitrate, "codec": self.config["record_codec"]}

    def record_stop(self):
        if not self.recording:
            raise RuntimeError("not recording")
        rec, self.recording = self.recording, None
        self.picam2.stop_encoder(rec["encoder"])
        seconds = round(time.monotonic() - rec["t0"], 1)
        size = rec["path"].stat().st_size
        stats = rec["output"].stats
        log(f"saved {rec['path']}: {seconds} s, {size / 1e6:.1f} MB, writer {stats}")
        return {"file": rec["file"], "seconds": seconds, "bytes": size, "writer": stats}

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
                transform=TRANSFORM, buffer_count=2, controls={**image_controls(), **self.live_controls})
            self._stop_preview()
            self.boost_paused = True    # these pictures are not the video's size or format
            try:
                request = self.picam2.switch_mode_and_capture_request(cfg, delay=2)
            finally:
                self.boost_paused = False
                self._start_preview()
            size = self.full_size
            self._boost_still(request, size)
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

    def _boost_still(self, request, size):
        """Edge brightening for a full-size BGR888 photo, like the video gets."""
        if self.edge_strength <= 0 or self.edge_grid is None or edge_boost.load() is None:
            return
        maps = edge_boost.build_maps(self.edge_grid, self.edge_strength, (0, 0) + tuple(self.full_size), size)
        if maps is None:
            return
        with MappedArray(request, "main") as m:
            edge_boost.apply_rgb(maps, m.array, size)

    def shading_capture(self, frames=SHADING_FRAMES, settle=20):
        """Average raw flat-field frames at full sensor size -> lens shading tables (lens_shading.py).
        Full size so the tables cover the whole sensor; libcamera crops them for smaller modes."""
        if self.recording:
            raise RuntimeError("stop the recording first")
        import numpy as np
        import lens_shading
        cfg = self.picam2.create_still_configuration(
            main={"size": (640, 400), "format": "YUV420"}, raw={"size": self.full_size},
            transform=TRANSFORM, buffer_count=2)
        self.boost_paused = True
        self._stop_preview()
        self.picam2.stop()
        acc, mds = None, []
        try:
            self.picam2.configure(cfg)
            self.picam2.start()
            for _ in range(settle):   # let exposure and white balance settle on the flat field
                self.picam2.capture_metadata()
            raw_cfg = self.picam2.camera_config["raw"]
            fmt, (w, h) = raw_cfg["format"], raw_cfg["size"]
            for _ in range(frames):
                request = self.picam2.capture_request()
                try:
                    img, depth = lens_shading.unpack_raw(request.make_array("raw"), fmt, w, h)
                    mds.append(request.get_metadata())
                finally:
                    request.release()
                acc = img.astype(np.float32) if acc is None else acc + img
        finally:
            self.boost_paused = False
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
        grids = stats.pop("_grids", None)
        log(f"lens shading measured: {stats}")
        return {"alsc": alsc, "stats": stats, "grids": grids}


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
                    reply["result"] = service.record_start(cmd.get("dir"), cmd.get("bitrate_mbps"))
                elif name == "record_stop":
                    reply["result"] = service.record_stop()
                elif name == "shading_capture":
                    reply["result"] = service.shading_capture(int(cmd.get("frames") or SHADING_FRAMES),
                                                              int(cmd.get("settle") or 20))
                elif name == "live":
                    reply["result"] = service.set_live(cmd["controls"])
                elif name == "edge_map":
                    reply["result"] = service.edge_map()
                elif name == "edge_reload":
                    reply["result"] = service.reload_edge()
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
