"""Lens shading calibration for the Pi ISP (libcamera "rpi.alsc"), from raw flat-field frames.

The 170° lens makes the image darker and tinted towards its edges. The ISP corrects that with
three 16x12 gain tables over the sensor: luminance (the fall-off) and Cr / Cb (the colour shift,
as gains for red and blue relative to green). This computes them the way Raspberry Pi's tuning
tool does (libcamera utils/tuning/libtuning/modules/lsc: ALSCRaspberryPi): per-cell channel
means minus black level, 1/G normalised to a minimum of 1 for luminance, G/R and G/B for colour,
each smoothed with a 3x3 median and clipped to the ISP's range (< 4).

The input must be a flat field: a white diffuser right in front of the lens (with 170° nothing
else fills the view evenly), lit evenly, not over-exposed.
"""
import numpy as np

GRID = (16, 12)              # vc4 ALSC cells (columns, rows)
MAX_GAIN = 3.999             # largest gain the tables may hold
DEFAULT_STRENGTH = 1.0       # share of the luminance fall-off corrected (1 = fully flat; edges get noisier)
RELIABLE_FRACTION = 0.06     # cells dimmer than this share of the brightest are noise: their colour is filled in


def unpack_raw(array, fmt, width, height):
    """Picamera2 raw array -> (uint16 Bayer image, bit depth). Handles CSI-2 packed 10/12-bit
    (e.g. SGRBG10_CSI2P) and unpacked 16-bit containers (e.g. SGRBG10)."""
    fmt = str(fmt)
    depth = int("".join(ch for ch in fmt[5:7] if ch.isdigit()) or 16)
    if fmt.endswith("_CSI2P"):
        raw = np.asarray(array, dtype=np.uint8)
        if depth == 10:
            b = raw[:, : width * 5 // 4].reshape(height, width // 4, 5).astype(np.uint16)
            out = np.empty((height, width // 4, 4), np.uint16)
            for i in range(4):
                out[:, :, i] = (b[:, :, i] << 2) | ((b[:, :, 4] >> (2 * i)) & 3)
        elif depth == 12:
            b = raw[:, : width * 3 // 2].reshape(height, width // 2, 3).astype(np.uint16)
            out = np.empty((height, width // 2, 2), np.uint16)
            out[:, :, 0] = (b[:, :, 0] << 4) | (b[:, :, 2] & 0xF)
            out[:, :, 1] = (b[:, :, 1] << 4) | (b[:, :, 2] >> 4)
        else:
            raise ValueError(f"unsupported packed raw format {fmt}")
        return out.reshape(height, width), depth
    return np.asarray(array).view(np.uint16)[:height, :width], depth


def split_bayer(img, fmt):
    """Bayer image -> {"R", "Gr", "Gb", "B"} half-resolution planes. The order comes from the
    format name (S<order><depth>...), which already reflects any sensor flips."""
    order = str(fmt)[1:5]
    if sorted(order) != ["B", "G", "G", "R"]:
        raise ValueError(f"not a colour Bayer format: {fmt}")
    planes = {}
    for i, (y, x) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        c = order[i]
        if c == "G":   # the green on the red row is Gr
            c = "Gr" if "R" in order[(i // 2) * 2:(i // 2) * 2 + 2] else "Gb"
        planes[c] = img[y::2, x::2]
    return planes


def _sector_bounds(n, k):
    """Split n pixels into k sectors, the remainder going to the first ones (libtuning's
    Linear gradient with Remainder.DistributeFront)."""
    sizes = [n // k + (1 if i < n % k else 0) for i in range(k)]
    return np.concatenate([[0], np.cumsum(sizes)])


def grid_means(plane):
    xs = _sector_bounds(plane.shape[1], GRID[0])
    ys = _sector_bounds(plane.shape[0], GRID[1])
    out = np.empty((GRID[1], GRID[0]))
    for r in range(GRID[1]):
        for c in range(GRID[0]):
            out[r, c] = plane[ys[r]:ys[r + 1], xs[c]:xs[c + 1]].mean()
    return out


def _median3(t):
    p = np.pad(t, 1, mode="edge")
    stack = [p[dy:dy + t.shape[0], dx:dx + t.shape[1]] for dy in range(3) for dx in range(3)]
    return np.median(np.stack(stack), axis=0)


def _fill_unreliable(table, reliable):
    """Replace table values where reliable is False by the average of the nearest reliable ones,
    spreading outwards ring by ring (near-black corner cells hold only noise)."""
    t = table.copy()
    ok = reliable.copy()
    rows, cols = t.shape
    if not ok.any():
        return t
    while not ok.all():
        new_vals = {}
        for r in range(rows):
            for c in range(cols):
                if ok[r, c]:
                    continue
                near = [t[rr, cc] for rr in range(max(r - 1, 0), min(r + 2, rows))
                        for cc in range(max(c - 1, 0), min(c + 2, cols)) if ok[rr, cc]]
                if near:
                    new_vals[(r, c)] = float(np.mean(near))
        for (r, c), v in new_vals.items():
            t[r, c], ok[r, c] = v, True
    return t


def compute_alsc(planes, black_levels, colour_temp, luminance_strength=DEFAULT_STRENGTH):
    """planes: {"R","Gr","Gb","B"} (any common scale); black_levels: same scale, in that order.
    -> (rpi.alsc dict for the tuning file, statistics for the user)."""
    blk = dict(zip(("R", "Gr", "Gb", "B"), black_levels))
    grids = {k: np.maximum(grid_means(np.asarray(v, dtype=np.float64)) - blk[k], 0.1) for k, v in planes.items()}
    g = (grids["Gr"] + grids["Gb"]) / 2
    lum = _median3(1 / g)
    lum = lum / lum.min()
    reliable = g > RELIABLE_FRACTION * g.max()
    cr = _median3(_fill_unreliable(g / grids["R"], reliable))
    cb = _median3(_fill_unreliable(g / grids["B"], reliable))

    def table(t):
        return [round(float(v), 3) for v in np.clip(t, 0, MAX_GAIN).flatten()]

    ct = int(round(colour_temp or 5000))
    alsc = {
        "omega": 1.3, "n_iter": 100, "luminance_strength": luminance_strength,
        "calibrations_Cr": [{"ct": ct, "table": table(cr)}],
        "calibrations_Cb": [{"ct": ct, "table": table(cb)}],
        "luminance_lut": table(lum),
        # Only one colour temperature measured: Raspberry Pi's defaults for the adaptive part.
        "sigma": 0.005, "sigma_Cb": 0.005,
    }
    rg, bg = grids["R"] / g, grids["B"] / g
    centre = (slice(GRID[1] // 2 - 1, GRID[1] // 2 + 1), slice(GRID[0] // 2 - 1, GRID[0] // 2 + 1))
    corners = [(0, 0), (0, -1), (-1, 0), (-1, -1)]
    stats = {
        "colour_temp_k": ct,
        "corner_brightness_pct": round(float(np.mean([g[c] for c in corners]) / g[centre].mean() * 100), 1),
        "max_luminance_gain": round(float(min(lum.max(), MAX_GAIN)), 2),
        # How far the edges' colour is from the centre's (the "hue"), before correction
        "red_shift_pct": round(float((np.max(np.abs(rg / rg[centre].mean() - 1))) * 100), 1),
        "blue_shift_pct": round(float((np.max(np.abs(bg / bg[centre].mean() - 1))) * 100), 1),
        "clipped_cells": int((lum > MAX_GAIN).sum() + (cr > MAX_GAIN).sum() + (cb > MAX_GAIN).sum()),
        # Share of the picture that needs more than the ISP's 4x brightening: it stays dark.
        "dark_area_pct": round(float((lum > MAX_GAIN).mean() * 100), 1),
        "unevenness_pct": unevenness_pct(g),
    }
    # The measured grids, for checking a calibration by hand (not saved with the correction)
    stats["_grids"] = {"green": np.round(g, 1).tolist(), "red_over_green": np.round(rg, 3).tolist(),
                       "blue_over_green": np.round(bg, 3).tolist()}
    return alsc, stats


EDGE_GRID = (20, 32)         # rows, columns of the edge gain map (60 px cells on a 1920x1200 frame)
EDGE_MAX_GAIN = 8.0          # most extra gain the edge map asks for
EDGE_NO_LIGHT = 0.04         # below this share of the white level there is no picture to brighten


def edge_gain_grid(y):
    """Extra gain still needed after the ISP's lens shading correction, from a flat-field luma
    picture (2-D array, the ISP's output, with the correction active). -> (rows, cols) gains >= 1.

    Each cell is brought up to the level of the middle of the picture, at most EDGE_MAX_GAIN.
    Where a cell is nearly black (below EDGE_NO_LIGHT of the white level) the lens delivers no
    light, so the gain fades back to 1 there: amplifying it would only show noise."""
    y = np.asarray(y, dtype=np.float64)
    h, w = y.shape
    rows, cols = EDGE_GRID
    plateau = float(np.percentile(y[h // 4:3 * h // 4, w // 4:3 * w // 4], 60))
    if plateau < 10:
        raise ValueError("the picture is too dark to measure")
    cells = y[: h // rows * rows, : w // cols * cols].reshape(rows, h // rows, cols, w // cols).mean(axis=(1, 3))
    rel = _median3(_median3(cells)) / plateau
    gain = np.clip(1 / np.maximum(rel, 1e-3), 1.0, EDGE_MAX_GAIN)
    fade = np.clip((rel - EDGE_NO_LIGHT) / EDGE_NO_LIGHT, 0.0, 1.0)
    return np.round(1 + (gain - 1) * fade, 3)


UNEVEN_PROBLEM_PCT = 20.0    # a patch this far from its surroundings: not a plain cover (thresholds: see tests)
UNEVEN_WARNING_PCT = 10.0


def assess(stats):
    """-> (problems that make the measurement unusable, warnings). Thresholds from synthetic
    tests: a smooth fall-off fits within ~3 %, one bright cell in view shows as ~20 %."""
    problems, warnings = [], []
    if stats["saturated_pct"] > 1:
        problems.append(f"Over-exposed ({stats['saturated_pct']} % of the image is clipped): add a layer "
                        "to the diffuser or use less light.")
    if stats["centre_level_pct"] < 8:
        problems.append(f"Too dark (centre at {stats['centre_level_pct']} % of full scale): use more light.")
    if stats["unevenness_pct"] > UNEVEN_PROBLEM_PCT:
        problems.append(f"The view is not an even white (a patch is {stats['unevenness_pct']} % brighter or darker than "
                        "its surroundings): the diffuser must cover the whole lens, with nothing else in view and even light.")
    elif stats["unevenness_pct"] > UNEVEN_WARNING_PCT:
        warnings.append(f"The light was a little uneven ({stats['unevenness_pct']} %); for the best result, "
                        "use a more even light or a thicker diffuser.")
    if stats["dark_area_pct"] >= 1:
        warnings.append(f"{stats['dark_area_pct']} % of the picture (its corners and edges, down to "
                        f"{stats['corner_brightness_pct']} % of the centre's brightness) is darker than the image "
                        "processor can lift (limit 4×), so it stays dark. The lens does not cover the whole sensor: "
                        "crop the picture or use a lens with a larger image circle.")
    return problems, warnings


def unevenness_pct(g):
    """How far the most out-of-line cell is from a smooth fall-off, in %.

    A plain cover under even light gives a smooth bowl in log brightness, which a 4th-order
    surface follows closely. A shadow, a bright patch or a gap in the diffuser does not fit.
    Cells below 10 % of the peak are left out (noise, and the log makes them dominate)."""
    rows, cols = g.shape
    valid = g > 0.10 * g.max()
    if valid.sum() < 40:
        return 0.0
    yy, xx = np.mgrid[0:rows, 0:cols]
    x = (xx - (cols - 1) / 2) / cols
    y = (yy - (rows - 1) / 2) / rows
    terms = np.stack([(x ** i * y ** j)[valid] for i in range(5) for j in range(5 - i)], axis=1)
    z = np.log(g[valid])
    coef, *_ = np.linalg.lstsq(terms, z, rcond=None)
    return round(float(np.max(np.abs(np.expm1(z - terms @ coef))) * 100), 1)
