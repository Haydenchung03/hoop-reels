# Hoop Reels

Pick an NBA player, a season, and a game they played. The app finds that game's highlights on YouTube,
picks the biggest moments (by listening for crowd roars), and cuts a 30s–2min reel you can watch or download.
A switch at the top flips between a bare-bones page and a styled one.

## Run it

Needs **Python 3.10+** (macOS's built-in `python3` is 3.9, so use `python3.12` or newer).

```bash
cd ~/Desktop/NBAGamesVideo
python3.12 -m venv .venv          # skip if .venv already exists
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:8000**. Use `PORT=9000 python app.py` for another port.
ffmpeg comes bundled with the `imageio-ffmpeg` package, so nothing else needs installing.

## How it works

| Step | File | What happens |
|---|---|---|
| Player search | `nba_data.py` | nba_api's built-in player list (offline, accent-insensitive) |
| Seasons and games | `nba_data.py` | stats.nba.com career stats and game logs (regular season, play-in, playoffs) |
| Find footage | `highlights.py` | yt-dlp searches YouTube; titles must name both teams and the date (or the playoff "Game N") |
| Download | `highlights.py` | best stream up to 720p, cached in `data/sources/` |
| Pick moments | `highlights.py` | audio loudness vs. the surrounding 30 s; the loudest spikes become ~7.5 s clips |
| Edit | `highlights.py` | ffmpeg trims, fades, joins, and encodes H.264 MP4 (16:9, or 9:16 with blurred fill) |
| Web app | `app.py`, `templates/`, `static/` | Flask API plus a background job queue; vanilla JS frontend that polls for progress |

**Whole game** uses full-game highlight packages. **Just my player** looks for a highlight video of that
player's night first and falls back to the game package if there isn't one.
If the wrong video gets picked, the result lists the other matches; click one to rebuild the reel from it.

## Troubleshooting

- **Seasons or games won't load**: stats.nba.com is slow or rate-limiting you. Wait a minute and retry.
  It often blocks cloud and VPN IP addresses.
- **Downloads fail** ("Sign in to confirm", 403, no formats): YouTube changed something. Run
  `pip install -U yt-dlp`. If it still fails, install [Deno](https://deno.com) (`curl -fsSL https://deno.land/install.sh | sh`),
  which yt-dlp uses to solve YouTube's JavaScript challenges.
- **"Couldn't find highlights"**: older games (roughly before 2012) often have no highlight video. Use the listed
  search results if one is the right game.
- **Disk space**: source videos (~100 MB each) and reels pile up in `data/`. Delete the folder whenever you like.

Footage belongs to the NBA and the uploaders. Keep this to personal use.
