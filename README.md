# Hoop Reels

Pick an NBA player, a season, and a game they played. The app cuts a 30s–2min reel of **that player's plays**
(their buckets, assists, steals, blocks and rebounds) from highlight footage on YouTube, ready for TikTok or Reels.
Watch it in the page or download the MP4. A switch at the top flips between a bare-bones page and a styled one.

> **Works best for modern games and players.** Recent seasons have the most highlight footage on YouTube,
> consistently titled videos, and clear on-screen scoreboards, which player reels rely on. Historical games
> are hit-or-miss: older games often have little or no footage online, the NBA play-by-play only goes back to
> 1996-97, and older broadcasts' scoreboards are harder to read. Better support for historical games is planned.

## Run it

Needs **Python 3.12+**. macOS's built-in `python3` is 3.9, which is too old. The latest yt-dlp needs 3.11+ and the latest numpy needs 3.12+.

```bash
cd ~/Desktop/NBAGamesVideo
python3.12 -m venv .venv          # skip if .venv already exists
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:8000**. Use `PORT=9000 python app.py` for another port.
ffmpeg comes bundled with the `imageio-ffmpeg` package, so nothing else needs installing.
Player reels read the on-screen scoreboard with Apple's Vision framework, so they need **macOS**.
On other systems, use the Full game option.

## How player reels work

YouTube titles can't tell you which clips show your player, so the app works it out from the game data:

1. **Plays.** The NBA play-by-play (`nba_data.player_events`) lists every play the player made happen
   with its quarter and game clock: made shots, assists, steals, blocks, offensive and defensive rebounds.
   Each gets a priority (3s, dunks and blocks first; crunch-time plays get a boost).
2. **Footage.** yt-dlp searches YouTube for player highlight videos of that night first, then full-game
   highlight packages. Titles must name the player or both teams, plus the date (or the playoff "Game N").
3. **Scoreboard reading** (`scoreboard.py`). macOS text recognition finds where this broadcast's scoreboard
   sits, then reads its quarter and clock twice a second. It handles one-line layouts ("1ST 10:40") and
   stacked ones (TNT's clock above the quarter), and ignores other games' tickers.
4. **Matching.** Readings are grouped into the separate plays the video jumps between. A play at
   "Q3 8:19" is cut from the moment the on-screen clock reads 8:19, with a few seconds of build-up
   (more before an assist, more after a steal), and kept within that play's footage.
5. **Picking.** The best plays fill the chosen length and are put back in game order. If one video doesn't
   have enough of them, up to three are combined. The page lists every play in the reel.

**Full game** reels instead take one full-game package and cut its loudest crowd moments.
If the wrong video gets picked, the result lists the other matches; click one to rebuild the reel from it.

| File | Role |
|---|---|
| `nba_data.py` | player search (offline list), seasons, game logs, play-by-play events |
| `highlights.py` | YouTube search, download, play matching, audio picking, ffmpeg editing |
| `scoreboard.py` | reading the quarter and game clock off video frames |
| `app.py` | Flask API and background job queue |
| `templates/`, `static/` | the page: vanilla JS, and the stylesheet bare-bones mode switches off |

## Troubleshooting

- **Seasons or games won't load**: stats.nba.com is slow or rate-limiting you. Wait a minute and retry.
  It often blocks cloud and VPN IP addresses.
- **Downloads fail** ("Sign in to confirm", 403, no formats): YouTube changed something. Run
  `pip install -U yt-dlp`. If it still fails, install [Deno](https://deno.com) (`curl -fsSL https://deno.land/install.sh | sh`),
  which yt-dlp uses to solve YouTube's JavaScript challenges.
- **"Couldn't find highlights"**: older games (roughly before 2012) often have no highlight video. Use the listed
  search results if one is the right game.
- **Player reel is shorter than chosen**: highlight videos only show some plays. The note under the video says
  how many of the player's plays were found. Bench players in quiet games may only have a few.
- **"Couldn't spot any of the player's plays"**: the footage has no readable scoreboard (some fan edits crop
  it out). Pick another video from the list.
- **Disk space**: source videos (~100 MB each) and reels pile up in `data/`. Delete the folder whenever you like.

Footage belongs to the NBA and the uploaders. Keep this to personal use.
