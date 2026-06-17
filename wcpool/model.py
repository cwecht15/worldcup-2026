"""Data loading, market odds handling, the match goal model, and the
third-place-slot matching solver."""

from itertools import combinations
import os

import numpy as np
import pandas as pd

from . import wcdata

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


# ---------------------------------------------------------------------------
# Team data
# ---------------------------------------------------------------------------
class Teams:
    """Container for the 48-team field with index-based lookups."""

    def __init__(self, df):
        self.df = df.reset_index(drop=True)
        self.n = len(self.df)
        self.names = self.df["team"].tolist()
        self.idx = {name: i for i, name in enumerate(self.names)}
        self.elo = self.df["elo"].to_numpy(float)
        self.tier = self.df["tier"].to_numpy(int)
        self.group = self.df["group"].tolist()
        self.host = self.df["host"].to_numpy(int)
        self.american = self.df["american_odds"].to_numpy(float)
        # groups: letter -> list of 4 team indices, in CSV order
        self.groups = {g: [] for g in wcdata.GROUP_LETTERS}
        for i, g in enumerate(self.group):
            self.groups[g].append(i)

    def tier_members(self, tier):
        return [i for i in range(self.n) if self.tier[i] == tier]


def load_teams(path=None):
    path = path or os.path.join(DATA_DIR, "teams.csv")
    df = pd.read_csv(path)
    return Teams(df)


def load_players(path=None):
    path = path or os.path.join(DATA_DIR, "players.csv")
    return pd.read_csv(path)


# ---------------------------------------------------------------------------
# Odds helpers
# ---------------------------------------------------------------------------
def american_to_prob(american):
    """American moneyline -> implied probability (with vig)."""
    american = np.asarray(american, float)
    pos = american > 0
    p = np.where(pos, 100.0 / (american + 100.0), -american / (-american + 100.0))
    return p


def devig_title_probs(american):
    """Normalize implied title probabilities to sum to 1 (proportional de-vig)."""
    p = american_to_prob(american)
    return p / p.sum()


# ---------------------------------------------------------------------------
# Match goal model
# ---------------------------------------------------------------------------
def effective_elo(teams, home_adv):
    """Elo with a fixed host bump baked in (hosts get it in every match)."""
    return teams.elo + teams.host * home_adv


def match_lambdas(elo_i, elo_j, beta, base):
    """Expected goals for both sides of a match from their (effective) Elo.

    Double-Poisson: lam_i = base * exp(+beta * d/400), lam_j = base * exp(-beta * d/400)
    where d = elo_i - elo_j.  `base` sets the overall scoring level; `beta`
    controls how strongly a rating edge tilts the goal expectation.
    """
    d = (elo_i - elo_j) / 400.0
    lam_i = base * np.exp(beta * d)
    lam_j = base * np.exp(-beta * d)
    return lam_i, lam_j


def win_expectancy(elo_i, elo_j):
    """Elo expected score in [0,1] (used for penalty-shootout tilt)."""
    return 1.0 / (1.0 + 10.0 ** (-(elo_i - elo_j) / 400.0))


def match_outcome_probs_from_d(d, beta, base, kmax=18):
    """Analytic double-Poisson outcome probs for rating gaps `d` (vectorized).

    Returns (p_win_i, p_draw, p_win_j), each shaped like d.  kmax=18 truncates
    the goal distribution far beyond any realistic lambda (<4).
    """
    d = np.atleast_1d(np.asarray(d, float))
    la, lb = match_lambdas(d, np.zeros_like(d), beta, base)
    k = np.arange(kmax + 1)
    fact = np.cumprod(np.concatenate(([1.0], np.arange(1, kmax + 1))))
    pa = np.exp(-la)[:, None] * la[:, None] ** k / fact          # [D, K]
    pb = np.exp(-lb)[:, None] * lb[:, None] ** k / fact
    cdf_b = np.cumsum(pb, axis=1)
    p_draw = (pa * pb).sum(axis=1)
    # P(X > Y) = sum_k pa[k] * P(Y < k)
    p_i = (pa[:, 1:] * cdf_b[:, :-1]).sum(axis=1)
    p_j = np.clip(1.0 - p_i - p_draw, 0.0, 1.0)
    return p_i, p_draw, p_j


# ---------------------------------------------------------------------------
# Match odds (group fixtures)
# ---------------------------------------------------------------------------
def load_match_odds(teams, path=None):
    """Load de-vigged match odds (from fetch_odds.py) keyed to team indices.

    Returns dict with arrays {"i", "j", "p_i", "p_draw", "p_j", "two_way"}
    (two_way = p_i / (p_i + p_j), the draw-free win share), or None if the
    file doesn't exist.  Fixtures with unknown team names are skipped.
    """
    path = path or os.path.join(DATA_DIR, "match_odds.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    ii, jj, pi, pd_, pj = [], [], [], [], []
    for _, r in df.iterrows():
        if r["home"] not in teams.idx or r["away"] not in teams.idx:
            continue
        ii.append(teams.idx[r["home"]])
        jj.append(teams.idx[r["away"]])
        pi.append(float(r["p_home"]))
        pd_.append(float(r["p_draw"]))
        pj.append(float(r["p_away"]))
    if not ii:
        return None
    pi, pj = np.array(pi), np.array(pj)
    return {"i": np.array(ii, dtype=int), "j": np.array(jj, dtype=int),
            "p_i": pi, "p_draw": np.array(pd_), "p_j": pj,
            "two_way": pi / (pi + pj)}


# ---------------------------------------------------------------------------
# Third-place slot assignment
# ---------------------------------------------------------------------------
def _solve_matching(qualifying_groups):
    """Assign the 8 reserved third-place slots to the 8 qualifying groups.

    Returns dict slot -> group letter, or None if no perfect matching exists.
    Deterministic (slots processed in fixed order, groups tried alphabetically).
    """
    slots = list(wcdata.THIRD_SLOTS.keys())
    qg = sorted(qualifying_groups)
    assignment = {}
    used = set()

    def backtrack(k):
        if k == len(slots):
            return True
        slot = slots[k]
        allowed = wcdata.THIRD_SLOTS[slot]
        for g in qg:
            if g in used or g not in allowed:
                continue
            assignment[slot] = g
            used.add(g)
            if backtrack(k + 1):
                return True
            used.discard(g)
            del assignment[slot]
        return False

    if backtrack(0):
        return dict(assignment)
    return None


def build_third_place_assignments():
    """Precompute slot->group assignments for all C(12,8)=495 qualifying sets.

    Keyed by frozenset of the 8 qualifying group letters.
    """
    table = {}
    failures = []
    for combo in combinations(wcdata.GROUP_LETTERS, 8):
        m = _solve_matching(set(combo))
        if m is None:
            failures.append(combo)
        table[frozenset(combo)] = m
    return table, failures
