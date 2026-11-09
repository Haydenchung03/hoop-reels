"""Hoop Reels - pick a player and a game, get a short highlight reel.

Run:  python app.py   then open http://127.0.0.1:8000
"""
from __future__ import annotations

import os
import re
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

import nba_data
from highlights import OUTPUT_DIR, HighlightError, Request, make_highlight

app = Flask(__name__)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=2)  # at most two reels render at once


def update_job(job_id: str, **fields) -> None:
    with jobs_lock:
        jobs[job_id].update(fields)


def api_error(message: str, status: int = 400, **extra):
    return jsonify({"error": message, **extra}), status


# --------------------------------------------------------------------------- pages

@app.get("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------- data API

@app.get("/api/players")
def api_players():
    return jsonify(nba_data.search_players(request.args.get("q", "")))


@app.get("/api/seasons")
def api_seasons():
    player_id = request.args.get("player_id", type=int)
    if not player_id or not nba_data.get_player(player_id):
        return api_error("Unknown player.")
    try:
        return jsonify(nba_data.get_seasons(player_id))
    except Exception as exc:
        return api_error(f"NBA stats are unavailable right now ({type(exc).__name__}). Try again.", 502)


@app.get("/api/games")
def api_games():
    player_id = request.args.get("player_id", type=int)
    season = request.args.get("season", "")
    if not player_id or not re.fullmatch(r"\d{4}-\d{2}", season):
        return api_error("player_id and season (e.g. 2024-25) are required.")
    try:
        return jsonify(nba_data.get_games(player_id, season))
    except Exception as exc:
        return api_error(f"NBA stats are unavailable right now ({type(exc).__name__}). Try again.", 502)


# --------------------------------------------------------------------------- highlight jobs

@app.post("/api/highlight")
def api_highlight():
    body = request.get_json(silent=True) or {}
    game = body.get("game") or {}
    player = nba_data.get_player(int(body.get("player_id") or 0))

    if not player:
        return api_error("Unknown player.")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(game.get("date", ""))):
        return api_error("Pick a game first.")
    for key in ("team", "opponent"):
        if not re.fullmatch(r"[A-Z]{2,4}", str(game.get(key, ""))):
            return api_error("Invalid game data.")
    if not re.fullmatch(r"\d{10}", str(game.get("game_id", ""))):
        return api_error("Invalid game id.")
    video_id = body.get("video_id")
    if video_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id)):
        return api_error("Invalid video id.")

    req = Request(
        player_id=player["id"],
        player_name=player["name"],
        game_id=game["game_id"],
        date=game["date"],
        team=game["team"],
        opponent=game["opponent"],
        home=bool(game.get("home")),
        pts=game.get("pts") if isinstance(game.get("pts"), int) else None,
        series_game=game.get("series_game") if game.get("series_game") in range(1, 8) else None,
        focus=body.get("focus") if body.get("focus") in ("game", "player") else "player",
        length=body.get("length") if body.get("length") in (30, 60, 90, 120) else 60,
        fmt=body.get("fmt") if body.get("fmt") in ("landscape", "vertical") else "vertical",
        video_id=video_id,
    )

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "status": "queued", "stage": "queued", "progress": 0,
                        "message": "Waiting in line…", "result": None, "error": None, "candidates": []}

    def report(stage: str, pct: float, message: str):
        update_job(job_id, stage=stage, progress=round(pct, 1), message=message)

    def run():
        update_job(job_id, status="running")
        try:
            result = make_highlight(req, report)
            result["video_url"] = f"/video/{result['file']}"
            result["download_url"] = f"/video/{result['file']}?download=1&name={result['download_name']}"
            result["fmt"] = req.fmt
            update_job(job_id, status="done", progress=100, result=result)
        except HighlightError as exc:
            update_job(job_id, status="error", error=str(exc), candidates=exc.candidates)
        except Exception as exc:  # yt-dlp / network / ffmpeg surprises
            traceback.print_exc()
            msg = str(exc).replace("ERROR: ", "").strip() or type(exc).__name__
            update_job(job_id, status="error", error=f"Something went wrong: {msg[:300]}")

    executor.submit(run)
    return jsonify({"job_id": job_id})


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        return jsonify(dict(job)) if job else api_error("Unknown job.", 404)


@app.get("/video/<path:filename>")
def video(filename: str):
    if not re.fullmatch(r"[\w.-]+\.mp4", filename) or ".part." in filename:
        abort(404)
    download = request.args.get("download") == "1"
    name = request.args.get("name", filename)
    if not re.fullmatch(r"[\w.-]+\.mp4", name):
        name = filename
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=download,
                               download_name=name, conditional=True, mimetype="video/mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))  # 5000 is taken by AirPlay on macOS
    print(f"\n  Hoop Reels running at http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
