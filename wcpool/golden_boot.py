"""Golden Boot (top scorer) model for the tiebreaker.

Player goals are modeled as Binomial(team_total_goals, goal_share): a player
scores a fixed share of his team's goals, and team goals scale with how deep
the team advances (already captured in sim.team_goals).  The winner each sim is
the player with the most goals; we report the most likely winner and the
expected winning goal total.
"""

import numpy as np
import pandas as pd

from .results import _fold


def player_goals(sim, teams, players_df, seed=11,
                 real_team_goals=None, real_player_goals=None):
    """Sample each candidate player's tournament goal total per sim.

    Returns (goals[N, P], players) where players is the candidate DataFrame
    (only players whose team is in the field), reset-indexed to match columns.

    If real_* are given (a live tournament), goals already scored are LOCKED and
    only the team's remaining goals are shared out:
        final = real_goals + Binomial(team_remaining_goals, goal_share).
    Real top scorers not already in the candidate list are added (share estimated
    from their current scoring rate).
    """
    rng = np.random.default_rng(seed)
    players = players_df[players_df["team"].isin(teams.names)].reset_index(drop=True)

    real_by_fold = {}
    if real_player_goals:
        real_by_fold = {_fold(k): v for k, v in real_player_goals.items()}
        # add real scorers who aren't already candidates
        have = {_fold(p) for p in players["player"]}
        extra = []
        for name, info in real_player_goals.items():
            if _fold(name) in have or info.get("team_idx") is None:
                continue
            ti = info["team_idx"]
            tg = int(real_team_goals[ti]) if real_team_goals is not None else 0
            share = info["goals"] / tg if tg > 0 else 0.25
            extra.append({"player": name, "team": teams.names[ti],
                          "goal_share": float(min(max(share, 0.12), 0.6)),
                          "penalty_taker": 0, "american_odds": 0})
        if extra:
            players = pd.concat([players, pd.DataFrame(extra)], ignore_index=True)

    N, P = sim.n, len(players)
    goals = np.zeros((N, P), dtype=np.int32)
    for j, row in players.iterrows():
        ti = teams.idx[row["team"]]
        share = float(row["goal_share"])
        if real_team_goals is not None:
            remaining = np.clip(sim.team_goals[:, ti] - int(real_team_goals[ti]), 0, None)
            base = real_by_fold.get(_fold(row["player"]), {}).get("goals", 0)
            goals[:, j] = base + rng.binomial(remaining, share)
        else:
            goals[:, j] = rng.binomial(sim.team_goals[:, ti], share)
    return goals, players


def simulate_golden_boot(sim, teams, players_df, seed=11,
                         real_team_goals=None, real_player_goals=None):
    goals, players = player_goals(sim, teams, players_df, seed=seed,
                                  real_team_goals=real_team_goals,
                                  real_player_goals=real_player_goals)
    N, P = goals.shape

    # winner per sim (ties -> the player with higher base share, deterministic)
    # add a tiny share-based tiebreak so ties resolve toward primary scorers
    tiebreak = players["goal_share"].to_numpy() * 1e-3
    score = goals + tiebreak
    winner = np.argmax(score, axis=1)
    win_total = goals[np.arange(N), winner]

    win_prob = np.bincount(winner, minlength=P) / N
    exp_goals = goals.mean(axis=0)

    out = pd.DataFrame({
        "player": players["player"],
        "team": players["team"],
        "win%": win_prob * 100,
        "exp_goals": exp_goals,
        "p_6plus": (goals >= 6).mean(axis=0) * 100,
    }).sort_values("win%", ascending=False).reset_index(drop=True)

    summary = {
        "table": out,
        "exp_winning_total": float(win_total.mean()),
        "median_winning_total": float(np.median(win_total)),
    }
    return summary
