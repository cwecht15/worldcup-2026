"""End-to-end: load data -> calibrate -> simulate -> optimize -> Golden Boot.

Usage:  python run.py            (full run, N=100k)
        python run.py --quick    (fast smoke test, small N)
"""

import argparse
import sys

import numpy as np

from wcpool import model, calibrate, simulate as sim_mod, optimize as opt, golden_boot, scoring


def names(teams, idxs):
    return [teams.names[i] for i in idxs]


def fmt_portfolio(teams, idxs):
    parts = [f"T{teams.tier[i]} {teams.names[i]}" for i in sorted(idxs, key=lambda i: teams.tier[i])]
    return " / ".join(parts)  # " / " not " | " so it survives inside markdown tables


def build_sim(quick=False, n_full=None, verbose=True, fixed=None):
    """Load data, calibrate to the market, and run the Monte Carlo.

    Returns (teams, players, third_table, beta, target, strength, sim).
    Reused by main() and the analysis scripts.  If `fixed` is provided (from
    results.Results.fixed()), the final simulation is conditioned on the matches
    already played; calibration still uses the market.
    """
    teams = model.load_teams()
    players = model.load_players()

    # ---- verification: every team in exactly one group and tier ----
    assert teams.n == 48, teams.n
    for g, members in teams.groups.items():
        assert len(members) == 4, (g, len(members))
    assert scoring.MAX_SINGLE_TEAM == 66, scoring.MAX_SINGLE_TEAM
    if verbose:
        print(f"[check] 48 teams, 12 groups of 4, max single-team score = {scoring.MAX_SINGLE_TEAM}")

    third_table, failures = model.build_third_place_assignments()
    if verbose:
        print(f"[check] third-place slot table: {len(third_table)} combos, "
              f"{len(failures)} with no valid matching")
    if failures:
        print("        WARNING unmatched combos:", failures[:5])

    n_cal = 5_000 if quick else 20_000
    if n_full is None:
        n_full = 8_000 if quick else 300_000

    match_odds = model.load_match_odds(teams)
    if verbose:
        if match_odds is None:
            print("[check] no match_odds.csv - calibrating to title odds only "
                  "(run fetch_odds.py to add fixture prices)")
        else:
            print(f"[check] {len(match_odds['i'])} priced group fixtures loaded "
                  f"for calibration")

    # Step 1: global beta sets the market-implied match concentration.  (Elo's
    # raw spread overstates single-match predictability for one-off tournament
    # games; the market implies a compressed effective spread, i.e. lower beta.)
    if verbose:
        print("\n[calibrate] step 1/2 - fitting global beta to market title odds...")
    betas = np.round(np.arange(0.30, 0.86, 0.05), 3)
    beta, _, target, _ = calibrate.calibrate_beta(
        teams, third_table, n_sims=n_cal, betas=betas, verbose=verbose)
    if verbose:
        print(f"[calibrate] chosen beta = {beta}")
        print("[calibrate] step 2/2 - per-team strength fit to market "
              "title + match odds...")
    strength, target, fit_mask = calibrate.calibrate_strengths(
        teams, third_table, beta=beta, n_sims=(4000 if quick else 60000),
        iters=(4 if quick else 12), match_odds=match_odds, verbose=verbose)
    if verbose:
        print(f"[calibrate] fitted {int(fit_mask.sum())} title-priced teams; "
              f"match odds constrain all priced fixtures")
        print(f"\n[simulate] running {n_full:,} simulations...")
    sim = sim_mod.simulate(teams, beta=beta, base=1.32, home_adv=70.0,
                           third_table=third_table, n_sims=n_full, seed=2026,
                           strength=strength, fixed=fixed)
    return teams, players, third_table, beta, target, strength, sim


