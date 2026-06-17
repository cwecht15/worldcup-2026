"""Full pool resolution INCLUDING the Golden Boot tiebreaker.

This lets us measure how much aligning your Golden Boot pick with your own
portfolio improves your real win rate (the points-only optimizer ignores ties).

Tiebreak rule (assumed, and stated in the output): among entries tied on points,
the winner is whoever (1) named the actual Golden Boot winner, then (2) is
closest to that player's goal total.  Remaining exact ties split evenly.

Your win probability depends only on you vs each opponent (the winner is the top
entry), so we never need to resolve opponents against each other.
"""

import numpy as np

from .optimize import popularity


def actual_boot_winner(goals, players):
    """Per-sim Golden Boot winner index and goal total (share-based tiebreak)."""
    tb = players["goal_share"].to_numpy() * 1e-3
    winner = np.argmax(goals + tb, axis=1)
    rows = np.arange(goals.shape[0])
    return winner, goals[rows, winner]


def boot_popularity(players, win_prob, gamma_b=1.2):
    """How the field picks the Golden Boot: chalk-weighted on model win%."""
    w = np.clip(win_prob, 1e-6, None) ** gamma_b
    return w / w.sum()


def evaluate(sim, teams, goals, players, win_prob, my_picks, my_boot_idx,
             my_boot_goal, M=20, gamma=1.5, gamma_b=1.2, seed=99):
    """Return (p_win_shared, p_win_strict) for a full strategy vs a modeled field.

    my_picks: list of 6 team indices.  my_boot_idx: player column index.
    my_boot_goal: your submitted goal number.  A fresh random field of M
    opponents is drawn per sim, so field randomness is integrated over sims.
    """
    rng = np.random.default_rng(seed)
    N = sim.n
    total_pts = sim.total_pts
    rows = np.arange(N)[:, None]

    winner, winner_goals = actual_boot_winner(goals, players)

    # ---- my entry ----
    my_pts = total_pts[:, list(my_picks)].sum(axis=1)
    my_correct = (my_boot_idx == winner).astype(np.int8)
    my_close = -np.abs(my_boot_goal - winner_goals)

    # ---- opponent field: [N, M] ----
    pop = popularity(teams, gamma)
    opp_pts = np.zeros((N, M))
    for t in range(1, 7):
        members = np.array(teams.tier_members(t))
        w = pop[members] / pop[members].sum()
        picks = rng.choice(members, size=(N, M), p=w)        # global team idxs
        opp_pts += total_pts[rows, picks]

    bp = boot_popularity(players, win_prob, gamma_b)
    opp_boot = rng.choice(len(players), size=(N, M), p=bp)
    # opponents guess a goal number clustered near the popular total (~7)
    opp_goal = np.clip(np.rint(rng.normal(6.8, 1.4, size=(N, M))), 3, 13)

    opp_correct = (opp_boot == winner[:, None]).astype(np.int8)
    opp_close = -np.abs(opp_goal - winner_goals[:, None])

    # ---- compare me vs each opponent, lexicographic (pts, correct, close) ----
    pts_gt = my_pts[:, None] > opp_pts
    pts_eq = my_pts[:, None] == opp_pts
    cor_gt = my_correct[:, None] > opp_correct
    cor_eq = my_correct[:, None] == opp_correct
    cls_gt = my_close[:, None] > opp_close
    cls_eq = my_close[:, None] == opp_close

    i_beat = pts_gt | (pts_eq & (cor_gt | (cor_eq & cls_gt)))
    i_tie = pts_eq & cor_eq & cls_eq
    i_geq = i_beat | i_tie

    co_leader = i_geq.all(axis=1)
    n_tied = i_tie.sum(axis=1)
    share = np.where(co_leader, 1.0 / (1.0 + n_tied), 0.0)
    strict = (i_beat.all(axis=1)).astype(float)

    # how often is the boot tiebreaker actually invoked for me?
    # (I'm a points co-leader AND at least one opponent matches my points)
    tie_invoked = (my_pts[:, None] >= opp_pts).all(axis=1) & pts_eq.any(axis=1)

    return {"shared": float(share.mean()), "strict": float(strict.mean()),
            "tie_invoked": float(tie_invoked.mean())}
