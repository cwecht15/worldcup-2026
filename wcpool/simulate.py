"""Vectorized Monte Carlo simulation of the actual 2026 World Cup bracket.

Produces, per simulation, the pool points each of the 48 teams earns and the
total goals each team scores (for the Golden Boot model).
"""

import numpy as np

from . import wcdata
from .model import match_lambdas, win_expectancy

GROUP_MATCHES = [(0, 1), (2, 3), (0, 2), (1, 3), (0, 3), (1, 2)]  # round robin of 4


def _scatter_add(target, rows, cols, vals):
    """target[rows, cols] += vals, allowing repeated indices."""
    np.add.at(target, (rows, cols), vals)


class SimResult:
    def __init__(self, teams, total_pts, team_goals, reach_counts,
                 roundwin_counts, champion, n_sims, tracked_outcomes=None):
        self.teams = teams
        self.total_pts = total_pts          # [N, 48] pool points per team
        self.team_goals = team_goals        # [N, 48] goals scored per team
        self.reach_counts = reach_counts     # [48] times reached the knockout (R32)
        self.roundwin_counts = roundwin_counts  # dict round -> [48] win counts
        self.champion = champion            # [N] champion team index
        self.n = n_sims
        # [N, G] per-sim outcome of each tracked game (None if track was empty):
        # 0=home win, 1=draw, 2=away win, -1=these two teams didn't meet this sim
        self.tracked_outcomes = tracked_outcomes

    def expected_points(self):
        return self.total_pts.mean(axis=0)

    def round_prob(self, round_name):
        return self.roundwin_counts[round_name] / self.n

    def reach_prob(self):
        return self.reach_counts / self.n

    def title_prob(self):
        return np.bincount(self.champion, minlength=self.teams.n) / self.n


