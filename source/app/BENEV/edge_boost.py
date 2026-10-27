"""Edge brightening for a wide-angle lens: a gain map applied to every video frame.

The Pi's ISP lifts a lens's dark edges by at most 4x (lens shading tables). Where a lens still
leaves the edges dark, lens calibration also stores how much extra gain each part needs
("edge_gain" in lens-shading.json, made by lens_shading.edge_gain_grid from a flat field taken
with the ISP correction already active). This module turns that into a per-pixel map for the
current picture size and applies it, through a small C routine (edge_boost.c, compiled on first
use) because numpy needs 75-120 ms per 1080p frame on a Pi Zero 2 W and the C routine 8 ms.

It cannot make light where there is none: parts of the picture outside the lens's image circle
get no extra gain (the map fades out below a few percent of the white level), so they stay black
instead of turning into noise.
"""
import ctypes
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("dashboard")

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "edge_boost.c"
LIBRARY = HERE / "libedgeboost.so"   # not edge_boost.so: Python would import that instead of edge_boost.py
FULL_FRAME = (1920, 1200)    # the frame the map is measured on (the sensor's full mode)
CHROMA_STRENGTH = 0.7        # colour is reduced by gain ** -0.7 where the gain is large
NR_START, NR_FULL = 1.3, 4.3                 # gain at which noise reduction starts / is complete (brightness)
NR_START_CHROMA, NR_FULL_CHROMA = 1.15, 3.0  # the same for colour, which is noisier
NR_PIXELS_PER_RADIUS = 200   # neighbourhood radius = picture width / 200 (3 px at 640, 10 px at 1920)

_lib = None
last_error = None


def can_build():
    return LIBRARY.exists() or (SOURCE.exists() and shutil.which("gcc") is not None)


