"""Pool scoring rules, in one place.

Per team, summed across all 6 of your picks:
  group tie  = 1     group win = 3
  R32 win    = 5     R16 win   = 7
  QF win     = 10    SF win    = 15
  Champion (final win) = 20

The simulator (simulate.py) applies these while building `total_pts[N, 48]`.
This module exposes the rules for verification and a portfolio helper.
"""

import numpy as np

from . import wcdata


def team_points(group_wins, group_draws, rounds_won):
    """Pure scoring function for one team (used in unit checks).

    group_wins/group_draws: ints from the 3 group matches.
    rounds_won: iterable of round names won, e.g. {"R32","R16","QF","SF","FINAL"}.
    """
    pts = group_wins * wcdata.GROUP_WIN_PTS + group_draws * wcdata.GROUP_DRAW_PTS
    for r in rounds_won:
        pts += wcdata.ROUND_POINTS[r]
    return pts


def portfolio_scores(total_pts, picks):
    """Per-sim total for a portfolio (list of team indices). Returns [N]."""
    return total_pts[:, list(picks)].sum(axis=1)


# Sanity reference: a champion that won all 3 group games scores
#   3*3 + 5 + 7 + 10 + 15 + 20 = 66
MAX_SINGLE_TEAM = team_points(3, 0, wcdata.ROUND_ORDER)  # == 66
