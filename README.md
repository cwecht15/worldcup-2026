# 2026 World Cup Pool Model

Monte Carlo model to attack a tiered World Cup pool: pick one team from each of
6 DraftKings-odds tiers, scored by how far each team advances. The model
simulates the **real** 2026 bracket, calibrates to the betting market, and finds
the 6-team portfolio with the highest probability of **winning the pool**.

## Pool scoring
Per team, summed across your 6 picks: group tie **1**, group win **3**,
R32 win **5**, R16 win **7**, QF win **10**, SF win **15**, Champion **20**.
Tiebreaker: Golden Boot winner + goal count.

## Run it
```bash
python run.py            # full run (300k sims) -> writes RECOMMENDATION.md
python run.py --quick    # fast smoke test
```
Open **RECOMMENDATION.md** for the picks, the per-tier EV table, the top
portfolios, field-size sensitivity, and the Golden Boot answer.

## How it works
1. **Data** (`data/teams.csv`): all 48 teams with tier, real group (Dec-2025
   draw), Elo rating, and DraftKings title odds. Golden Boot candidates in
   `data/players.csv`.
2. **Calibration** (`wcpool/calibrate.py`): a global `beta` sets the
   market-implied match concentration, then a per-team strength fit targets
   TWO market signals at once: de-vigged title odds (deep-run tail, priced
   teams) and de-vigged DraftKings match odds for every group fixture
   (`data/match_odds.csv` - early rounds, all 48 teams). The fit uses common
   random numbers across iterations so it converges instead of chasing
   Monte-Carlo noise.
3. **Simulation** (`wcpool/simulate.py`): vectorized double-Poisson goal model
   over the actual group fixtures + the exact knockout bracket (incl. the FIFA
   third-place slot table), with host bumps and extra-time/penalty handling.
   Outputs per-sim pool points and goals for all 48 teams.
4. **Optimizer** (`wcpool/optimize.py`): models a field of M chalk-weighted
   opponents, builds the opponent-score distribution per sim by FFT convolution,
   and ranks portfolios by P(finish 1st). Reports win%-max vs EV-max.
5. **Golden Boot** (`wcpool/golden_boot.py`): player goals ~ Binomial(team goals,
   share); team goals scale with how deep the team runs.

## Refreshing odds (optional)
To pull live DraftKings title + match odds from The Odds API (uses 2 of the
500 free monthly requests):
```bash
echo YOUR_KEY > data/.odds_api_key   # git-ignored
python fetch_odds.py
python run.py
```

## Tuning
- Field size / chalkiness: `optimize(..., M=20, gamma=1.5)` in `run.py`.
- More teams in the Golden Boot race: add rows to `data/players.csv`.
- As the tournament unfolds, edit results into the model (future work) or just
  re-run with updated odds.
