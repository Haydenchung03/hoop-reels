"""Read the broadcast scoreboard (quarter + game clock) from a video, frame by frame.

This lets us line up NBA play-by-play events ("Q2 7:49 Curry 32' 3PT") with the exact
moment they appear in a highlight video. Text recognition uses Apple's Vision framework
(macOS only, via pyobjc). On other systems `available()` returns False and the app falls
back to player-specific YouTube videos.

Two passes:
  1. calibrate - OCR ~16 frames in full to find where this broadcast's scoreboard sits
                 (other games' tickers also show clocks, so position matters).
  2. scan      - OCR just that strip at 2 frames per second in fast mode.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Callable

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SCAN_FPS = 2

try:
    import objc
    import Quartz
    import Vision
    _HAVE_VISION = True
except Exception:  # not macOS, or pyobjc missing
    _HAVE_VISION = False


def available() -> bool:
    return _HAVE_VISION


# Scoreboards differ by broadcaster: "1ST 10:40" on one line, or the clock stacked above
# the quarter (TNT: "9:34" over "2ND"). So quarter and clock are found separately, anywhere
# in the scoreboard, and the closest pair wins.
_PERIOD = re.compile(r"(1ST|2ND|3RD|4TH|(?<![A-Z])[2-4]?OT(?![A-Z])|(?<![A-Z])Q[1-4](?!\d))")
_CLOCK = re.compile(r"(?<![\d.:])(\d{1,2}:\d{2}(?:\.\d)?|:?\d{1,2}\.\d)(?![\d:])")
# Bump when parsing changes so cached timelines are rebuilt.
TIMELINE_VERSION = 2


def _period_num(tok: str) -> int:
    if tok.startswith("Q"):
        return int(tok[1])
    if tok.endswith("OT"):
        return 4 + (int(tok[0]) if tok[0].isdigit() else 1)
    return int(tok[0])


def _clock_secs(tok: str) -> float | None:
    tok = tok.lstrip(":")
    if ":" in tok:
        m, s = tok.split(":")
        secs = int(m) * 60 + float(s)
    else:
        secs = float(tok)
    return secs if secs <= 12 * 60 else None


def parse_frame(obs: list, max_dist: float | None = 0.15) -> tuple[int, float, tuple] | None:
    """Find (period, clock_seconds, box) among one frame's text observations.

    `obs` is [(text, x, y, w, h), ...] normalised to the image. `box` covers both tokens.
    """
    periods, clocks = [], []
    for text, x, y, w, h in obs:
        t = text.upper()
        for m in _PERIOD.finditer(t):
            periods.append((_period_num(m.group(1)), (x, y, w, h)))
        for m in _CLOCK.finditer(t):
            secs = _clock_secs(m.group(1))
            if secs is not None:
                # mm:ss is always the game clock; a bare "4.3" could be the shot clock.
                clocks.append((secs, ":" in m.group(1).lstrip(":"), (x, y, w, h)))
    if not periods or not clocks:
        return None

    def centre(b):
        return b[0] + b[2] / 2, b[1] + b[3] / 2

    best = None
    for period, pbox in periods:
        px, py = centre(pbox)
        for secs, has_colon, cbox in clocks:
            if period > 4 and secs > 5 * 60:  # overtime is 5 minutes
                continue
            cx, cy = centre(cbox)
            dist = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
            # Prefer mm:ss, then the larger of two decimal clocks (game clock >= shot clock).
            key = (dist - 0.05 * has_colon, -secs)
            if best is None or key < best[0]:
                x0, y0 = min(pbox[0], cbox[0]), min(pbox[1], cbox[1])
                x1 = max(pbox[0] + pbox[2], cbox[0] + cbox[2])
                y1 = max(pbox[1] + pbox[3], cbox[1] + cbox[3])
                best = (key, dist, (period, secs, (x0, y0, x1 - x0, y1 - y0)))
    if best is None or (max_dist is not None and best[1] > max_dist):  # too far apart: not one scoreboard
        return None
    return best[2]


# --------------------------------------------------------------------------- OCR plumbing

def _ocr(buf: bytes, w: int, h: int, fast: bool) -> list[tuple[str, float, float, float, float]]:
    """Return (text, x, y, width, height) with a top-left origin, normalised 0-1."""
    with objc.autorelease_pool():
        cs = Quartz.CGColorSpaceCreateDeviceRGB()
        prov = Quartz.CGDataProviderCreateWithData(None, buf, len(buf), None)
        img = Quartz.CGImageCreate(
            w, h, 8, 32, w * 4, cs,
            Quartz.kCGBitmapByteOrder32Little | Quartz.kCGImageAlphaPremultipliedFirst,
            prov, None, False, Quartz.kCGRenderingIntentDefault,
        )
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(1 if fast else 0)  # 1 = fast, 0 = accurate
        req.setUsesLanguageCorrection_(False)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
        handler.performRequests_error_([req], None)
        out = []
        for obs in req.results() or []:
            bb = obs.boundingBox()
            x, y, bw, bh = bb.origin.x, bb.origin.y, bb.size.width, bb.size.height
            out.append((obs.topCandidates_(1)[0].string(), x, 1 - y - bh, bw, bh))
        return out


def _frames(path: Path, vf: str, w: int, h: int, ss: float | None = None, count: int | None = None):
    args = [FFMPEG, "-v", "error"]
    if ss is not None:
        args += ["-ss", f"{ss:.2f}"]
    args += ["-i", str(path), "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgra"]
    if count:
        args += ["-frames:v", str(count)]
    args.append("-")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE)
    size = w * h * 4
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield buf
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


# --------------------------------------------------------------------------- passes

def _calibrate(path: Path, duration: float) -> tuple[int, int, int, int] | None:
    """Find the scoreboard's pixel box (x, y, w, h) in 1280x720 space."""
    W, H = 1280, 720
    hits = []
    n = 16
    for i in range(n):
        t = duration * (0.05 + 0.9 * i / (n - 1))
        for buf in _frames(path, f"scale={W}:{H}", W, H, ss=t, count=1):
            hit = parse_frame(_ocr(buf, W, H, fast=False))
            if hit:
                hits.append(hit[2])
    if len(hits) < 3:
        return None
    # The broadcast's own scoreboard sits in the same place every time; tickers move around.
    bucket = Counter((round(x * 10), round(y * 10)) for x, y, _, _ in hits).most_common(1)[0][0]
    same = [h for h in hits if (round(h[0] * 10), round(h[1] * 10)) == bucket]
    x0 = min(h[0] for h in same)
    y0 = min(h[1] for h in same)
    x1 = max(h[0] + h[2] for h in same)
    y1 = max(h[1] + h[3] for h in same)
    # Pad generously: the text grows/shrinks ("1ST 10:40" vs "4TH :29.7") and some videos shift.
    pad_x, pad_y = 0.12, 0.03
    x0, x1 = max(0.0, x0 - pad_x), min(1.0, x1 + pad_x)
    y0, y1 = max(0.0, y0 - pad_y), min(1.0, y1 + pad_y)
    px = lambda v, scale: int(round(v * scale / 2)) * 2  # even numbers for ffmpeg
    return px(x0, W), px(y0, H), max(16, px(x1 - x0, W)), max(16, px(y1 - y0, H))


