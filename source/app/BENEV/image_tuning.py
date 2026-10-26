"""Image (ISP) settings for recording: exposure limit, sharpening, contrast, colour, metering.

Two parts, both driven by the same settings dict (saved as "image" in camera.json):
  - apply_to_tuning(): edits the sensor's libcamera tuning file (camera.py writes the copy):
    the "custom" exposure mode, and whether the ISP's adaptive contrast enhancement runs.
  - controls(): per-frame ISP controls the camera worker applies (names as in libcamera).

Exposure. Auto-exposure follows a staircase (libcamera exposure modes): the exposure time rises
to each step at the previous gain, then the gain rises at that fixed time. The sensor's stock
"normal" mode lets the exposure time reach 30 ms before it raises the gain, which smears
anything that moves (a head turning at 100 deg/s moves the 170 degree picture ~11 pixels per
10 ms). The "custom" mode here holds the exposure time at max_exposure_ms and raises the gain
instead, and only beyond max_gain lets the exposure time grow again, up to the frame time.

Flicker. Lamps on AC mains flicker at twice the mains frequency (100 Hz on 50 Hz mains). An
exposure that is a whole number of flicker periods (10 ms, 20 ms) sees every frame the same
brightness; a shorter one can pulse from frame to frame under cheap LED and fluorescent lamps.
With mains_hz set, auto-exposure rounds exposures of one period or more down to whole
periods, which is why the default limit is exactly one period (10 ms).
"""

DEFAULTS = {
    "max_exposure_ms": 10.0,    # exposure time kept at or below this while the gain can still rise
    "max_gain": 8.0,            # gain reached before the exposure time may exceed max_exposure_ms
    "sharpness": 1.0,           # ISP sharpening strength (0 = off, 1 = the tuning's default)
    "contrast": 1.0,
    "saturation": 1.0,
    "ev": 0.0,                  # exposure compensation, stops
    "mains_hz": 50,             # mains frequency, for flicker-free exposures; 0 = off
    "metering": "matrix",       # centre-weighted | matrix | spot
    "highlight_protect": False, # also keep bright areas (windows, lamps) from clipping
    "contrast_enhance": True,   # the ISP's adaptive contrast enhancement
    "edge_boost": 0.0,          # extra gain for edges the lens leaves dark (edge_boost.py); 0 = off
}
RANGES = {"max_exposure_ms": (1.0, 33.0), "max_gain": (1.0, 16.0), "sharpness": (0.0, 4.0), "edge_boost": (0.0, 1.0),
          "contrast": (0.5, 2.0), "saturation": (0.0, 2.0), "ev": (-2.0, 2.0)}
CHOICES = {"metering": ("centre-weighted", "matrix", "spot"), "mains_hz": (0, 50, 60)}
BOOLEANS = ("highlight_protect", "contrast_enhance")


def validate(changes):
    """-> the checked subset of settings; raises ValueError / KeyError."""
    out = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise KeyError(key)
        if key in BOOLEANS:
            out[key] = bool(value)
        elif key in CHOICES:
            if isinstance(value, bool) or value not in CHOICES[key]:
                raise ValueError(f"{key} must be one of {', '.join(str(c) for c in CHOICES[key])}")
            out[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a number")
            lo, hi = RANGES[key]
            if not lo <= value <= hi:
                raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
            out[key] = float(value)
    return out


def merged(saved):
    cfg = dict(DEFAULTS)
    try:
        cfg.update(validate(saved or {}))
    except (ValueError, KeyError):
        pass   # a bad saved value must not stop the camera: fall back to the defaults
    return cfg


def exposure_mode(cfg, frame_ms):
    """The "custom" exposure mode: shutter times in microseconds and the gain at each step."""
    longest = frame_ms * 1000 * 0.95                # an exposure must fit in the frame
    cap = min(cfg["max_exposure_ms"] * 1000, longest)
    top = cfg["max_gain"]
    gains = [g for g in (1.0, 2.0, 4.0, 6.0, 8.0, 12.0, 16.0) if g < top] + [top]
    shutter = [100.0] + [cap] * len(gains)          # time to cap at gain 1, then gain rises at cap
    gain = [1.0] + gains
    # Out of gain: let the exposure time grow again, but never past the frame time.
    shutter.append(longest)
    gain.append(top)
    return {"shutter": [round(s) for s in shutter], "gain": gain}


def apply_to_tuning(tuning, cfg, frame_ms=33.3):
    """Edit a parsed tuning file in place."""
    for algo in tuning["algorithms"]:
        if "rpi.agc" in algo:
            algo["rpi.agc"].setdefault("exposure_modes", {})["custom"] = exposure_mode(cfg, frame_ms)
        elif "rpi.contrast" in algo:
            algo["rpi.contrast"]["ce_enable"] = 1 if cfg["contrast_enhance"] else 0


def controls(cfg):
    """libcamera control values for the worker (enum members are given by name)."""
    flicker = {}
    if cfg["mains_hz"]:
        flicker = {"AeFlickerMode": "Manual", "AeFlickerPeriod": round(1e6 / (2 * cfg["mains_hz"]))}
    return {
        **flicker,
        "AeExposureMode": "Custom",
        "AeMeteringMode": {"centre-weighted": "CentreWeighted", "matrix": "Matrix", "spot": "Spot"}[cfg["metering"]],
        "AeConstraintMode": "Highlight" if cfg["highlight_protect"] else "Normal",
        "ExposureValue": cfg["ev"],
        "Sharpness": cfg["sharpness"],
        "Contrast": cfg["contrast"],
        "Saturation": cfg["saturation"],
    }
