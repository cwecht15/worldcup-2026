"""Portfolio EV table and the leverage-aware win-probability optimizer.

Win-probability model
---------------------
The pool resolves on ONE tournament, so "winning" = top score among all entries
in the realized outcome.  Opponents are modeled as M independent entries, each
picking one team per tier from a chalk-weighted popularity distribution.

For a simulated tournament s, an opponent's score is the sum of 6 independent
per-tier point draws.  We build the opponent-score PMF per sim by FFT convolution
of the six tiers' (integer-valued) point distributions.  For our candidate
portfolio with score q(s),  P(beat one opponent) = CDF(q-1) + 0.5*PMF(q), and
  P(win pool) = mean over sims of  P(beat one)^M.
This respects all cross-team correlations because every entry is scored on the
same simulated tournament.
"""

import itertools

import numpy as np
import pandas as pd

from .model import american_to_prob

L = 512  # max integer score support for the opponent-score convolution


def ev_table(sim, teams):
    ev = sim.expected_points()
    df = pd.DataFrame({
        "team": teams.names,
        "tier": teams.tier,
        "group": teams.group,
        "ev": ev,
        "title%": sim.title_prob() * 100,
        "reachKO%": sim.reach_prob() * 100,
        "winR32%": sim.round_prob("R32") * 100,
        "winR16%": sim.round_prob("R16") * 100,
        "winQF%": sim.round_prob("QF") * 100,
        "winSF%": sim.round_prob("SF") * 100,
    })
    return df.sort_values(["tier", "ev"], ascending=[True, False]).reset_index(drop=True)


def popularity(teams, gamma):
    """Field pick-popularity weight per team, normalized within each tier.

    weight_i  ∝  (market implied prob_i)^gamma   within its tier.
    gamma=0 -> uniform picking; larger gamma -> field piles onto tier favorites.
    """
    p = american_to_prob(teams.american)
    w = np.zeros(teams.n)
    for t in range(1, 7):
        members = teams.tier_members(t)
        base = p[members] ** gamma
        w[members] = base / base.sum()
    return w


def opponent_pmf(total_pts_sub, teams, pop):
    """FFT-convolve the 6 tier point-distributions -> opponent score PMF/CDF.

    total_pts_sub: [N2, 48] integer points (a subsample of sims).
    Returns (pmf, cdf) each [N2, L].
    """
    N2 = total_pts_sub.shape[0]
    fft_prod = np.ones((N2, L // 2 + 1), dtype=np.complex128)
    for t in range(1, 7):
        members = teams.tier_members(t)
        tier_pmf = np.zeros((N2, L))
        rows = np.arange(N2)
        for m in members:
            vals = np.clip(total_pts_sub[:, m].astype(int), 0, L - 1)
            np.add.at(tier_pmf, (rows, vals), pop[m])
        fft_prod *= np.fft.rfft(tier_pmf, axis=1)
    pmf = np.fft.irfft(fft_prod, n=L, axis=1)
    pmf = np.clip(pmf, 0, None)
    pmf /= pmf.sum(axis=1, keepdims=True)
    cdf = np.cumsum(pmf, axis=1)
    return pmf, cdf


def win_probability(our_scores, pmf, cdf, M):
    """P(this portfolio is the unique top score vs M iid opponents)."""
    q = np.clip(np.rint(our_scores).astype(int), 0, L - 1)
    rows = np.arange(len(q))
    cdf_below = np.where(q > 0, cdf[rows, q - 1], 0.0)
    pmf_at = pmf[rows, q]
    beat_one = cdf_below + 0.5 * pmf_at
    return float(np.mean(beat_one ** M))


def _win_prob_batch(pts_sub, combos, pmf, cdf, M, batch=500):
    """Vectorized P(win) for many portfolios at once. combos: [C, 6] team idxs."""
    N2 = pts_sub.shape[0]
    C = combos.shape[0]
    rows = np.broadcast_to(np.arange(N2), (min(batch, C), N2))
    wps = np.empty(C)
    for s in range(0, C, batch):
        cb = combos[s:s + batch]                      # [b, 6]
        b = cb.shape[0]
        our = np.zeros((N2, b))
        for j in range(cb.shape[1]):
            our += pts_sub[:, cb[:, j]]               # [N2, b]
        q = np.clip(np.rint(our.T), 0, L - 1).astype(int)   # [b, N2]
        r = rows[:b]
        cdf_below = np.where(q > 0, cdf[r, np.clip(q - 1, 0, L - 1)], 0.0)
        beat_one = cdf_below + 0.5 * pmf[r, q]
        wps[s:s + b] = (beat_one ** M).mean(axis=1)
    return wps


def _candidates(ev_df, teams, topk):
    """Per-tier candidate team indices: top `topk` by EV (favorite included)."""
    cands = {}
    for t in range(1, 7):
        sub = ev_df[ev_df["tier"] == t].head(topk)
        cands[t] = [teams.idx[name] for name in sub["team"]]
    return cands


def optimize(sim, teams, M=20, gamma=1.5, n_sub=30_000, topk=4, seed=7):
    """Return ranked candidate portfolios by win probability, plus the EV-max."""
    ev_df = ev_table(sim, teams)

    rng = np.random.default_rng(seed)
    N = sim.total_pts.shape[0]
    idx_sub = rng.choice(N, size=min(n_sub, N), replace=False)
    pts_sub = sim.total_pts[idx_sub].astype(int)

    pop = popularity(teams, gamma)
    pmf, cdf = opponent_pmf(pts_sub, teams, pop)

    cands = _candidates(ev_df, teams, topk)
    tiers = list(range(1, 7))
    combos = np.array(list(itertools.product(*[cands[t] for t in tiers])), dtype=int)
    C, N2 = combos.shape[0], pts_sub.shape[0]

    ev_per_team = sim.expected_points()
    wps = _win_prob_batch(pts_sub, combos, pmf, cdf, M)
    ev_sums = ev_per_team[combos].sum(axis=1)
    results = [(tuple(int(x) for x in combos[c]), float(wps[c]), float(ev_sums[c]))
               for c in range(C)]
    results.sort(key=lambda r: -r[1])

    # EV-max portfolio (argmax EV within each tier)
    ev_max = tuple(ev_df[ev_df["tier"] == t].iloc[0]["team"] for t in tiers)
    ev_max_idx = tuple(teams.idx[n] for n in ev_max)
    our_evmax = pts_sub[:, list(ev_max_idx)].sum(axis=1)
    ev_max_wp = win_probability(our_evmax, pmf, cdf, M)

    return {
        "ev_df": ev_df,
        "ranked": results,
        "ev_max": (ev_max_idx, ev_max_wp,
                   float(ev_per_team[list(ev_max_idx)].sum())),
        "popularity": pop,
        "idx_sub": idx_sub,
    }


def portfolio_correlation(sim, picks):
    """Pearson correlation matrix of the per-sim points of the picked teams."""
    X = sim.total_pts[:, list(picks)]
    return np.corrcoef(X.T)