def main(quick=False):
    teams, players, third_table, beta, target, strength, sim = build_sim(quick=quick)
    n_full = sim.n

    # ---- title-prob fit diagnostic ----
    tp = sim.title_prob()
    order = np.argsort(-target)[:8]
    print("\n[fit] title probability: team / market / model")
    for i in order:
        print(f"   {teams.names[i]:<12} {target[i]*100:5.1f}%   {tp[i]*100:5.1f}%")

    # ---- match-odds fit diagnostic ----
    match_odds = model.load_match_odds(teams)
    if match_odds is not None:
        eff = strength + teams.host * 70.0
        d = eff[match_odds["i"]] - eff[match_odds["j"]]
        pi, pd_, pj = model.match_outcome_probs_from_d(d, beta, 1.32)
        w_model = pi / (pi + pj)
        w_mkt = match_odds["two_way"]
        err = np.abs(w_model - w_mkt)
        print(f"\n[fit] match odds ({len(d)} fixtures): "
              f"two-way win share  mean|err|={err.mean():.3f}  max|err|={err.max():.3f}")
        print(f"      draw rate: model {pd_.mean()*100:.1f}%  market "
              f"{match_odds['p_draw'].mean()*100:.1f}%")

    # ---- EV table ----
    ev_df = opt.ev_table(sim, teams)
    print("\n[EV] expected pool points by tier (top of each tier):")
    for t in range(1, 7):
        sub = ev_df[ev_df["tier"] == t].head(4)
        print(f"  Tier {t}:")
        for _, r in sub.iterrows():
            print(f"    {r['team']:<13} EV={r['ev']:5.2f}  title={r['title%']:4.1f}%  "
                  f"reachKO={r['reachKO%']:4.0f}%  winR16={r['winR16%']:4.1f}%")

    # ---- optimizer (recommended: medium field) ----
    M = 20
    print(f"\n[optimize] leverage-aware win% (field M={M}, gamma=1.5)...")
    res = opt.optimize(sim, teams, M=M, gamma=1.5, n_sub=(4000 if quick else 100000),
                       topk=4)
    ranked = res["ranked"]
    best_combo, best_wp, best_ev = ranked[0]
    evmax_idx, evmax_wp, evmax_ev = res["ev_max"]

    print(f"\n  WIN%-MAX:  {fmt_portfolio(teams, best_combo)}")
    print(f"             P(win)={best_wp*100:.2f}%   EV={best_ev:.1f}")
    print(f"  EV-MAX:    {fmt_portfolio(teams, evmax_idx)}")
    print(f"             P(win)={evmax_wp*100:.2f}%   EV={evmax_ev:.1f}")

    print("\n  Top 8 portfolios by win%:")
    for combo, wp, ev in ranked[:8]:
        print(f"    {wp*100:5.2f}%  EV={ev:5.1f}  {fmt_portfolio(teams, combo)}")

    # ---- 2D grid: pool size x chalkiness ----
    print("\n[grid] win%-max pick by pool size x chalkiness:")
    pool_sizes = [10, 20] if quick else [10, 20, 30, 50, 76]
    chalk_levels = [("Low", 0.5), ("Medium", 1.5), ("High", 3.0)]
    grid = []  # list of {pool, chalk, gamma, combo, wp}
    for pool in pool_sizes:
        for clabel, g in chalk_levels:
            r = opt.optimize(sim, teams, M=pool - 1, gamma=g,
                             n_sub=(4000 if quick else 40000), topk=5)
            combo, wp, ev = r["ranked"][0]
            grid.append({"pool": pool, "chalk": clabel, "gamma": g,
                         "combo": combo, "wp": wp})
            print(f"   {pool:>3}p {clabel:<6}(g={g}): {fmt_portfolio(teams, combo)}  "
                  f"({wp*100:.2f}%)")

    # ---- golden boot ----
    print("\n[golden boot] top scorer model:")
    gb = golden_boot.simulate_golden_boot(sim, teams, players)
    for _, r in gb["table"].head(6).iterrows():
        print(f"   {r['player']:<20} ({r['team']:<11}) win={r['win%']:4.1f}%  "
              f"expG={r['exp_goals']:.2f}  P(6+)={r['p_6plus']:.0f}%")
    print(f"   expected winning total = {gb['exp_winning_total']:.1f} goals "
          f"(median {gb['median_winning_total']:.0f})")

    write_recommendation(teams, beta, target, tp, ev_df, res, grid, gb, M, n_full, sim)
    print("\n[done] wrote RECOMMENDATION.md")


