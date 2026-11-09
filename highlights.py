"""Find a highlight video for a game on YouTube, download it, and cut a short reel.

Pipeline:
  1. search   - yt-dlp searches YouTube; titles are scored on team names, date, player name.
  2. download - best stream at up to 720p, merged to mp4.
  3. analyze  - audio loudness over time; crowd roars mark the big plays.
  4. render   - the loudest moments are cut and joined with short fades (16:9 or 9:16).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import imageio_ffmpeg
import numpy as np
from yt_dlp import YoutubeDL

from nba_data import team_info

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SOURCE_DIR = DATA_DIR / "sources"
OUTPUT_DIR = DATA_DIR / "output"
for d in (SOURCE_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

SEGMENT_SECONDS = 7.5   # length of each clip in the reel
LEAD_IN = 5.0           # seconds kept before a loudness peak (the play builds up before the roar)
MAX_SOURCE_SECONDS = 30 * 60  # skip full-game replays
BAD_WORDS = ("reaction", "2k", "simulation", "prediction", "preview", "live stream", "livestream",
             "full game replay", "podcast", "breakdown", "fantasy", "betting", "picks")

Report = Callable[[str, float, str], None]  # (stage, percent, message)


class HighlightError(Exception):
    def __init__(self, message: str, candidates: list[dict] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


@dataclass
class Request:
    player_id: int
    player_name: str
    game_id: str
    date: str          # YYYY-MM-DD
    team: str          # player's team abbr
    opponent: str      # opponent abbr
    home: bool
    pts: int | None
    series_game: int | None = None  # playoff game number in the series (1-7)
    focus: str = "game"        # "game" or "player"
    length: int = 60           # seconds
    fmt: str = "landscape"     # "landscape" or "vertical"
    video_id: str | None = None  # force a specific YouTube video

    teams: dict = field(init=False)

    def __post_init__(self):
        me, opp = team_info(self.team), team_info(self.opponent)
        self.teams = {"me": me, "opp": opp,
                      "home": me if self.home else opp,
                      "away": opp if self.home else me}

    @property
    def dt(self) -> datetime:
        return datetime.strptime(self.date, "%Y-%m-%d")

    @property
    def cache_key(self) -> str:
        who = self.player_id if self.focus == "player" else "game"
        src = self.video_id or "auto"
        return f"{self.game_id}_{who}_{self.length}s_{self.fmt}_{src}"

    @property
    def nice_name(self) -> str:
        base = f"{self.teams['away']['abbr']}-at-{self.teams['home']['abbr']}_{self.date}"
        if self.focus == "player":
            base = re.sub(r"[^A-Za-z0-9]+", "-", self.player_name).strip("-") + "_" + base
        return f"{base}_{self.length}s_{self.fmt}.mp4"


# --------------------------------------------------------------------------- search

def _date_patterns(dt: datetime) -> list[re.Pattern]:
    full, abbr = dt.strftime("%B").lower(), dt.strftime("%b").lower()
    d, m, y, yy = dt.day, dt.month, dt.year, dt.strftime("%y")
    month = f"(?:{full}|{abbr}|sept)" if m == 9 else f"(?:{full}|{abbr})"
    return [
        re.compile(rf"\b{month}\.?\s+0?{d}(?:st|nd|rd|th)?\b"),            # April 9 / Apr. 9th
        re.compile(rf"\b0?{d}(?:st|nd|rd|th)?\s+(?:of\s+)?{month}\b"),      # 9 April
        re.compile(rf"\b0?{m}[/.\-]0?{d}[/.\-](?:{y}|{yy})\b"),             # 4/9/2025, 4.9.25
        re.compile(rf"\b{y}[/.\-]0?{m}[/.\-]0?{d}\b"),                      # 2025-04-09
    ]


def _score(entry: dict, req: Request) -> float | None:
    """Score a search result; None means it is not about this game."""
    title = (entry.get("title") or "").lower()
    dur = entry.get("duration") or 0
    if not title or (dur and (dur < 25 or dur > MAX_SOURCE_SECONDS)):
        return None
    if any(w in title for w in BAD_WORDS):
        return None

    date_hit = any(p.search(title) for p in _date_patterns(req.dt))
    if not date_hit and req.series_game and str(req.dt.year) in title:
        # Playoff games: "1998 NBA Finals Game 6", "Finals G6"
        date_hit = bool(re.search(rf"\b(?:game|g)\s*{req.series_game}\b", title))
    opp_hit = any(a in title for a in req.teams["opp"]["aliases"])
    me_hit = any(a in title for a in req.teams["me"]["aliases"])
    channel = (entry.get("channel") or entry.get("uploader") or "").lower()
    score = 0.0

    if req.focus == "player":
        last = req.player_name.split()[-1].lower()
        name_hit = last in title or req.player_name.lower() in title
        pts_hit = req.pts is not None and re.search(rf"\b{req.pts}\s*(?:pts|points)\b", title)
        if not name_hit or not (date_hit or (opp_hit and pts_hit)):
            return None
        score += 6 + 3 * date_hit + 2 * opp_hit + 2 * bool(pts_hit)
    else:
        if not (me_hit and opp_hit and date_hit):
            return None
        score += 10
        score += 2 * ("full game highlights" in title)

    score += 1.5 * ("highlight" in title)
    score += 3 * (channel in ("nba", "house of highlights", "ballislife"))
    if 120 <= dur <= 20 * 60:
        score += 1  # a proper highlight package, not a single clip
    return score


def _search(query: str, n: int = 12) -> list[dict]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    return [e for e in (info or {}).get("entries", []) if e and e.get("id")]


def _summary(e: dict) -> dict:
    return {
        "id": e["id"],
        "title": e.get("title") or e["id"],
        "channel": e.get("channel") or e.get("uploader") or "",
        "duration": e.get("duration"),
        "url": f"https://www.youtube.com/watch?v={e['id']}",
    }


def find_candidates(req: Request) -> list[dict]:
    dt = req.dt
    day = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    home, away = req.teams["home"], req.teams["away"]
    game_queries = [
        f"{away['nickname']} at {home['nickname']} full game highlights {day}",
        f"{away['full_name']} vs {home['full_name']} highlights {day}",
    ]
    player_queries = [
        f"{req.player_name} highlights vs {req.teams['opp']['nickname']} {day}",
        f"{req.player_name} {req.pts} pts {req.teams['opp']['nickname']} {day}",
    ]
    if req.series_game:
        n, y = req.series_game, dt.year
        game_queries.append(f"{away['nickname']} vs {home['nickname']} {y} playoffs game {n} highlights")
        player_queries.append(f"{req.player_name} {y} playoffs game {n} vs {req.teams['opp']['nickname']} highlights")

    def run(queries: list[str], focus: str) -> tuple[list[dict], list[dict]]:
        seen, scored, raw = set(), [], []
        probe = replace(req, focus=focus) if focus != req.focus else req
        for q in queries:
            for e in _search(q):
                if e["id"] in seen:
                    continue
                seen.add(e["id"])
                raw.append(e)
                s = _score(e, probe)
                if s is not None:
                    scored.append((s, e))
        scored.sort(key=lambda x: -x[0])
        return [_summary(e) for _, e in scored], [_summary(e) for e in raw]

    if req.focus == "player":
        matches, raw = run(player_queries, "player")
        if matches:
            return matches
        # No player package for that night - fall back to the full game highlights.
        matches, raw2 = run(game_queries, "game")
        if matches:
            return matches
        raise HighlightError("Couldn't find highlights for this game on YouTube.", (raw + raw2)[:8])

    matches, raw = run(game_queries, "game")
    if matches:
        return matches
    raise HighlightError("Couldn't find highlights for this game on YouTube.", raw[:8])


# --------------------------------------------------------------------------- download

def download(video_id: str, report: Report) -> Path:
    existing = [p for p in SOURCE_DIR.glob(f"{video_id}.*") if p.suffix in (".mp4", ".mkv", ".webm")]
    if existing:
        report("download", 55, "Using cached source video")
        return existing[0]

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = (d.get("downloaded_bytes") or 0) / total if total else 0
            report("download", 10 + 45 * frac, f"Downloading source video… {frac:.0%}")

    opts = {
        "format": "bv*+ba/b",
        "format_sort": ["res:720", "vcodec:h264", "acodec:aac"],
        "merge_output_format": "mp4",
        "outtmpl": str(SOURCE_DIR / "%(id)s.%(ext)s"),
        "ffmpeg_location": FFMPEG,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    with YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

    files = [p for p in SOURCE_DIR.glob(f"{video_id}.*") if p.suffix in (".mp4", ".mkv", ".webm")]
    if not files:
        raise HighlightError("The download finished but no video file was produced.")
    return files[0]


# --------------------------------------------------------------------------- analyze

def probe_duration(path: Path) -> float:
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not m:
        raise HighlightError("Could not read the source video.")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def excitement_curve(path: Path, hop: float = 0.5) -> np.ndarray | None:
    """Loudness (dB) per `hop` seconds, relative to the surrounding 30 s. None if no audio."""
    sr = 8000
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True,
    ).stdout
    if len(raw) < sr * 2:
        return None
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(sr * hop)
    frames = len(a) // n
    rms = np.sqrt((a[: frames * n].reshape(frames, n) ** 2).mean(axis=1) + 1e-10)
    db = 20 * np.log10(rms)

    def smooth(x, seconds):
        w = max(1, int(seconds / hop))
        return np.convolve(x, np.ones(w) / w, mode="same")

    return smooth(db, 2.5) - smooth(db, 30)  # sustained roar vs. local baseline


def pick_segments(duration: float, target: float, curve: np.ndarray | None, hop: float = 0.5):
    """Return sorted (start, end) clips totalling about `target` seconds."""
    head, tail = 3.0, 4.0  # skip intro card / end screen
    usable = duration - head - tail
    if usable <= target + 3:
        return [(0.0, duration)]  # source is already short enough

    k = max(1, round(target / SEGMENT_SECONDS))
    seg = target / k
    gap = 1.0

    def place(t_peak):
        s = min(max(head, t_peak - LEAD_IN), duration - tail - seg)
        return (s, s + seg)

    chosen: list[tuple[float, float]] = []
    if curve is not None:
        # Greedy: loudest moments first, no overlaps.
        for idx in np.argsort(curve)[::-1]:
            s, e = place(idx * hop)
            if all(e + gap <= cs or s >= ce + gap for cs, ce in chosen):
                chosen.append((s, e))
                if len(chosen) == k:
                    break
    if len(chosen) < k:  # no audio or not enough room: spread evenly
        chosen = [place(head + LEAD_IN + i * (usable - seg) / max(1, k - 1)) for i in range(k)] if k > 1 \
            else [place(duration / 2)]
    return sorted(chosen)


# --------------------------------------------------------------------------- render

def render(src: Path, clips, fmt: str, out: Path, report: Report) -> None:
    W, H = (720, 1280) if fmt == "vertical" else (1280, 720)
    fade = 0.25
    args = [FFMPEG, "-y", "-hide_banner", "-v", "error", "-nostats", "-progress", "pipe:1"]
    for s, e in clips:
        args += ["-ss", f"{s:.2f}", "-t", f"{e - s:.2f}", "-i", str(src)]

    parts = []
    for i, (s, e) in enumerate(clips):
        d = e - s
        fades = f"fade=t=in:st=0:d={fade},fade=t=out:st={max(0, d - fade):.2f}:d={fade}"
        if fmt == "vertical":
            parts.append(
                f"[{i}:v]split[b{i}][f{i}];"
                f"[b{i}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2[bg{i}];"
                f"[f{i}]scale={W}:-2[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,format=yuv420p,{fades}[v{i}]"
            )
        else:
            parts.append(
                f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p,{fades}[v{i}]"
            )
        parts.append(
            f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"afade=t=in:st=0:d={fade},afade=t=out:st={max(0, d - fade):.2f}:d={fade}[a{i}]"
        )
    streams = "".join(f"[v{i}][a{i}]" for i in range(len(clips)))
    parts.append(f"{streams}concat=n={len(clips)}:v=1:a=1[v][a]")

    tmp = out.with_suffix(".part.mp4")
    args += [
        "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp),
    ]
    total = sum(e - s for s, e in clips)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and line.strip() != "out_time_us=N/A":
            done = int(line.split("=")[1]) / 1e6
            report("render", 65 + 34 * min(1.0, done / total), f"Editing your reel… {min(1, done / total):.0%}")
    err = proc.stderr.read()
    if proc.wait() != 0:
        tmp.unlink(missing_ok=True)
        detail = err.strip().splitlines()[-1] if err.strip() else "unknown error"
        raise HighlightError(f"ffmpeg failed while editing the video: {detail}")
    tmp.replace(out)


# --------------------------------------------------------------------------- main entry

def make_highlight(req: Request, report: Report) -> dict:
    out = OUTPUT_DIR / f"{req.cache_key}.mp4"

    report("search", 3, "Searching the web for game highlights…")
    if req.video_id:
        candidates = [{"id": req.video_id, "title": "Selected video", "channel": "", "duration": None,
                       "url": f"https://www.youtube.com/watch?v={req.video_id}"}]
    else:
        candidates = find_candidates(req)
    source = candidates[0]
    report("search", 10, f"Found: {source['title']}")

    if out.exists():
        report("render", 100, "Ready (cached)")
        return {"file": out.name, "download_name": req.nice_name, "source": source,
                "candidates": candidates[1:6], "clips": []}

    src = download(source["id"], report)

    report("analyze", 58, "Listening for the crowd to find the biggest plays…")
    duration = probe_duration(src)
    clips = pick_segments(duration, req.length, excitement_curve(src))

    report("render", 65, f"Cutting {len(clips)} clips…")
    render(src, clips, req.fmt, out, report)
    report("render", 100, "Done!")
    return {
        "file": out.name,
        "download_name": req.nice_name,
        "source": source,
        "candidates": candidates[1:6],
        "clips": [{"start": round(s, 1), "end": round(e, 1)} for s, e in clips],
    }