def load():
    """The compiled routine, building it if needed; None (and last_error) if that is not possible."""
    global _lib, last_error
    if _lib is not None:
        return _lib
    try:
        if not LIBRARY.exists() or LIBRARY.stat().st_mtime < SOURCE.stat().st_mtime:
            tmp = Path(tempfile.mkdtemp(dir=HERE)) / LIBRARY.name
            for flags in (["-O3", "-mfpu=neon-vfpv4", "-ftree-vectorize"], ["-O3"]):
                r = subprocess.run(["gcc", *flags, "-shared", "-fPIC", "-o", str(tmp), str(SOURCE)],
                                   capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    break
            else:
                raise RuntimeError(f"gcc failed: {r.stderr.strip()[:200]}")
            os.replace(tmp, LIBRARY)
            shutil.rmtree(tmp.parent, ignore_errors=True)
        lib = ctypes.CDLL(str(LIBRARY))
        p = ctypes.c_void_p
        lib.boost_luma.argtypes = [p, ctypes.c_int, ctypes.c_int, ctypes.c_int, p, ctypes.c_int, ctypes.c_int]
        lib.soften_chroma.argtypes = [p, ctypes.c_int, ctypes.c_int, ctypes.c_int, p]
        lib.boost_rgb.argtypes = [p, ctypes.c_int, ctypes.c_int, ctypes.c_int, p]
        i = ctypes.c_int
        lib.boost_luma_nr.argtypes = [p, i, i, i, p, p, i, i, i]
        lib.soften_chroma_nr.argtypes = [p, i, i, i, p, p, i]
        lib.boost_rgb_nr.argtypes = [p, i, i, i, p, p, i]
        lib.desaturate_rgb.argtypes = [p, i, i, i, p, i]
        for f in (lib.boost_luma_nr, lib.soften_chroma_nr, lib.boost_rgb_nr):
            f.restype = i
        _lib, last_error = lib, None
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        last_error = f"edge brightening is not available: {e}"
        log.warning("%s", last_error)
    return _lib


def _resize(arr, size):
    return np.asarray(Image.fromarray(np.asarray(arr, dtype=np.float32)).resize(size, Image.BILINEAR))


class Maps:
    """Gain maps for one picture size: luma gain (8.8 fixed point) and chroma keep factor (0-256)."""

    def __init__(self, gain, keep, mix, mix_chroma, radius):
        self.gain = np.ascontiguousarray(gain, dtype=np.uint16)
        self.keep = np.ascontiguousarray(keep, dtype=np.uint8)
        self.mix = np.ascontiguousarray(mix, dtype=np.uint8)               # noise reduction, luma / colour channels
        self.mix_chroma = np.ascontiguousarray(mix_chroma, dtype=np.uint8)  # the same for the chroma planes
        self.radius = int(radius)                                          # of the neighbourhood averaged

    @property
    def max_gain(self):
        return float(self.gain.max()) / 256


class LogGain:
    """ln(gain) at full strength for one picture size (and at half size for the chroma planes).
    Slow to make (seconds on a Pi Zero 2 W), but then any strength is one cheap exp()."""

    def __init__(self, luma, chroma):
        self.luma = luma
        self.chroma = chroma


def build_log_gain(grid, crop, size):
    """grid: (rows, cols) gains >= 1 over the full frame; crop: (x, y, w, h) of the sensor area
    this picture shows (the mode's crop limits, in full-frame pixels); size: (width, height)."""
    fw, fh = FULL_FRAME
    full = _resize(np.asarray(grid, dtype=np.float32), (fw, fh))
    x, y, w, h = (int(v) for v in crop)
    luma = np.log(np.maximum(_resize(full[y:y + h, x:x + w], tuple(size)), 1.0)).astype(np.float32)
    return LogGain(luma, _resize(luma, (size[0] // 2, size[1] // 2)))


def maps_for(log_gain, strength):
    """Maps at a strength 0..1 (the gain is raised to this power); None if that changes nothing."""
    if strength <= 0 or log_gain is None:
        return None
    gain = np.exp(log_gain.luma * np.float32(strength))
    if float(gain.max()) < 1.02:
        return None
    chroma_gain = np.exp(log_gain.chroma * np.float32(strength))
    keep = np.clip(256 * np.exp(log_gain.chroma * np.float32(-CHROMA_STRENGTH * strength)), 32, 255)   # 255: it is stored in a byte
    # Noise grows with the gain: from NR_START on, mix in the average of the neighbours (fully by NR_FULL).
    mix = np.clip((gain - NR_START) / (NR_FULL - NR_START), 0, 1) * 255
    mix_chroma = np.clip((chroma_gain - NR_START_CHROMA) / (NR_FULL_CHROMA - NR_START_CHROMA), 0, 1) * 255
    radius = max(2, round(gain.shape[1] / NR_PIXELS_PER_RADIUS))
    return Maps(np.clip(gain, 1.0, 255) * 256, keep, mix, mix_chroma, radius)


def build_maps(grid, strength, crop, size):
    """Maps in one go (for a single photo); see build_log_gain and maps_for."""
    if strength <= 0 or grid is None:
        return None
    return maps_for(build_log_gain(grid, crop, size), strength)


def apply(maps, array, size, stride, limited_range):
    """Brighten a YUV420 frame (the stream's mapped array, rows of `stride` bytes) in place."""
    lib = load()
    if lib is None or maps is None:
        return False
    w, h = size
    if array.ndim != 2 or array.shape[0] != h * 3 // 2 or array.shape[1] < w:
        return False   # not the YUV420 picture these maps were made for
    lo, hi = (16, 235) if limited_range else (0, 255)
    y = array[:h]
    u = array[h:h + h // 4].reshape(h // 2, stride // 2)
    v = array[h + h // 4:h + h // 2].reshape(h // 2, stride // 2)
    r = maps.radius
    bad = lib.boost_luma_nr(y.ctypes.data, stride, w, h, maps.gain.ctypes.data, maps.mix.ctypes.data, r, lo, hi)
    for plane in (u, v):
        bad |= lib.soften_chroma_nr(plane.ctypes.data, stride // 2, w // 2, h // 2, maps.keep.ctypes.data,
                                    maps.mix_chroma.ctypes.data, r)
    return bad == 0


def apply_rgb(maps, array, size):
    """Brighten an interleaved 3-channel picture (a BGR888 still, rows x width x 3) in place."""
    lib = load()
    w, h = size
    if lib is None or maps is None or array.ndim != 3 or array.shape[0] != h or array.shape[1] < w or not array.flags.c_contiguous:
        return False
    bad = lib.boost_rgb_nr(array.ctypes.data, array.strides[0], w, h, maps.gain.ctypes.data,
                           maps.mix.ctypes.data, maps.radius)
    lib.desaturate_rgb(array.ctypes.data, array.strides[0], w, h, maps.keep.ctypes.data, maps.keep.shape[1])
    return bad == 0


def _blur_fp(d, radius):
    """Average of the (2R+1)^2 neighbourhood (edges repeated) in 8.8 fixed point; d: int32 plane."""
    n = 2 * radius + 1
    p = np.pad(d, radius, mode="edge").astype(np.int64)
    for axis in (0, 1):
        c = np.cumsum(p, axis=axis)
        c = np.concatenate([np.zeros_like(np.take(c, [0], axis=axis)), c], axis=axis)
        p = np.take(c, range(n, c.shape[axis]), axis=axis) - np.take(c, range(0, c.shape[axis] - n), axis=axis)
    return (p.astype(np.float32) * np.float32(256.0 / (float(n) * float(n)))).astype(np.int32)


def _mixed(plane, lo, mix, radius):
    d = np.maximum(plane.astype(np.int32) - lo, 0)
    d_fp = d << 8
    blur = _blur_fp(d, radius)
    return np.maximum(d_fp + (((blur - d_fp) * mix.astype(np.int32)) >> 8), 0)


def apply_numpy(maps, y, u, v, limited_range):
    """The same correction as apply(), in numpy, for the offline tool (a PC does a frame in a fraction
    of a second). y, u, v: uint8 planes (u and v half size); changed in place."""
    lo, hi = (16, 235) if limited_range else (0, 255)
    m = _mixed(y, lo, maps.mix, maps.radius).astype(np.uint64)
    y[...] = np.minimum(((m * maps.gain.astype(np.uint64)) >> 16) + lo, hi).astype(np.uint8)
    for plane in (u, v):
        m = _mixed(plane, 0, maps.mix_chroma, maps.radius)
        plane[...] = np.clip(128 + (((m - 32768) * maps.keep.astype(np.int32)) >> 16), 0, 255).astype(np.uint8)
