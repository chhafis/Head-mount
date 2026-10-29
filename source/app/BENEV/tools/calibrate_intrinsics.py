#!/usr/bin/env python3
"""Lens intrinsics of a head mount: focal length, optical centre, distortion and field of view.

Run on a PC (pip install opencv-python-headless numpy) with 15-30 photos of a printed checkerboard
taken with the head mount's own lens ("Take photo" on the Device page; full 1920x1200 pictures,
board held at different distances and angles, filling the picture including its corners and edges,
sharp, with no edge brightening needed). Then:

    python calibrate_intrinsics.py PHOTO_FOLDER --corners 9x6 --square-mm 25 -o lens-intrinsics.json

--corners is the number of INNER corners (columns x rows) of the board. The tool fits OpenCV's
fisheye model (equidistant projection with four distortion terms), prints the reprojection error and
the field of view, and writes lens-intrinsics.json. Copy that file to the head mount as
~/BENEV/lens-intrinsics.json (for example with scp): every recording then contains a copy of it
(lens_intrinsics.json) and its field of view in session.json.

The picture must not be rotated or cropped after taking it. The numbers are for the 1920x1200 full
frame; a 1920x1080 recording is that frame with 120 lines removed (Camera 1: from the top), so the
principal point in a 1080 recording is cy - 120 (Camera 1) or cy - 60 (Camera 2).
"""
import argparse
import glob
import json
import math
import os
import re
import sys

import numpy as np


def ray_angle_degrees(K, D, p, q):
    """Angle in degrees between the viewing rays of two pixels, from the fisheye model:
    r_d = theta * (1 + k1 theta^2 + k2 theta^4 + k3 theta^6 + k4 theta^8), solved for theta by Newton's method."""
    def ray(px):
        x, y = (px[0] - K[0][2]) / K[0][0], (px[1] - K[1][2]) / K[1][1]
        r_d = math.hypot(x, y)
        theta = r_d
        for _ in range(30):
            t2 = theta * theta
            f = theta * (1 + D[0] * t2 + D[1] * t2 ** 2 + D[2] * t2 ** 3 + D[3] * t2 ** 4) - r_d
            df = 1 + 3 * D[0] * t2 + 5 * D[1] * t2 ** 2 + 7 * D[2] * t2 ** 3 + 9 * D[3] * t2 ** 4
            theta -= f / df
        t2 = theta * theta
        fit = theta * (1 + D[0] * t2 + D[1] * t2 ** 2 + D[2] * t2 ** 3 + D[3] * t2 ** 4)
        slope = 1 + 3 * D[0] * t2 + 5 * D[1] * t2 ** 2 + 7 * D[2] * t2 ** 3 + 9 * D[3] * t2 ** 4
        if abs(fit - r_d) > 1e-6 or slope <= 0 or not 0 <= theta <= math.pi:
            return None   # outside what the lens can see (or beyond where the model is valid)
        phi = math.atan2(y, x)
        return np.array([math.sin(theta) * math.cos(phi), math.sin(theta) * math.sin(phi), math.cos(theta)])
    a, b = ray(p), ray(q)
    if a is None or b is None:
        return None
    return round(math.degrees(math.acos(max(-1.0, min(1.0, float(a @ b))))), 1)


def field_of_view(K, D, size, circle_px=None):
    """Field of view in degrees across the frame (horizontal and vertical through the optical centre,
    and corner to corner); None where the frame is outside the lens's image circle. With circle_px (the
    diameter of the lit image circle) also the full angle of the image circle."""
    w, h = size
    cx, cy = K[0][2], K[1][2]
    out = {
        "fov_horizontal_deg": ray_angle_degrees(K, D, (0, cy), (w - 1, cy)),
        "fov_vertical_deg": ray_angle_degrees(K, D, (cx, 0), (cx, h - 1)),
        "fov_diagonal_deg": ray_angle_degrees(K, D, (0, 0), (w - 1, h - 1)),
    }
    if circle_px:
        r = circle_px / 2
        out["fov_image_circle_deg"] = ray_angle_degrees(K, D, (cx - r, cy), (cx + r, cy))
    return out


