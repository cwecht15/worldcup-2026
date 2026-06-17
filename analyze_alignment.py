"""Quantify the Golden Boot alignment question:

  A) With a fixed (Spain-core) portfolio, does naming a player on one of YOUR
     teams beat naming the raw favorite?  (isolates the tiebreaker effect)
  B) Does aligning at the Tier-1 pick (France->Mbappe, England->Kane) beat
     Spain+favorite once the tiebreaker is included?

Run:  python analyze_alignment.py
"""

import numpy as np

from wcpool import golden_boot, tiebreaker
from run import build_sim


def pidx(players, name):
    m = players.index[players["player"] == name]
    if len(m) == 0:
        raise KeyError(f"{name} not in candidate list")
    return int(m[0])


def main():
    teams, players_df, third_table, beta, target, strength, sim = build_sim(
        quick=False, n_full=80_000)
    print(f"\n[model] beta={beta}, {sim.n:,} sims\n")

    goals, players = golden_boot.player_goals(sim, teams, players_df)
    P = len(players)
    win_prob = np.bincount(
        tiebreaker.actual_boot_winner(goals, players)[0], minlength=P) / sim.n

    ti = teams.idx
    core = [ti["Germany"], ti["Switzerland"], ti["Canada"],
            ti["South Korea"], ti["New Zealand"]]   # T2..T6 (recommended)

    KANE = pidx(players, "Harry Kane")
    MBAPPE = pidx(players, "Kylian Mbappe")
    YAMAL = pidx(players, "Lamine Yamal")

    def show(label, picks, boot_idx, boot_goal):
        r = tiebreaker.evaluate(sim, teams, goals, players, win_prob,
                                picks, boot_idx, boot_goal, M=20)
        nm = players.iloc[boot_idx]["player"]
        print(f"  {label:<34} boot={nm:<14} g={boot_goal}  "
              f"win={r['shared']*100:5.2f}%  (strict {r['strict']*100:4.2f}%)")
        return r

    print("Tiebreaker is invoked (you're tied on points at the top) "
          f"~{tiebreaker.evaluate(sim, teams, goals, players, win_prob, [ti['Spain']]+core, KANE, 8, M=20)['tie_invoked']*100:.1f}% "
          "of the time.\n")

    print("== A) FIXED Spain-core portfolio, vary the Golden Boot pick ==")
    spain = [ti["Spain"]] + core
    show("Spain core + Mbappe (favorite)", spain, MBAPPE, 8)
    show("Spain core + Kane (favorite)", spain, KANE, 8)
    show("Spain core + Yamal (YOUR team)", spain, YAMAL, 7)

    print("\n== B) Align at Tier-1 (portfolio + boot together) ==")
    show("Spain  + Mbappe (no alignment)", [ti["Spain"]] + core, MBAPPE, 8)
    show("France + Mbappe (aligned)", [ti["France"]] + core, MBAPPE, 8)
    show("England+ Kane (aligned)", [ti["England"]] + core, KANE, 8)

    print("\n(win% = share-of-pool win incl. tiebreaker, M=20 opponents, "
          "chalk-weighted field; strict = sole-winner only)")


if __name__ == "__main__":
    main()
