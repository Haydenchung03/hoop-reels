"""Find highlight footage for a game on YouTube and cut a short reel.

Two kinds of reel:

* Player reel (focus="player"): the NBA play-by-play lists every play the player made
  happen (shots, assists, steals, blocks, rebounds) with its quarter and game clock.
  We download highlight videos (player-specific ones first, then full-game packages),
  read the on-screen scoreboard frame by frame (scoreboard.py), find the moments whose
  clock matches the player's plays, and cut only those. Every clip is one of their plays.

* Game reel (focus="game"): one full-game highlight package; the loudest crowd moments
  are cut together.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import imageio_ffmpeg
import numpy as np
from yt_dlp import YoutubeDL

import scoreboard
from nba_data import player_events, team_info

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SOURCE_DIR = DATA_DIR / "sources"
OUTPUT_DIR = DATA_DIR / "output"
for d in (SOURCE_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

SEGMENT_SECONDS = 7.5   # game reel: length of each clip
LEAD_IN = 5.0           # game reel: seconds kept before a loudness peak
MAX_SOURCE_SECONDS = 30 * 60  # skip full-game replays
MAX_PLAYER_SOURCES = 3  # player reel: videos to scan at most
BAD_WORDS = ("reaction", "2k", "simulation", "prediction", "preview", "live stream", "livestream",
             "full game replay", "podcast", "breakdown", "fantasy", "betting", "picks")

# Seconds kept before / after the moment of each kind of play.
PLAY_WINDOWS = {
    "3PT": (5.0, 2.5), "DUNK": (5.0, 2.5), "FG": (5.0, 2.5),
    "AST": (5.5, 2.5),                      # the pass comes before the basket
    "BLK": (3.5, 3.0), "STL": (3.0, 4.0),   # a steal often turns into a fast break
    "OREB": (3.5, 3.0), "REB": (3.0, 2.5),
}
KIND_NAMES = {"3PT": "3-pointer", "DUNK": "Dunk", "FG": "Bucket", "AST": "Assist",
              "BLK": "Block", "STL": "Steal", "OREB": "Offensive rebound", "REB": "Rebound"}

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
    focus: str = "player"      # "player" or "game"
    length: int = 60           # seconds
    fmt: str = "vertical"      # "vertical" or "landscape"
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
    def last_name(self) -> str:
        return self.player_name.split()[-1]

    @property
    def cache_key(self) -> str:
        who = f"p{self.player_id}v2" if self.focus == "player" else "game"
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
        name_hit = req.last_name.lower() in title
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


def _search_scored(req: Request, focus: str) -> tuple[list[dict], list[dict]]:
    """(matching videos best-first, every search result) for focus 'player' or 'game'."""
    dt = req.dt
    day = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    home, away, opp = req.teams["home"], req.teams["away"], req.teams["opp"]
    if focus == "player":
        queries = [
            f"{req.player_name} highlights vs {opp['nickname']} {day}",
            f"{req.player_name} {req.pts} pts {opp['nickname']} {day}",
        ]
        if req.series_game:
            queries.append(f"{req.player_name} {dt.year} playoffs game {req.series_game} vs {opp['nickname']} highlights")
    else:
        queries = [
            f"{away['nickname']} at {home['nickname']} full game highlights {day}",
            f"{away['full_name']} vs {home['full_name']} highlights {day}",
        ]
        if req.series_game:
            queries.append(f"{away['nickname']} vs {home['nickname']} {dt.year} playoffs game {req.series_game} highlights")

    probe = replace(req, focus=focus) if focus != req.focus else req
    seen, scored, raw = set(), [], []
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


def _forced_source(req: Request) -> dict:
    return {"id": req.video_id, "title": "Selected video", "channel": "", "duration": None,
            "url": f"https://www.youtube.com/watch?v={req.video_id}"}


# --------------------------------------------------------------------------- download

def download(video_id: str, report: Report, lo: float = 10, hi: float = 55, label: str = "") -> Path:
    existing = [p for p in SOURCE_DIR.glob(f"{video_id}.*") if p.suffix in (".mp4", ".mkv", ".webm")]
    if existing:
        report("download", hi, f"Using cached video{label}")
        return existing[0]

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = (d.get("downloaded_bytes") or 0) / total if total else 0
            report("download", lo + (hi - lo) * frac, f"Downloading footage{label}… {frac:.0%}")

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


# --------------------------------------------------------------------------- audio analysis (game reels)

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

def render(clips: list[tuple[Path, float, float]], fmt: str, out: Path, report: Report, lo: float = 80) -> None:
    W, H = (720, 1280) if fmt == "vertical" else (1280, 720)
    fade = 0.2
    args = [FFMPEG, "-y", "-hide_banner", "-v", "error", "-nostats", "-progress", "pipe:1"]
    for src, s, e in clips:
        args += ["-ss", f"{s:.2f}", "-t", f"{e - s:.2f}", "-i", str(src)]

    parts = []
    for i, (_, s, e) in enumerate(clips):
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
    total = sum(e - s for _, s, e in clips)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and line.strip() != "out_time_us=N/A":
            frac = min(1.0, int(line.split("=")[1]) / 1e6 / total)
            report("render", lo + (99 - lo) * frac, f"Editing your reel… {frac:.0%}")
    err = proc.stderr.read()
    if proc.wait() != 0:
        tmp.unlink(missing_ok=True)
        detail = err.strip().splitlines()[-1] if err.strip() else "unknown error"
        raise HighlightError(f"ffmpeg failed while editing the video: {detail}")
    tmp.replace(out)


# --------------------------------------------------------------------------- player reel

def _play_clip(ev: dict, t: float, seg_index: int, segs: list[dict]) -> tuple[float, float] | None:
    """Cut window for a play at video time t, kept inside its highlight segment."""
    pre, post = PLAY_WINDOWS[ev["kind"]]
    seg = segs[seg_index]
    next_start = segs[seg_index + 1]["start"] if seg_index + 1 < len(segs) else float("inf")
    s = max(seg["start"] - 0.5, t - pre, 0.0)
    e = min(t + post, seg["end"] + 1.5, next_start - 0.2)
    return (s, e) if e - s >= 2.5 else None


def _select_plays(found: dict, target: float) -> list[dict]:
    """Best plays first until the reel is long enough, then back in game order."""
    picked, total = [], 0.0
    for play in sorted(found.values(), key=lambda p: -p["ev"]["priority"]):
        if total >= target - 2:
            break
        picked.append(play)
        total += play["end"] - play["start"]
    return sorted(picked, key=lambda p: (p["ev"]["period"], -p["ev"]["clock"]))


def _merge(plays: list[dict]) -> list[dict]:
    """Join plays that overlap in the same video (e.g. a steal and the layup after it)."""
    merged: list[dict] = []
    for p in plays:
        last = merged[-1] if merged else None
        if last and last["path"] == p["path"] and p["start"] <= last["end"] + 0.6 and p["end"] >= last["start"]:
            last["start"], last["end"] = min(last["start"], p["start"]), max(last["end"], p["end"])
            last["events"].append(p["ev"])
        else:
            merged.append({"path": p["path"], "start": p["start"], "end": p["end"], "events": [p["ev"]]})
    return merged


def make_player_reel(req: Request, report: Report) -> dict:
    name = req.player_name
    report("search", 2, f"Pulling {name}'s plays from the NBA play-by-play…")
    try:
        events = player_events(req.game_id, req.player_id)
    except Exception as exc:
        raise HighlightError(f"Couldn't load the play-by-play for this game ({type(exc).__name__}). Try again.")
    events = [e for e in events if e["kind"] in PLAY_WINDOWS]
    if not events:
        raise HighlightError(f"The NBA has no play-by-play of {name} for this game, so a player reel isn't possible. "
                             "Try the Full game option.")
    if not scoreboard.available():
        raise HighlightError("Player reels need macOS text recognition (pip install pyobjc-framework-Vision "
                             "pyobjc-framework-Quartz). Use the Full game option instead.")

    report("search", 5, f"Found {len(events)} {name} plays. Searching for footage…")
    if req.video_id:
        sources, others = [_forced_source(req)], []
    else:
        player_vids, raw_p = _search_scored(req, "player")
        game_vids, raw_g = _search_scored(req, "game")
        sources, seen = [], set()
        for v in player_vids[:2] + game_vids[:2] + player_vids[2:] + game_vids[2:]:
            if v["id"] not in seen:
                seen.add(v["id"])
                sources.append(v)
        others = sources[MAX_PLAYER_SOURCES:]
        sources = sources[:MAX_PLAYER_SOURCES]
        if not sources:
            raise HighlightError("Couldn't find footage of this game on YouTube.", (raw_p + raw_g)[:8])

    found: dict = {}
    used: list[dict] = []
    span = 72 / len(sources)
    for i, src in enumerate(sources):
        lo = 8 + span * i
        label = f" ({i + 1}/{len(sources)})" if len(sources) > 1 else ""
        try:
            path = download(src["id"], report, lo, lo + span * 0.5, label)
        except Exception as exc:
            if len(sources) == 1:
                raise
            print(f"skipping {src['id']}: {exc}")
            continue
        duration = probe_duration(path)
        report("scan", lo + span * 0.5, f"Reading the scoreboard to find {name}'s plays{label}…")
        rows = scoreboard.read_timeline(
            path, duration,
            lambda f: report("scan", lo + span * (0.5 + 0.5 * f), f"Reading the scoreboard to find {name}'s plays{label}… {f:.0%}"),
        )
        segs = scoreboard.segments(rows)
        new = 0
        for ev in events:
            if ev["id"] in found:
                continue
            loc = scoreboard.locate(segs, ev["period"], ev["clock"])
            if not loc:
                continue
            t, seg = loc
            window = _play_clip(ev, t, segs.index(seg), segs)
            if window:
                found[ev["id"]] = {"path": path, "start": window[0], "end": window[1], "ev": ev}
                new += 1
        if new:
            used.append({**src, "plays": new})
        have = sum(p["end"] - p["start"] for p in found.values())
        report("scan", lo + span, f"Spotted {len(found)} of {name}'s {len(events)} plays so far")
        if have >= req.length - 2:
            break

    if not found:
        raise HighlightError(
            f"Found footage of the game, but couldn't spot any of {name}'s plays in it "
            "(the scoreboard may be hidden or unreadable). Try another video below.",
            sources + others,
        )

    clips = _merge(_select_plays(found, req.length))
    render([(c["path"], c["start"], c["end"]) for c in clips], req.fmt, OUTPUT_DIR / f"{req.cache_key}.mp4", report)

    plays = [{"label": ev["label"], "kind": KIND_NAMES[ev["kind"]], "desc": ev["desc"]}
             for c in clips for ev in c["events"]]
    seconds = sum(c["end"] - c["start"] for c in clips)
    note = f"{len(plays)} of {name}'s plays · {seconds:.0f}s"
    if seconds < req.length * 0.75:
        note += f" (only {len(found)} of {len(events)} plays appear in the footage that's online)"
    unused = [s for s in sources if s["id"] not in {u["id"] for u in used}]
    return {"sources": used, "candidates": (unused + others)[:6], "plays": plays, "note": note,
            "clips": [{"start": round(c["start"], 1), "end": round(c["end"], 1)} for c in clips]}


# --------------------------------------------------------------------------- game reel

def make_game_reel(req: Request, report: Report) -> dict:
    report("search", 3, "Searching the web for game highlights…")
    if req.video_id:
        candidates = [_forced_source(req)]
    else:
        candidates, raw = _search_scored(req, "game")
        if not candidates:
            raise HighlightError("Couldn't find highlights for this game on YouTube.", raw[:8])
    source = candidates[0]
    report("search", 10, f"Found: {source['title']}")

    path = download(source["id"], report, 10, 55)
    report("analyze", 58, "Listening for the crowd to find the biggest plays…")
    duration = probe_duration(path)
    clips = pick_segments(duration, req.length, excitement_curve(path))

    report("render", 65, f"Cutting {len(clips)} clips…")
    render([(path, s, e) for s, e in clips], req.fmt, OUTPUT_DIR / f"{req.cache_key}.mp4", report, lo=65)
    return {"sources": [source], "candidates": candidates[1:6], "plays": [],
            "note": f"{len(clips)} biggest moments of the game",
            "clips": [{"start": round(s, 1), "end": round(e, 1)} for s, e in clips]}


# --------------------------------------------------------------------------- main entry

def make_highlight(req: Request, report: Report) -> dict:
    out = OUTPUT_DIR / f"{req.cache_key}.mp4"
    meta = out.with_suffix(".json")
    if out.exists() and meta.exists():
        report("render", 100, "Ready (cached)")
        return json.loads(meta.read_text())

    result = make_player_reel(req, report) if req.focus == "player" else make_game_reel(req, report)
    result.update({"file": out.name, "download_name": req.nice_name, "focus": req.focus})
    meta.write_text(json.dumps(result))
    report("render", 100, "Done!")
    return result