def calibrate(object_points, image_points, size, circle_px=None):
    """OpenCV fisheye calibration -> dict. object_points: list of (N,1,3) float32; image_points: (N,1,2)."""
    import cv2
    K = np.zeros((3, 3))
    D = np.zeros((4, 1))
    # OpenCV 4 keeps these constants in cv2.fisheye, OpenCV 5 in cv2
    flag = lambda name: getattr(cv2.fisheye, name, None) or getattr(cv2, name)
    flags = flag("CALIB_RECOMPUTE_EXTRINSIC") | flag("CALIB_FIX_SKEW") | flag("CALIB_CHECK_COND")
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-9)
    object_points, image_points = list(object_points), list(image_points)
    while True:
        try:
            rms, K, D, _, _ = cv2.fisheye.calibrate(object_points, image_points, size, K, D, flags=flags, criteria=criteria)
            break
        except cv2.error as e:   # "Ill-conditioned matrix for input array N": drop that picture and try again
            m = re.search(r"input array (\d+)", str(e))
            if not m or len(object_points) <= 8:
                raise
            print(f"  dropping picture {m.group(1)} (the board there is badly placed)")
            del object_points[int(m.group(1))], image_points[int(m.group(1))]
            K, D = np.zeros((3, 3)), np.zeros((4, 1))
    D = D.ravel().tolist()
    # The model is only trusted where the board was seen: say so if the frame edges lie beyond that.
    seen = max(math.hypot(u - K[0][2], v - K[1][2]) for pts in image_points for u, v in np.asarray(pts).reshape(-1, 2))
    edges = max(math.hypot(x - K[0][2], y - K[1][2]) for x, y in ((0, 0), (size[0] - 1, 0), (0, size[1] - 1), (size[0] - 1, size[1] - 1)))
    note = None
    if edges > 1.05 * seen:
        note = (f"the pictures cover the frame only up to {seen:.0f} px from the optical centre; the frame edges are "
                f"{edges:.0f} px away, so the field of view there is extrapolated. Hold the board closer and at an angle "
                "so that it also reaches the edges and corners.")
    return {"fov_note": note, "model": "opencv_fisheye (equidistant, theta_d = theta*(1+k1 theta^2+k2 theta^4+k3 theta^6+k4 theta^8))",
            "image_size": [size[0], size[1]], "K": K.tolist(), "D": D, "rms_error_px": round(float(rms), 3),
            **field_of_view(K.tolist(), D, size, circle_px)}


def find_corners(folder, pattern, square_mm):
    import cv2
    cols, rows = pattern
    grid = np.zeros((1, cols * rows, 3), np.float32)
    grid[0, :, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square_mm
    objp, imgp, used, size = [], [], [], None
    files = sorted(sum((glob.glob(os.path.join(folder, e)) for e in ("*.jpg", "*.jpeg", "*.png")), []))
    for f in files:
        img = cv2.imread(f, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        size = (img.shape[1], img.shape[0])
        found, corners = cv2.findChessboardCorners(
            img, (cols, rows), cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE + cv2.CALIB_CB_FAST_CHECK)
        if not found:
            print(f"  {os.path.basename(f)}: board not found")
            continue
        corners = cv2.cornerSubPix(img, corners, (7, 7), (-1, -1),
                                   (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 1e-3))
        objp.append(grid.copy())
        imgp.append(corners.reshape(1, -1, 2).astype(np.float32))
        used.append(os.path.basename(f))
    return objp, imgp, used, size


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--corners", default="9x6", help="inner corners, columns x rows (default 9x6)")
    ap.add_argument("--square-mm", type=float, default=25.0)
    ap.add_argument("--circle-px", type=float, default=None,
                    help="diameter of the lit image circle in pixels (the lens calibration reports how much of the "
                         "picture is dark); adds fov_image_circle_deg, the lens's own field of view")
    ap.add_argument("-o", "--output", default="lens-intrinsics.json")
    args = ap.parse_args()
    cols, rows = (int(v) for v in args.corners.lower().split("x"))
    objp, imgp, used, size = find_corners(args.folder, (cols, rows), args.square_mm)
    print(f"board found in {len(used)} pictures")
    if len(used) < 10:
        sys.exit("need at least 10 pictures with the whole board visible (15-30 is better)")
    result = calibrate(objp, imgp, size, args.circle_px)
    result["pictures_used"] = len(used)
    result["board"] = {"inner_corners": [cols, rows], "square_mm": args.square_mm}
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"RMS reprojection error {result['rms_error_px']} px (below 1 px is good)")
    show = lambda v: "n/a (outside the image circle)" if v is None else f"{v} deg"
    print(f"field of view of the frame: horizontal {show(result['fov_horizontal_deg'])}, "
          f"vertical {show(result['fov_vertical_deg'])}, corner to corner {show(result['fov_diagonal_deg'])}")
    if "fov_image_circle_deg" in result:
        print(f"field of view of the image circle: {show(result['fov_image_circle_deg'])}")
    if result.get("fov_note"):
        print("NOTE:", result["fov_note"])
    print(f"written {args.output}; copy it to the head mount as ~/BENEV/lens-intrinsics.json")


if __name__ == "__main__":
    main()