def write_recommendation(teams, beta, target, tp, ev_df, res, grid, gb, M, n_full, sim):
    ranked = res["ranked"]
    best_combo, best_wp, best_ev = ranked[0]
    evmax_idx, evmax_wp, evmax_ev = res["ev_max"]

    # which tiers does win%-max differ from EV-max?
    diffs = []
    for a, e in zip(sorted(best_combo, key=lambda i: teams.tier[i]),
                    sorted(evmax_idx, key=lambda i: teams.tier[i])):
        if a != e:
            diffs.append(f"T{teams.tier[a]} {teams.names[a]} (over {teams.names[e]})")

    L = []
    L.append("# 2026 World Cup Pool - Recommendation\n")
    L.append(f"_Monte Carlo: {n_full:,} simulations of the real bracket, "
             f"goal model calibrated to DraftKings title odds AND per-fixture "
             f"match odds (beta={beta})._\n")

    L.append("## TL;DR\n")
    L.append(f"**Picks:** {fmt_portfolio(teams, best_combo)}\n\n")
    L.append(f"**Golden Boot:** {gb['table'].iloc[0]['player']} "
             f"({round(gb['exp_winning_total'])} goals)\n\n")
    L.append(f"This lineup wins a {M}-opponent pool (21 entries) **{best_wp*100:.1f}%** "
             f"of the time vs a {100/(M+1):.1f}% fair share - about "
             f"{best_wp*(M+1):.1f}x edge.\n")
    L.append("\n**Durable edges (robust to assumptions):**\n")
    L.append("- **Fade the Tier-2 chalk:** take a strong, less-popular T2 team instead "
             "of the odds-on favorite the field piles onto.\n")
    L.append("- **Soft-path value in T3/T4:** the best EV in the middle tiers comes from "
             "teams in weak groups, not the tier's betting favorite.\n")
    L.append("- Top-tier and bottom-tier picks are close to chalk (the favorite is also "
             "the best pick); the leverage lives in the middle.\n")
    L.append(f"\n_Note: the top several portfolios are within Monte-Carlo noise of each "
             f"other (~0.2%); treat the exact #1 as one of a cluster. The consistent "
             f"signals above matter more than the precise ranking._\n")

    L.append("\n## The picks (one per tier)\n")
    L.append("**Recommended (maximize chance of winning the pool):**\n")
    L.append(f"> {fmt_portfolio(teams, best_combo)}\n")
    L.append(f"- Modeled P(finish 1st) among {M} opponents (21 total entries): "
             f"**{best_wp*100:.2f}%** (vs {100/(M+1):.2f}% if everyone were equal)\n")
    L.append(f"- Expected points: {best_ev:.1f}\n")
    L.append("\n**Pure expected-points lineup (for comparison):**\n")
    L.append(f"> {fmt_portfolio(teams, evmax_idx)}\n")
    L.append(f"- P(win)={evmax_wp*100:.2f}%, EV={evmax_ev:.1f}\n")
    if diffs:
        L.append(f"- Win%-max differs from EV-max only at: {', '.join(diffs)} - "
                 f"a small EV give-up for lower ownership.\n")
    else:
        L.append("- Win%-max equals EV-max here (the value picks are also under-owned).\n")

    L.append("\n## Why these picks\n")
    ev_lookup = {teams.names[i]: ev_df[ev_df['team']==teams.names[i]].iloc[0] for i in best_combo}
    for i in sorted(best_combo, key=lambda i: teams.tier[i]):
        r = ev_df[ev_df["team"] == teams.names[i]].iloc[0]
        tierfav = ev_df[ev_df["tier"] == teams.tier[i]].iloc[0]["team"]
        note = "tier EV leader" if tierfav == teams.names[i] else f"leverage vs chalk ({tierfav})"
        L.append(f"- **T{teams.tier[i]} {teams.names[i]}** (Group {teams.group[i]}): "
                 f"EV {r['ev']:.2f}, title {r['title%']:.1f}%, reach KO {r['reachKO%']:.0f}%, "
                 f"win R16 {r['winR16%']:.1f}% - {note}.\n")

    L.append("\n## Expected points by tier (top candidates)\n")
    L.append("| Tier | Team | EV | Title% | ReachKO% | WinR32% | WinR16% | WinQF% |\n")
    L.append("|---|---|---|---|---|---|---|---|\n")
    for t in range(1, 7):
        for _, r in ev_df[ev_df["tier"] == t].head(4).iterrows():
            L.append(f"| {t} | {r['team']} | {r['ev']:.2f} | {r['title%']:.1f} | "
                     f"{r['reachKO%']:.0f} | {r['winR32%']:.1f} | {r['winR16%']:.1f} | "
                     f"{r['winQF%']:.1f} |\n")

    L.append("\n## Top portfolios by win probability\n")
    L.append("| Rank | P(win) | EV | Portfolio |\n|---|---|---|---|\n")
    for k, (combo, wp, ev) in enumerate(ranked[:10], 1):
        L.append(f"| {k} | {wp*100:.2f}% | {ev:.1f} | {fmt_portfolio(teams, combo)} |\n")

    # ---- 2D reference matrix: pool size x chalkiness ----
    abbr = {"South Korea": "S.Korea", "Switzerland": "Switz", "Netherlands": "Neths"}

    def short(name):
        return abbr.get(name, name)

    def pick_in_tier(combo, t):
        return [teams.names[i] for i in combo if teams.tier[i] == t][0]

    tier_choices = {t: set() for t in range(1, 7)}
    for c in grid:
        for i in c["combo"]:
            tier_choices[teams.tier[i]].add(teams.names[i])
    const_tiers = [t for t in range(1, 7) if len(tier_choices[t]) == 1]
    vary_tiers = [t for t in range(1, 7) if len(tier_choices[t]) > 1]
    core_str = " / ".join(f"T{t} {next(iter(tier_choices[t]))}" for t in const_tiers)

    L.append("\n## Pick matrix: pool size x chalkiness (reference)\n")
    L.append("_Two things move the optimal lineup: how many people are in the pool, and "
             "how 'chalky' the field is (how hard everyone piles onto the betting "
             "favorite in each tier). Pick the cell that matches your pool; if unsure on "
             "chalkiness, use Medium._\n\n")
    if const_tiers:
        L.append(f"**Locked core - identical in every scenario:** {core_str}. "
                 "Set these and forget them.\n\n")
    L.append("Each cell shows only the tiers that change, plus your win% "
             "(fair share = 1/pool size):\n\n")

    pools = sorted(set(c["pool"] for c in grid))
    chalks = ["Low", "Medium", "High"]
    chalk_head = {"Low": "Low chalk<br>_(picks scatter)_",
                  "Medium": "Medium chalk<br>_(typical)_",
                  "High": "High chalk<br>_(all on favorites)_"}
    L.append("| Pool size | " + " | ".join(chalk_head[c] for c in chalks) + " |\n")
    L.append("|" + "---|" * (len(chalks) + 1) + "\n")
    for pool in pools:
        cells = []
        for c in chalks:
            cell = next(x for x in grid if x["pool"] == pool and x["chalk"] == c)
            if vary_tiers:
                picks = " / ".join(f"T{t} {short(pick_in_tier(cell['combo'], t))}"
                                   for t in vary_tiers)
            else:
                picks = "(core only)"
            cells.append(f"{picks}<br>**{cell['wp']*100:.1f}%**")
        L.append(f"| **{pool} people** | " + " | ".join(cells) + " |\n")

    L.append("\n**How to read it:**\n")
    L.append("- **Bigger pool -> lower win%** (more competition for one prize), but the "
             "*picks* barely move with size alone - they move with chalk.\n")
    L.append("- **Chalkier field -> fade the crowded favorites harder.** Low chalk: just "
             "take the best (EV) team. Medium: fade the Tier-2 favorite. High: fade Tier-1 "
             "and Tier-2 favorites toward lower-owned, high-ceiling teams.\n")
    L.append("- Win% is **U-shaped in chalk**: a scattered field is easy to beat, a "
             "chalk-hammering field is exploitable via leverage, and the *typical* pool in "
             "between is the hardest to win.\n")
    L.append("- In the **deep-fade cells** (big + high-chalk), the exact contrarian team "
             "is within Monte-Carlo noise - e.g. T1 Argentina <-> Brazil, T2 Netherlands "
             "<-> Belgium are interchangeable. The signal is *fade the favorite to a "
             "lower-owned elite*, not the precise name.\n")

    # correlation among picks
    L.append("\n## Portfolio correlations (per-sim points)\n")
    cmat = opt.portfolio_correlation(sim, best_combo)
    nm = [f"T{teams.tier[i]} {teams.names[i]}" for i in best_combo]
    L.append("| | " + " | ".join(nm) + " |\n")
    L.append("|" + "---|" * (len(nm) + 1) + "\n")
    for a in range(len(best_combo)):
        L.append("| " + nm[a] + " | " + " | ".join(f"{cmat[a,b]:+.2f}" for b in range(len(best_combo))) + " |\n")
    same_group = {}
    for i in best_combo:
        same_group.setdefault(teams.group[i], []).append(teams.names[i])
    collide = {g: v for g, v in same_group.items() if len(v) > 1}
    if collide:
        L.append(f"\n> Note: same-group collision(s): {collide} - these picks "
                 f"directly compete, capping combined upside.\n")
    else:
        L.append("\n> No two picks share a group (good - avoids direct head-to-head drag).\n")

    L.append("\n## Golden Boot (tiebreaker)\n")
    top = gb["table"].iloc[0]
    L.append(f"**Pick: {top['player']} ({top['team']}) - "
             f"{round(gb['exp_winning_total'])} goals.**\n\n")
    L.append(f"_{top['player']} wins the model {top['win%']:.1f}% of the time; the winning "
             f"total averages {gb['exp_winning_total']:.1f} goals._\n\n")
    L.append("| Player | Team | Win% | Exp goals | P(6+) |\n|---|---|---|---|---|\n")
    for _, r in gb["table"].head(8).iterrows():
        L.append(f"| {r['player']} | {r['team']} | {r['win%']:.1f}% | "
                 f"{r['exp_goals']:.2f} | {r['p_6plus']:.0f}% |\n")

    L.append("\n---\n_Re-run anytime with `python run.py`. Odds/ratings live in "
             "`data/teams.csv`; Golden Boot candidates in `data/players.csv`._\n")

    with open("RECOMMENDATION.md", "w", encoding="utf-8") as f:
        f.write("".join(L))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    main(quick=args.quick)