def simulate(teams, beta, base, home_adv, third_table, n_sims=100_000, seed=0,
             strength=None, fixed=None, track=None):
    """Monte Carlo of the bracket. If `fixed` is given (from results.Results.fixed()),
    matches that have actually been played are forced to their real outcome and only
    the remaining matches are simulated (a conditional simulation).

    If `track` is given (a list of {"code": lo*nteam+hi, "home_idx", "away_idx"},
    one per upcoming game we want to analyze), the per-sim outcome of each tracked
    game is recorded in SimResult.tracked_outcomes [N, G] (oriented to home_idx).
    Recording only reads simulated values, so it never changes any other output."""
    rng = np.random.default_rng(seed)
    N = n_sims
    nteam = teams.n
    base_strength = teams.elo if strength is None else strength
    eff_elo = base_strength + teams.host * home_adv

    total_pts = np.zeros((N, nteam), dtype=np.float32)
    team_goals = np.zeros((N, nteam), dtype=np.int32)

    # tracked games: per-sim outcome (0 home / 1 draw / 2 away / -1 didn't meet)
    track = track or []
    G = len(track)
    tracked_outcomes = np.full((N, G), -1, dtype=np.int8) if G else None
    track_by_code = {int(t["code"]): g for g, t in enumerate(track)}
    track_home = {int(t["code"]): int(t["home_idx"]) for t in track}

    GL = wcdata.GROUP_LETTERS
    winners = np.empty((N, 12), dtype=np.int32)
    runners = np.empty((N, 12), dtype=np.int32)
    thirds = np.empty((N, 12), dtype=np.int32)
    third_score = np.empty((N, 12), dtype=np.float64)

    rows = np.repeat(np.arange(N), 4)

    # ----- Group stage -----
    for gi, g in enumerate(GL):
        members = np.array(teams.groups[g])           # 4 global indices
        pts = np.zeros((N, 4), dtype=np.float64)
        gf = np.zeros((N, 4), dtype=np.float64)
        ga = np.zeros((N, 4), dtype=np.float64)
        for a, b in GROUP_MATCHES:
            la, lb = match_lambdas(eff_elo[members[a]], eff_elo[members[b]], beta, base)
            xa = rng.poisson(la, size=N)
            xb = rng.poisson(lb, size=N)
            if fixed is not None:                       # force a completed group game
                i, j = int(members[a]), int(members[b])
                lo, hi = (i, j) if i < j else (j, i)
                code = lo * nteam + hi
                if fixed["present"][code]:
                    goals_i = fixed["g_lo"][code] if i == lo else fixed["g_hi"][code]
                    goals_j = fixed["g_hi"][code] if i == lo else fixed["g_lo"][code]
                    xa = np.full(N, goals_i, dtype=xa.dtype)
                    xb = np.full(N, goals_j, dtype=xb.dtype)
            awin = xa > xb
            bwin = xb > xa
            draw = ~(awin | bwin)
            pts[:, a] += np.where(awin, 3.0, np.where(draw, 1.0, 0.0))
            pts[:, b] += np.where(bwin, 3.0, np.where(draw, 1.0, 0.0))
            gf[:, a] += xa; ga[:, a] += xb
            gf[:, b] += xb; ga[:, b] += xa
            if track_by_code:                           # record tracked group game
                i, j = int(members[a]), int(members[b])
                lo, hi = (i, j) if i < j else (j, i)
                code = lo * nteam + hi
                g = track_by_code.get(code)
                if g is not None:
                    home_is_a = track_home[code] == i
                    hwin = awin if home_is_a else bwin  # home team wins
                    awin_h = bwin if home_is_a else awin  # away team wins
                    tracked_outcomes[:, g] = np.where(hwin, 0, np.where(awin_h, 2, 1))

        # team-level pool points for group stage = win/draw points already in pts
        _scatter_add(total_pts, rows, np.tile(members, N), pts.ravel())
        _scatter_add(team_goals, rows, np.tile(members, N), gf.ravel().astype(np.int32))

        gd = gf - ga
        noise = rng.random((N, 4)) * 1e-3
        key = pts * 1e6 + gd * 1e3 + gf + noise
        order = np.argsort(-key, axis=1)              # [N,4] local positions
        rN = np.arange(N)
        winners[:, gi] = members[order[:, 0]]
        runners[:, gi] = members[order[:, 1]]
        third_local = order[:, 2]
        thirds[:, gi] = members[third_local]
        third_score[:, gi] = key[rN, third_local]

    # ----- Best 8 third-place teams -> slot assignment -----
    arg = np.argsort(-third_score, axis=1)            # [N,12] group cols best->worst
    top8 = arg[:, :8]
    qual = np.zeros((N, 12), dtype=bool)
    np.put_along_axis(qual, top8, True, axis=1)
    powers = (1 << np.arange(12)).astype(np.int64)
    mask = qual.astype(np.int64) @ powers

    slot_ids = list(wcdata.THIRD_SLOTS.keys())
    third_slot_team = {s: np.full(N, -1, dtype=np.int32) for s in slot_ids}
    for m in np.unique(mask):
        groups_in = [GL[p] for p in range(12) if (m >> p) & 1]
        assign = third_table[frozenset(groups_in)]
        sel = mask == m
        for slot, grp in assign.items():
            third_slot_team[slot][sel] = thirds[sel, GL.index(grp)]

    # ----- Build the 32 knockout entrants in bracket-fold order -----
    def resolve(part):
        kind, ref = part
        if kind == "W":
            return winners[:, GL.index(ref)]
        if kind == "R":
            return runners[:, GL.index(ref)]
        return third_slot_team[ref]                   # ("3", slot_id)

    entrants = []
    for mnum in wcdata.BRACKET_ORDER:
        p1, p2 = wcdata.R32_MATCHES[mnum]
        entrants.append(resolve(p1))
        entrants.append(resolve(p2))
    current = np.stack(entrants, axis=1)              # [N, 32]

    reach_counts = np.bincount(current.ravel(), minlength=nteam)
    roundwin_counts = {}

    rng_ko = rng
    for rnd in wcdata.ROUND_ORDER:
        K = current.shape[1]
        pair = current.reshape(N, K // 2, 2)
        ta = pair[:, :, 0]
        tb = pair[:, :, 1]
        ea = eff_elo[ta]; eb = eff_elo[tb]
        la, lb = match_lambdas(ea, eb, beta, base)
        ga_ = rng_ko.poisson(la)
        gb_ = rng_ko.poisson(lb)
        # extra time for ties
        tie = ga_ == gb_
        eta = rng_ko.poisson(la * 0.33)
        etb = rng_ko.poisson(lb * 0.33)
        ga2 = ga_ + np.where(tie, eta, 0)
        gb2 = gb_ + np.where(tie, etb, 0)
        a_better = ga2 > gb2
        still_tie = ga2 == gb2
        we_a = win_expectancy(ea, eb)
        pens_a = rng_ko.random(ga2.shape) < we_a
        a_wins = a_better | (still_tie & pens_a)

        if fixed is not None:                           # force completed knockout games
            lo = np.minimum(ta, tb); hi = np.maximum(ta, tb)
            codes = lo * nteam + hi
            pres = fixed["present"][codes]
            if pres.any():
                glo = fixed["g_lo"][codes]; ghi = fixed["g_hi"][codes]
                win = fixed["winner"][codes]
                ga_real = np.where(ta <= tb, glo, ghi)
                gb_real = np.where(ta <= tb, ghi, glo)
                ga2 = np.where(pres, ga_real, ga2)
                gb2 = np.where(pres, gb_real, gb2)
                a_wins = np.where(pres, win == ta, a_wins)

        winner = np.where(a_wins, ta, tb)
        if track_by_code:                               # record tracked KO games
            lo_ko = np.minimum(ta, tb)
            hi_ko = np.maximum(ta, tb)
            codes_ko = lo_ko * nteam + hi_ko            # [N, P]
            rN = np.arange(N)
            for code, g in track_by_code.items():
                m = codes_ko == code                    # [N, P]; pair meets <=1x
                col = m.argmax(axis=1)
                w_row = winner[rN, col]                 # winner where present
                # only write -1 cells: a tracked group game stays its group result
                take = m.any(axis=1) & (tracked_outcomes[:, g] == -1)
                if take.any():
                    tracked_outcomes[take, g] = np.where(
                        w_row[take] == track_home[code], 0, 2)
        # team goals scored this match
        P = K // 2
        rrows = np.repeat(np.arange(N), P)
        _scatter_add(team_goals, rrows, ta.ravel(), ga2.ravel().astype(np.int32))
        _scatter_add(team_goals, rrows, tb.ravel(), gb2.ravel().astype(np.int32))
        # pool points to the winners of this round
        pts_r = wcdata.ROUND_POINTS[rnd]
        _scatter_add(total_pts, rrows, winner.ravel(),
                     np.full(winner.size, pts_r, dtype=np.float32))
        roundwin_counts[rnd] = np.bincount(winner.ravel(), minlength=nteam)

        current = winner

    champion = current[:, 0]
    return SimResult(teams, total_pts, team_goals, reach_counts,
                     roundwin_counts, champion, N,
                     tracked_outcomes=tracked_outcomes)
