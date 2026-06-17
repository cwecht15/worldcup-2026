"""Live tournament results -> 'fixed match' overrides for a conditional sim.

The base simulator (simulate.py) replays the whole tournament from scratch.  When
real results are available (data/results.json, produced by fetch_results.py from
football-data.org), we *fix* the matches that have actually been played: their
scores/winners are forced, and only the remaining matches are simulated.  Then
accrued pool points, win %, Golden Boot, and "blocked from winning" all reflect
where the tournament actually is.

A match is keyed by the unordered pair of its two teams (each pair meets at most
once in the whole bracket), encoded as one integer `lo*nteam + hi`.  The simulator
looks every match up in these arrays and, if present, uses the real outcome.
"""

import json
import os
import unicodedata

import numpy as np

from . import wcdata

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DEFAULT_PATH = os.path.join(DATA_DIR, "results.json")

# football-data.org stage -> our round key (None = no pool points, e.g. 3rd place)
STAGE_MAP = {
    "GROUP_STAGE": "GROUP", "GROUP": "GROUP",
    "LAST_32": "R32", "ROUND_OF_32": "R32",
    "LAST_16": "R16", "ROUND_OF_16": "R16",
    "QUARTER_FINALS": "QF", "QUARTER_FINAL": "QF", "QUARTERFINALS": "QF",
    "SEMI_FINALS": "SF", "SEMI_FINAL": "SF", "SEMIFINALS": "SF",
    "FINAL": "FINAL",
    "THIRD_PLACE": None, "3RD_PLACE_FINAL": None,
}

# sheet/api spelling (folded) -> teams.csv canonical, where they differ
TEAM_ALIASES = {
    "unitedstates": "USA", "usa": "USA",
    "korearepublic": "South Korea", "southkorea": "South Korea",
    "republicofkorea": "South Korea",
    "czechrepublic": "Czechia", "czechia": "Czechia",
    "cotedivoire": "Ivory Coast", "ivorycoast": "Ivory Coast",
    "turkiye": "Turkey", "turkey": "Turkey",
    "bosniaandherzegovina": "Bosnia", "bosniaherzegovina": "Bosnia",
    "congodr": "DR Congo", "drcongo": "DR Congo", "irancng": "DR Congo",
    "iriran": "Iran", "iran": "Iran", "iranislamicrepublic": "Iran",
    "uzbekistan": "Uzbekistan", "capeverde": "Cape Verde",
    "caboverde": "Cape Verde", "capeverdeislands": "Cape Verde",
    "saudiarabia": "Saudi Arabia",
    "southafrica": "South Africa", "newzealand": "New Zealand",
}


def _fold(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return "".join(ch for ch in s.lower() if ch.isalnum())


def resolve_team(name, teams):
    """team name (api/sheet spelling) -> teams index, or None."""
    if name is None:
        return None
    raw = str(name).strip()
    if not raw:
        return None
    if raw in teams.idx:
        return teams.idx[raw]
    key = _fold(raw)
    for i, nm in enumerate(teams.names):
        if _fold(nm) == key:
            return i
    canon = TEAM_ALIASES.get(key)
    if canon and canon in teams.idx:
        return teams.idx[canon]
    return None


class Results:
    """Parsed live results, ready to condition the simulator on."""

    def __init__(self, teams):
        n = teams.n
        self.n = n
        self.as_of = None
        self.n_matches = 0
        # pair-keyed override arrays (index = lo*n + hi)
        self.present = np.zeros(n * n, dtype=bool)
        self.g_lo = np.zeros(n * n, dtype=np.int32)
        self.g_hi = np.zeros(n * n, dtype=np.int32)
        self.winner = np.full(n * n, -1, dtype=np.int32)   # team idx, -1 = decide by goals
        # real goals already scored
        self.real_team_goals = np.zeros(n, dtype=np.int32)
        self.real_player_goals = {}    # player name -> {"team", "goals", "team_idx"}
        self.matches = []              # parsed completed matches (for standings/scores)
        self.unmatched = []            # team names we couldn't resolve

    def fixed(self):
        """Dict passed to simulate.simulate(fixed=...)."""
        return {"n": self.n, "present": self.present, "g_lo": self.g_lo,
                "g_hi": self.g_hi, "winner": self.winner}

    def _add_match(self, i, j, gi, gj, round_key, winner_idx):
        n = self.n
        lo, hi = (i, j) if i < j else (j, i)
        code = lo * n + hi
        self.present[code] = True
        self.g_lo[code] = gi if i == lo else gj
        self.g_hi[code] = gj if i == lo else gi
        self.winner[code] = winner_idx if round_key != "GROUP" else -1
        self.real_team_goals[i] += gi
        self.real_team_goals[j] += gj
        self.matches.append({"i": i, "j": j, "gi": int(gi), "gj": int(gj),
                             "round": round_key})
        self.n_matches += 1


def load_results(teams, path=None):
    """Load data/results.json -> Results, or None if the file is absent/empty."""
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    matches = data.get("matches", [])
    if not matches:
        return None

    res = Results(teams)
    res.as_of = data.get("as_of")
    for m in matches:
        if str(m.get("status", "FINISHED")).upper() not in ("FINISHED", "AWARDED"):
            continue
        round_key = STAGE_MAP.get(str(m.get("stage", "GROUP_STAGE")).upper(), "GROUP")
        if round_key is None:                      # 3rd-place playoff: no pool points
            continue
        i = resolve_team(m.get("home"), teams)
        j = resolve_team(m.get("away"), teams)
        if i is None or j is None:
            if i is None:
                res.unmatched.append(m.get("home"))
            if j is None:
                res.unmatched.append(m.get("away"))
            continue
        gi = int(m.get("home_goals", 0) or 0)
        gj = int(m.get("away_goals", 0) or 0)
        winner_idx = -1
        if round_key != "GROUP":
            w = str(m.get("winner", "")).upper()
            if w == "HOME":
                winner_idx = i
            elif w == "AWAY":
                winner_idx = j
            else:                                   # fall back to goals if not provided
                winner_idx = i if gi >= gj else j
        res._add_match(i, j, gi, gj, round_key, winner_idx)

    for s in data.get("scorers", []):
        name = s.get("player")
        if not name:
            continue
        ti = resolve_team(s.get("team"), teams)
        res.real_player_goals[name] = {
            "team": s.get("team"), "team_idx": ti, "goals": int(s.get("goals", 0) or 0)}
    return res
