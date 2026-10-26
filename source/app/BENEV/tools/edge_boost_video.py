#!/usr/bin/env python3
"""Brighten the dark edges of a recorded video, afterwards (offline).

The camera cannot do this live at 1080p30 (it costs more than a frame's time on a Pi Zero 2 W), so a
recording keeps the camera's original picture. Each recording folder also holds edge_gain.json, the
lens's extra-gain map; this applies it to the video. Run it on a PC (about 1 minute per minute of
video) or on the Pi (much slower).

    pip install av numpy pillow
    python edge_boost_video.py RECORDING_FOLDER                  -> RECORDING_FOLDER/video_edge.mp4
    python edge_boost_video.py video.mp4 --map edge_gain.json --strength 0.8 -o out.mp4

--strength 0..1 raises the gain to that power (1 = the full map). Parts the lens never lit stay black:
the map fades out there instead of amplifying noise. The number, order and timing of the frames do not
change, so frames.csv and the sensor files still line up.
"""
import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import edge_boost  # noqa: E402


def crop_for(width, height):
    """The part of the 1920x1200 sensor frame a recorded picture of this size shows."""
    return (0, 60, 1920, 1080) if abs(width / height - 16 / 9) < 0.02 else (0, 0, 1920, 1200)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", help="a recording folder, or a video file")
    ap.add_argument("--map", help="edge_gain.json (default: the one in the recording folder)")
    ap.add_argument("--strength", type=float, default=1.0)
    ap.add_argument("-o", "--output")
    args = ap.parse_args()

    src = Path(args.source)
    video = next(iter(sorted(src.glob("video.*"))), None) if src.is_dir() else src
    if video is None:
        sys.exit(f"no video.* in {src}")
    map_file = Path(args.map) if args.map else (src if src.is_dir() else src.parent) / "edge_gain.json"
    if not map_file.is_file():
        sys.exit(f"{map_file} not found: this recording was made without a lens calibration with an edge map")
    grid = np.array(json.loads(map_file.read_text())["grid"], dtype=np.float32)
    out_path = Path(args.output) if args.output else video.with_name(video.stem + "_edge.mp4")

    with av.open(str(video)) as inp:
        vs = inp.streams.video[0]
        w, h = vs.codec_context.width, vs.codec_context.height
        limited = getattr(vs.codec_context, "color_range", 1) != 2          # 2 = full (JPEG) range
        maps = edge_boost.maps_for(edge_boost.build_log_gain(grid, crop_for(w, h), (w, h)), args.strength)
        if maps is None:
            sys.exit("nothing to brighten at this strength")
        rate = vs.average_rate or Fraction(30)
        print(f"{video.name}: {w}x{h}, {float(rate):.2f} fps, brightening up to x{maps.max_gain:.1f} -> {out_path.name}")
        with av.open(str(out_path), "w") as out:
            ov = out.add_stream("libx264", rate=rate)
            ov.width, ov.height, ov.pix_fmt = w, h, "yuv420p"
            ov.options = {"crf": "16", "preset": "medium"}
            n = 0
            for frame in inp.decode(video=0):
                arr = frame.to_ndarray(format="yuv420p")
                y = arr[:h]
                u = arr[h:h + h // 4].reshape(h // 2, w // 2)
                v = arr[h + h // 4:].reshape(h // 2, w // 2)
                edge_boost.apply_numpy(maps, y, u, v, limited)
                for pkt in ov.encode(av.VideoFrame.from_ndarray(arr, format="yuv420p")):
                    out.mux(pkt)
                n += 1
                if n % 100 == 0:
                    print(f"  {n} frames", flush=True)
            for pkt in ov.encode():
                out.mux(pkt)
    print(f"done: {n} frames")


if __name__ == "__main__":
    main()
