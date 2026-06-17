"""Golden Boot (top scorer) model for the tiebreaker.

Player goals are modeled as Binomial(team_total_goals, goal_share): a player
scores a fixed share of his team's goals, and team goals scale with how deep
the team advances (already captured in sim.team_goals).  The winner each sim is
the player with the most goals; we report the most likely winner and the
expected winning goal total.
"""

import numpy as np
import pandas as pd


def player_goals(sim, teams, players_df, seed=11):
    """Sample each candidate player's tournament goal total per sim.

    Returns (goals[N, P], players) where players is the filtered DataFrame
    (only players whose team is in the field), reset-indexed to match columns.
    """
    rng = np.random.default_rng(seed)
    players = players_df[players_df["team"].isin(teams.names)].reset_index(drop=True)
    N = sim.n
    P = len(players)
    goals = np.zeros((N, P), dtype=np.int32)
    for j, row in players.iterrows():
        ti = teams.idx[row["team"]]
        tg = sim.team_goals[:, ti]
        share = float(row["goal_share"])
        goals[:, j] = rng.binomial(tg, share)
    return goals, players


def simulate_golden_boot(sim, teams, players_df, seed=11):
    goals, players = player_goals(sim, teams, players_df, seed=seed)
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