def read_timeline(path: Path, duration: float, report: Callable[[float], None] | None = None) -> list:
    """Return [(video_seconds, period, clock_seconds), ...]; empty if no scoreboard was found.

    Cached next to the video as <name>.timeline.json.
    """
    cache = path.with_suffix(".timeline.json")
    if cache.exists():
        data = json.loads(cache.read_text())
        if data.get("version") == TIMELINE_VERSION:
            return [tuple(r) for r in data["rows"]]
    if not available():
        return []

    box = _calibrate(path, duration)
    rows = []
    if box:
        x, y, w, h = box
        scale = 2  # upscale the strip: small text reads better
        vf = f"fps={SCAN_FPS},scale=1280:720,crop={w}:{h}:{x}:{y},scale={w * scale}:{h * scale}"
        total = max(1, int(duration * SCAN_FPS))
        for i, buf in enumerate(_frames(path, vf, w * scale, h * scale)):
            t = i / SCAN_FPS
            hit = parse_frame(_ocr(buf, w * scale, h * scale, fast=True), max_dist=None)
            if hit:
                rows.append((t, hit[0], hit[1]))
            if report and i % 10 == 0:
                report(min(1.0, i / total))

    cache.write_text(json.dumps({"version": TIMELINE_VERSION, "box": box, "rows": rows}))
    return rows


# --------------------------------------------------------------------------- matching

def segments(rows: list) -> list[dict]:
    """Group readings into continuous stretches of game action.

    A highlight video jumps from play to play; inside one play the clock runs down
    in step with the video (or stands still). Anything else starts a new segment.
    """
    segs: list[dict] = []
    cur = None
    for t, period, clock in rows:
        if cur:
            pt, pp, pc = cur["rows"][-1]
            dt = t - pt
            drop = pc - clock  # how much game clock passed
            continuous = (
                period == pp and dt <= 3.0
                and -0.6 <= drop <= dt + 1.5  # clock running (≈ dt) or stopped (0)
            )
            if continuous:
                cur["rows"].append((t, period, clock))
                continue
        cur = {"rows": [(t, period, clock)]}
        segs.append(cur)
    for s in segs:
        s["start"], s["end"] = s["rows"][0][0], s["rows"][-1][0]
        s["period"] = s["rows"][0][1]
        s["clock_hi"], s["clock_lo"] = s["rows"][0][2], s["rows"][-1][2]
    return [s for s in segs if len(s["rows"]) >= 2]  # single readings are usually OCR noise


def locate(segs: list[dict], period: int, clock: float) -> tuple[float, dict] | None:
    """Video time at which the game clock reads `clock` in `period`, and its segment."""
    for s in segs:
        if s["period"] != period:
            continue
        if not (s["clock_lo"] - 1.0 <= clock <= s["clock_hi"] + 1.0):
            continue
        prev = None
        for t, _, c in s["rows"]:
            if c <= clock + 0.25:
                if prev and prev[1] > clock:
                    # interpolate between readings while the clock is running
                    return min(t, prev[0] + (prev[1] - clock)), s
                return t, s
            prev = (t, c)
        return s["end"], s
    return None
