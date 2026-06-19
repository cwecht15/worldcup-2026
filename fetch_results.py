"""Optional: pull live 2026 World Cup results from football-data.org.

Writes data/results.json (completed matches + goal scorers), which
build_web_data.py reads to CONDITION the simulation on real results — so accrued
points, win %, the Golden Boot, and "blocked from winning" all reflect where the
tournament actually is.

Setup: get a free key at https://www.football-data.org/client/register and put it
in data/.football_data_key (one line) or the env var FOOTBALL_DATA_KEY, then:
    python fetch_results.py

Free tier = 10 requests/minute; this uses 2 per run.  If there is no key, the
competition isn't on your plan, or no matches have finished, it writes nothing
and the build simply stays in pre-results mode (never fails the build).
"""

import datetime as dt
import json
import os
import sys
import urllib.request

DATA = os.path.join(os.path.dirname(__file__), "data")
KEY_FILE = os.path.join(DATA, ".football_data_key")
OUT = os.path.join(DATA, "results.json")
BASE = "https://api.football-data.org/v4"
COMP = "WC"   # FIFA World Cup


def get_key():
    k = os.environ.get("FOOTBALL_DATA_KEY")
    if k and k.strip():
        return k.strip()
    if os.path.exists(KEY_FILE):
        return open(KEY_FILE, encoding="utf-8-sig").read().strip()
    return None


def _get(path, key):
    req = urllib.request.Request(BASE + path, headers={"X-Auth-Token": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def build():
    key = get_key()
    if not key:
        print("[results] no FOOTBALL_DATA_KEY / data/.football_data_key -> "
              "skipping (sim stays pre-results)")
        return False

    try:
        md = _get(f"/competitions/{COMP}/matches", key)
    except Exception as e:
        print(f"[results] could not fetch matches ({e}); staying pre-results")
        return False

    matches = []
    upcoming = []
    for m in md.get("matches", []):
        status = m.get("status")
        if status not in ("FINISHED", "AWARDED"):
            # not played yet -> capture as an upcoming fixture (for the schedule card)
            if status in ("SCHEDULED", "TIMED", "IN_PLAY", "PAUSED"):
                upcoming.append({
                    "utcDate": m.get("utcDate"),
                    "stage": m.get("stage"),
                    "group": m.get("group"),
                    "matchday": m.get("matchday"),
                    "home": (m.get("homeTeam") or {}).get("name"),
                    "away": (m.get("awayTeam") or {}).get("name"),
                })
            continue
        sc = m.get("score", {}) or {}
        ft = sc.get("fullTime", {}) or {}
        if ft.get("home") is None or ft.get("away") is None:
            continue
        w = sc.get("winner")            # HOME_TEAM / AWAY_TEAM / DRAW
        winner = "HOME" if w == "HOME_TEAM" else "AWAY" if w == "AWAY_TEAM" else None
        matches.append({
            "stage": m.get("stage"),
            "group": m.get("group"),
            "utcDate": m.get("utcDate"),
            "matchday": m.get("matchday"),
            "home": (m.get("homeTeam") or {}).get("name"),
            "away": (m.get("awayTeam") or {}).get("name"),
            "home_goals": ft["home"], "away_goals": ft["away"],
            "winner": winner, "status": "FINISHED",
        })
    upcoming.sort(key=lambda x: x.get("utcDate") or "")
    upcoming = upcoming[:40]

    scorers = []
    try:
        sd = _get(f"/competitions/{COMP}/scorers?limit=60", key)
        for s in sd.get("scorers", []):
            scorers.append({
                "player": (s.get("player") or {}).get("name"),
                "team": (s.get("team") or {}).get("name"),
                "goals": s.get("goals") or 0,
            })
    except Exception as e:
        print(f"[results] scorers unavailable ({e}); Golden Boot stays projection-based")

    if not matches:
        print("[results] no finished matches yet -> staying pre-results")
        # remove any stale file so the build doesn't condition on nothing
        if os.path.exists(OUT):
            os.remove(OUT)
        return False

    payload = {
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "matches": matches, "scorers": scorers, "upcoming": upcoming,
    }
    os.makedirs(DATA, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"[results] wrote {OUT}: {len(matches)} finished matches, "
          f"{len(scorers)} scorers, {len(upcoming)} upcoming fixtures")
    return True


if __name__ == "__main__":
    build()
    sys.exit(0)   # never fail the build
