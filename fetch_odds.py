"""Optional: refresh DraftKings odds from The Odds API.

Updates two things:
  1. data/teams.csv       - title (outright winner) odds
  2. data/match_odds.csv  - de-vigged win/draw/win probs for every priced
                            group fixture (used to calibrate group-stage play)

Setup: put your key in data/.odds_api_key (one line), then run:
    python fetch_odds.py

Free tier = 500 requests/month; this script uses 2 per run (the /sports list
call is free).  The key file is git-ignored.
"""

import os
import sys
import json
import urllib.request

import pandas as pd

DATA = os.path.join(os.path.dirname(__file__), "data")
KEY_FILE = os.path.join(DATA, ".odds_api_key")
SPORT = "soccer_fifa_world_cup_winner"
MATCH_SPORT = "soccer_fifa_world_cup"
# The /sports endpoint is FREE (does not count against the monthly quota) and
# every response carries the x-requests-remaining header -> use it as a preflight.
SPORTS_URL = "https://api.the-odds-api.com/v4/sports/"

# The Odds API team name -> our teams.csv name (only where they differ)
NAME_MAP = {
    "United States": "USA", "USA": "USA",
    "South Korea": "South Korea", "Korea Republic": "South Korea",
    "Czech Republic": "Czechia", "Czechia": "Czechia",
    "Ivory Coast": "Ivory Coast", "Cote d'Ivoire": "Ivory Coast",
    "Turkiye": "Turkey", "Turkey": "Turkey", "Türkiye": "Turkey",
    "Bosnia and Herzegovina": "Bosnia", "Bosnia & Herzegovina": "Bosnia",
    "DR Congo": "DR Congo", "Congo DR": "DR Congo",
    "Curacao": "Curacao", "Curaçao": "Curacao",
}


def get_key():
    if not os.path.exists(KEY_FILE):
        sys.exit(f"No key found. Put your The Odds API key in {KEY_FILE}")
    # utf-8-sig strips a BOM if present; strip() drops whitespace/newlines
    return open(KEY_FILE, encoding="utf-8-sig").read().strip()


def requests_remaining(key=None):
    """Free preflight: how many Odds API requests are left this month.

    Hits the /sports endpoint, which does NOT count against the quota, and reads
    the x-requests-remaining header.  Returns an int, or None if unknown.
    """
    try:
        key = key or get_key()
        url = f"{SPORTS_URL}?apiKey={key}"
        with urllib.request.urlopen(url, timeout=30) as r:
            rem = r.headers.get("x-requests-remaining")
        return int(float(rem)) if rem is not None else None
    except Exception as e:
        print(f"[odds] could not check remaining quota: {e}")
        return None


def fetch():
    key = get_key()
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"
           f"?regions=us&markets=outrights&oddsFormat=american"
           f"&bookmakers=draftkings&apiKey={key}")
    with urllib.request.urlopen(url) as r:
        remaining = r.headers.get("x-requests-remaining")
        data = json.load(r)
    print(f"[odds] requests remaining this month: {remaining}")

    odds = {}
    for event in data:
        for bk in event.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                for outcome in mkt.get("outcomes", []):
                    name = NAME_MAP.get(outcome["name"], outcome["name"])
                    odds[name] = outcome["price"]
    return odds


def update_csv(odds):
    path = os.path.join(DATA, "teams.csv")
    df = pd.read_csv(path)
    updated = 0
    for i, row in df.iterrows():
        if row["team"] in odds:
            df.at[i, "american_odds"] = odds[row["team"]]
            updated += 1
    df.to_csv(path, index=False)
    missing = [t for t in df["team"] if t not in odds]
    print(f"[odds] updated {updated}/{len(df)} teams in teams.csv")
    if missing:
        print(f"[odds] no DraftKings price found for: {missing}")


def american_to_prob(price):
    price = float(price)
    if price > 0:
        return 100.0 / (price + 100.0)
    return -price / (-price + 100.0)


def fetch_match_odds():
    """One request: h2h (win/draw/win) for every priced fixture -> de-vigged
    probabilities, written to data/match_odds.csv with our team names."""
    key = get_key()
    url = (f"https://api.the-odds-api.com/v4/sports/{MATCH_SPORT}/odds"
           f"?regions=us&markets=h2h&oddsFormat=american"
           f"&bookmakers=draftkings&apiKey={key}")
    with urllib.request.urlopen(url) as r:
        remaining = r.headers.get("x-requests-remaining")
        data = json.load(r)
    print(f"[match odds] requests remaining this month: {remaining}")

    rows = []
    for event in data:
        home = NAME_MAP.get(event["home_team"], event["home_team"])
        away = NAME_MAP.get(event["away_team"], event["away_team"])
        for bk in event.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                p = {}
                for outcome in mkt.get("outcomes", []):
                    name = NAME_MAP.get(outcome["name"], outcome["name"])
                    if name == home:
                        p["home"] = american_to_prob(outcome["price"])
                    elif name == away:
                        p["away"] = american_to_prob(outcome["price"])
                    elif outcome["name"] == "Draw":
                        p["draw"] = american_to_prob(outcome["price"])
                # group fixtures price 3 ways (home/draw/away); knockout h2h has no
                # draw (2-way) -> accept either, recording p_draw=0 when absent.
                if "home" in p and "away" in p:
                    draw = p.get("draw", 0.0)
                    tot = p["home"] + draw + p["away"]
                    if tot <= 0:
                        continue
                    rows.append({
                        "home": home, "away": away,
                        "p_home": p["home"] / tot,
                        "p_draw": draw / tot,
                        "p_away": p["away"] / tot,
                        "commence_time": event.get("commence_time", ""),
                    })
    df = pd.DataFrame(rows)
    out = os.path.join(DATA, "match_odds.csv")
    df.to_csv(out, index=False)
    print(f"[match odds] wrote {len(df)} priced fixtures to match_odds.csv")
    return df


if __name__ == "__main__":
    update_csv(fetch())
    fetch_match_odds()
