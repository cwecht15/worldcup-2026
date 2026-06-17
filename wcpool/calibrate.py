"""Calibrate the match model to the betting market: a global `beta` plus a
per-team strength fit against BOTH the de-vigged title odds (deep-run tail)
and the de-vigged match odds for the real group fixtures (early rounds,
where most pool points live)."""

import numpy as np

from .model import devig_title_probs, match_outcome_probs_from_d
from .simulate import simulate


def calibrate_beta(teams, third_table, base=1.32, home_adv=70.0,
                   betas=None, n_sims=20_000, top_k=20, seed=1, verbose=True):
    """Grid-search beta to best match market title probabilities (log-space,
    market-weighted error over the top_k market favorites)."""
    if betas is None:
        betas = np.round(np.arange(0.45, 1.21, 0.05), 3)

    target = devig_title_probs(teams.american)
    top = np.argsort(-target)[:top_k]
    w = target[top]

    best = None
    rows = []
    for b in betas:
        res = simulate(teams, beta=b, base=base, home_adv=home_adv,
                       third_table=third_table, n_sims=n_sims, seed=seed)
        sim_tp = res.title_prob()
        # avoid log(0)
        st = np.clip(sim_tp[top], 1e-5, None)
        tt = np.clip(target[top], 1e-5, None)
        err = float(np.sum(w * (np.log(st) - np.log(tt)) ** 2))
        rows.append((float(b), err))
        if best is None or err < best[1]:
            best = (float(b), err, sim_tp)
        if verbose:
            print(f"  beta={b:.2f}  weighted_log_err={err:.4f}")

    return best[0], best[2], target, rows


def _match_step(S, teams, match_odds, beta, base, home_adv,
                d_lo=-1400.0, d_hi=1400.0, d_pts=561):
    """Per-team Elo-style residual from market match odds (analytic, no MC).

    For each priced fixture, invert the double-Poisson two-way win share
    W(d) = P(win_i)/(P(win_i)+P(win_j)) to the rating gap d* the market
    implies, and split the residual d* - d between the two teams.  Returns
    (per-team mean residual/2, per-team match count, mean |W_model - W_mkt|).
    """
    eff = S + teams.host * home_adv
    mi, mj = match_odds["i"], match_odds["j"]
    d = eff[mi] - eff[mj]

    grid = np.linspace(d_lo, d_hi, d_pts)
    gi, _, gj = match_outcome_probs_from_d(grid, beta, base)
    W_grid = gi / (gi + gj)                       # monotone increasing in d
    d_star = np.interp(match_odds["two_way"], W_grid, grid)
    resid = d_star - d

    pi, _, pj = match_outcome_probs_from_d(d, beta, base)
    w_err = float(np.mean(np.abs(pi / (pi + pj) - match_odds["two_way"])))

    num = np.zeros(teams.n)
    cnt = np.zeros(teams.n)
    np.add.at(num, mi, resid / 2.0)
    np.add.at(cnt, mi, 1.0)
    np.add.at(num, mj, -resid / 2.0)
    np.add.at(cnt, mj, 1.0)
    step = np.where(cnt > 0, num / np.maximum(cnt, 1.0), 0.0)
    return step, cnt, w_err


def calibrate_strengths(teams, third_table, beta, base=1.32, home_adv=70.0,
                        n_sims=30_000, iters=12, floor=0.004, k=120.0,
                        step_cap=80.0, seed=3, match_odds=None, k_match=0.6,
                        verbose=True):
    """Per-team strength fit against the market, two signals at once:

    - title odds (simulated title prob vs de-vigged market, priced teams only)
      pin the deep-run tail;
    - match odds (analytic two-way win share vs de-vigged DraftKings fixture
      prices, all priced teams) pin group-stage play, where most pool points
      are scored.  Teams with both prices get a weighted compromise.

    Common random numbers: every iteration re-simulates with the SAME seed, so
    the update is a deterministic fixed-point iteration instead of chasing
    fresh Monte-Carlo noise each pass.

    Each iteration re-centers the updated teams on their Elo mean, so the fit
    only reshapes spread without drifting against the untouched teams.

    Returns the fitted strength array (one value per team).
    """
    target = devig_title_probs(teams.american)
    fit_mask = target >= floor
    S = teams.elo.astype(float).copy()

    upd_mask = fit_mask.copy()
    if match_odds is not None:
        cnt0 = np.zeros(teams.n)
        np.add.at(cnt0, match_odds["i"], 1.0)
        np.add.at(cnt0, match_odds["j"], 1.0)
        upd_mask |= cnt0 > 0
    elo_mean_upd = teams.elo[upd_mask].mean()

    max_err = w_err = np.inf
    for it in range(iters):
        res = simulate(teams, beta=beta, base=base, home_adv=home_adv,
                       third_table=third_table, n_sims=n_sims, seed=seed,
                       strength=S)
        sim_tp = res.title_prob()
        logratio = np.log(np.clip(target, 1e-4, None)) - np.log(np.clip(sim_tp, 1e-4, None))
        step = np.clip(k * logratio, -step_cap, step_cap)
        S[fit_mask] += step[fit_mask]
        max_err = float(np.max(np.abs(logratio[fit_mask])))

        if match_odds is not None:
            mstep, _, w_err = _match_step(S, teams, match_odds, beta, base, home_adv)
            S += np.clip(k_match * mstep, -step_cap, step_cap)

        S[upd_mask] -= (S[upd_mask].mean() - elo_mean_upd)  # re-center
        if verbose:
            msg = f"  iter {it}: max|log(target/sim)| title = {max_err:.3f}"
            if match_odds is not None:
                msg += f"   mean|W_model - W_mkt| match = {w_err:.4f}"
            print(msg)
        if max_err < 0.08 and (match_odds is None or w_err < 0.010):
            break

    return S, target, fit_mask

