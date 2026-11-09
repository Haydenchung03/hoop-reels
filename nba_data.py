"""NBA data helpers: player search, seasons played, and per-season game logs.

Player search uses nba_api's bundled static list (no network).
Seasons and game logs come from stats.nba.com through nba_api and are cached in memory.
"""
from __future__ import annotations

import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache

from nba_api.stats.endpoints import playercareerstats, playergamelog
from nba_api.stats.static import players as static_players
from nba_api.stats.static import teams as static_teams

TIMEOUT = 45  # stats.nba.com can be slow

# Abbreviations for franchises that relocated or were renamed (not in nba_api's current team list).
HISTORIC_TEAMS = {
    "SEA": ("Seattle SuperSonics", "SuperSonics"),
    "NJN": ("New Jersey Nets", "Nets"),
    "VAN": ("Vancouver Grizzlies", "Grizzlies"),
    "CHH": ("Charlotte Hornets", "Hornets"),
    "NOH": ("New Orleans Hornets", "Hornets"),
    "NOK": ("New Orleans/Oklahoma City Hornets", "Hornets"),
    "WSB": ("Washington Bullets", "Bullets"),
    "KCK": ("Kansas City Kings", "Kings"),
    "SDC": ("San Diego Clippers", "Clippers"),
    "PHL": ("Philadelphia 76ers", "76ers"),
    "GOS": ("Golden State Warriors", "Warriors"),
    "UTH": ("Utah Jazz", "Jazz"),
    "SAN": ("San Antonio Spurs", "Spurs"),
    "PHO": ("Phoenix Suns", "Suns"),
    "NOJ": ("New Orleans Jazz", "Jazz"),
}

# Other names a team goes by in video titles.
NICKNAME_ALIASES = {
    "76ers": ["sixers"],
    "Trail Blazers": ["blazers"],
    "Timberwolves": ["wolves", "t-wolves"],
    "Mavericks": ["mavs"],
    "Cavaliers": ["cavs"],
    "SuperSonics": ["sonics"],
}


def _fold(text: str) -> str:
    """Lowercase and strip accents so 'Doncic' matches 'Dončić'."""
    norm = unicodedata.normalize("NFKD", text)
    return "".join(c for c in norm if not unicodedata.combining(c)).lower().strip()


@lru_cache(maxsize=1)
def _all_players():
    return [(p, _fold(p["full_name"])) for p in static_players.get_players()]


def search_players(query: str, limit: int = 12) -> list[dict]:
    q = _fold(query)
    if len(q) < 2:
        return []
    hits = []
    for p, folded in _all_players():
        if q in folded:
            # Rank: exact > starts with > a later word starts with > contains; active players first.
            if folded == q:
                rank = 0
            elif folded.startswith(q):
                rank = 1
            elif any(part.startswith(q) for part in folded.split()):
                rank = 2
            else:
                rank = 3
            hits.append((rank, not p["is_active"], p["full_name"], p))
    hits.sort(key=lambda h: h[:3])
    return [
        {"id": p["id"], "name": p["full_name"], "active": bool(p["is_active"])}
        for *_, p in hits[:limit]
    ]


def get_player(player_id: int) -> dict | None:
    p = static_players.find_player_by_id(player_id)
    return {"id": p["id"], "name": p["full_name"], "active": bool(p["is_active"])} if p else None


@lru_cache(maxsize=256)
def get_seasons(player_id: int) -> list[str]:
    data = playercareerstats.PlayerCareerStats(
        player_id=player_id, timeout=TIMEOUT
    ).get_normalized_dict()
    seasons = set()
    for key in ("SeasonTotalsRegularSeason", "SeasonTotalsPostSeason"):
        for row in data.get(key, []):
            if row.get("SEASON_ID"):
                seasons.add(row["SEASON_ID"])
    return sorted(seasons, reverse=True)


def team_info(abbr: str) -> dict:
    abbr = abbr.upper()
    t = static_teams.find_team_by_abbreviation(abbr)
    if t:
        full, nick = t["full_name"], t["nickname"]
    elif abbr in HISTORIC_TEAMS:
        full, nick = HISTORIC_TEAMS[abbr]
    else:
        full, nick = abbr, abbr
    return {
        "abbr": abbr,
        "full_name": full,
        "nickname": nick,
        "aliases": [nick.lower()] + NICKNAME_ALIASES.get(nick, []),
    }


def _parse_date(raw: str) -> datetime:
    for fmt in ("%b %d, %Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {raw}")


def _fetch_log(player_id: int, season: str, season_type: str) -> list[dict]:
    try:
        data = playergamelog.PlayerGameLog(
            player_id=player_id,
            season=season,
            season_type_all_star=season_type,
            timeout=TIMEOUT,
        ).get_normalized_dict()
    except Exception:
        if season_type == "Regular Season":
            raise
        return []  # playoffs / play-in missing is normal
    return data.get("PlayerGameLog", [])


@lru_cache(maxsize=512)
def _games_cached(player_id: int, season: str) -> tuple:
    types = ["Regular Season", "Playoffs"]
    if int(season[:4]) >= 2019:
        types.append("PlayIn")
    with ThreadPoolExecutor(max_workers=len(types)) as pool:
        logs = list(pool.map(lambda t: _fetch_log(player_id, season, t), types))

    games = []
    for season_type, rows in zip(types, logs):
        label = {"PlayIn": "Play-In"}.get(season_type, season_type)
        for r in rows:
            matchup = r["MATCHUP"]  # "LAL vs. HOU" (home) or "LAL @ DAL" (away)
            home = " vs. " in matchup
            team_abbr, opp_abbr = [s.strip() for s in matchup.replace(" vs. ", "|").replace(" @ ", "|").split("|")]
            dt = _parse_date(r["GAME_DATE"])
            games.append({
                "game_id": r["Game_ID"],
                "date": dt.strftime("%Y-%m-%d"),
                "date_label": dt.strftime("%b %-d, %Y"),
                "matchup": matchup,
                "home": home,
                "team": team_abbr,
                "opponent": opp_abbr,
                "wl": r.get("WL") or "",
                "pts": r.get("PTS"),
                "reb": r.get("REB"),
                "ast": r.get("AST"),
                "min": r.get("MIN"),
                "type": label,
            })
    # Playoff videos are usually titled "Game 6" rather than by date, so number each series.
    series_count: dict[str, int] = {}
    for g in sorted(games, key=lambda g: g["date"]):
        g["series_game"] = None
        if g["type"] == "Playoffs":
            series_count[g["opponent"]] = series_count.get(g["opponent"], 0) + 1
            g["series_game"] = series_count[g["opponent"]]

    games.sort(key=lambda g: g["date"], reverse=True)
    return tuple(games)


def get_games(player_id: int, season: str) -> list[dict]:
    return list(_games_cached(player_id, season))
