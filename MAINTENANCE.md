# Maintaining the model / porting to a new tournament

Notes for running this engine on a future World Cup (or re-running 2026). Read
the two **correctness traps** first — they are non-obvious and cost real debugging
time in 2026.

## Correctness traps (read these first)

### 1. The third-place slot table must be FIFA's *official* Annex C — not a computed matching

In the 48-team format, 8 of the 12 third-placed teams advance, and each is
assigned to a reserved Round-of-32 slot **based only on which groups they came
from** (not their ranking). `wcdata.THIRD_SLOTS` lists the *allowed* groups per
slot — but for most of the C(12,8)=495 qualifying combinations that constraint
admits **several** legal assignments, and FIFA publishes **one specific choice**
(Annex C of the tournament regulations).

Do **not** compute "any valid perfect matching" here. A greedy matcher over
`THIRD_SLOTS` disagreed with the official 2026 table on **463 of the 495
combinations**. The official table lives in `wcpool/third_place_table.py`
(`ANNEX_C`), transcribed from the Wikipedia template that reproduces Annex C.
`model.build_third_place_assignments()` reads it; the column→slot mapping is
derived from `wcdata.R32_MATCHES` so the data stays bound to the bracket.

**Why it matters even mid-tournament:** the conditional sim forces played
knockout results with an override keyed by the *team pair* (see trap 2). If the
sim re-derives the bracket from a wrong third-place table, it pairs teams who
never actually met, the override misses, and a team that really lost keeps
advancing in the sim (in 2026 this left Germany with a ~5% title chance after it
had already been knocked out).

### 2. Once the group stage is complete, the conditional sim pins the R32 bracket to reality

`simulate.simulate(..., fixed=...)` forces already-played matches by looking each
match up by its unordered team pair. That only fires if the simulator actually
pairs those two teams in that round. So once results exist, `build_web_data`
resolves the real 16 R32 matchups (`_resolve_bracket`, in `wcdata.BRACKET_ORDER`)
and passes them as `r32_entrants` to pin the sim's Round-of-32 field to the real
bracket every sim. This makes every knockout override land correctly and cascade
forward. Pre-tournament and market-calibration sims are **not** pinned (the real
bracket isn't known yet) — they fall back to the Annex C table.

Net: the Annex C table (trap 1) governs **pre-knockout / calibration** odds; the
pinning (trap 2) governs the **conditional** sim once the bracket is known. Both
are needed.

## What changes for a new tournament

Update, roughly in order:

| File | What to update |
|---|---|
| `data/teams.csv` | 48 teams: tier, group (from the draw), Elo, title odds |
| `data/players.csv` | Golden Boot candidates |
| `data/match_odds.csv` | per-fixture odds — regenerate via `python fetch_odds.py` |
| `wcpool/wcdata.py` | bracket **structure**: `GROUP_LETTERS`, `R32_MATCHES`, `THIRD_SLOTS`, `BRACKET_ORDER`, `ROUND_POINTS`, group-stage scoring |
| `wcpool/third_place_table.py` | FIFA's official third-place allocation (**re-transcribe per tournament/format** — see below) |
| `fetch_results.py` | `COMP` code for the competition on football-data.org (currently `"WC"`) |
| `.github/workflows/build-and-deploy.yml` | results-sweep cron hours (align to that tournament's kickoff window) |

If the field size or format changes (e.g. back to a 32-team bracket), the whole
bracket structure in `wcdata.py` **and** the third-place table change shape.
`scoring.MAX_SINGLE_TEAM` and the `assert`s in `run.build_sim` also encode the
48-team / 12-group shape.

## Rebuilding and verifying the third-place table (Annex C)

The table is pure structural data; regenerate it from an authoritative source and
verify, don't hand-edit.

1. **Source.** Wikipedia `Template:<YEAR> FIFA World Cup third-place table` is the
   easiest machine-readable copy of Annex C. Pull raw wikitext:
   `…/w/index.php?title=Template:<YEAR>_FIFA_World_Cup_third-place_table&action=raw`.
   The columns are the group winners that face a third-placed team (for 2026:
   `1A,1B,1D,1E,1G,1I,1K,1L`); each row's `3X` tokens are the third-place group
   assigned to each of those winners, in column order. The 8 assignment letters
   in a row also **are** that row's qualifying set.
   - Parsing gotcha (2026): the highlighted "actual qualifiers" row carries a
     `style="background-color:#BBF3BB"` — the `3B` inside that hex leaks a
     spurious token. Cut each row at its `\n|-` separator before extracting `3X`.

2. **Store** as `ANNEX_C = { "<sorted 8 group letters>": "<8-char assignment in
   COLUMN_ORDER>" }`, with `COLUMN_ORDER` = those winner-groups sorted.

3. **Verify — every check below must pass** (they all did for 2026):
   - Exactly C(n_groups, n_thirds) rows (495 for 48-team).
   - Every combination appears once; full coverage of all C(n,k) sets.
   - Every assignment is legal under `wcdata.THIRD_SLOTS`.
     `model.build_third_place_assignments()` **raises** if any assignment is
     illegal, and returns a non-empty `failures` list if coverage is incomplete
     (`run.build_sim` prints the count).
   - **Ground truth:** the row for the tournament's actual 8 qualifying groups
     reproduces the real Round-of-32 bracket. Cross-check against published
     fixtures (e.g. 2026: winner E vs 3rd-of-D = Germany vs Paraguay).

## Live results & deploy

- `fetch_results.py` pulls completed matches + upcoming fixtures from
  football-data.org into `data/results.json` (git-ignored; key in
  `data/.football_data_key` or `FOOTBALL_DATA_KEY`). It never fails the build —
  no key / no matches ⇒ the sim just stays pre-tournament.
- `build_web_data.py` conditions the sim on those results and writes
  `web/data/sim.json` for the site.
- The GitHub Action rebuilds from a **fresh** fetch and redeploys `web/` on every
  scheduled run and on push to `main` — it does **not** commit `data/results.json`
  or `web/data/sim.json`, so the committed copies are just snapshots. Don't
  diagnose "the site looks stale" from the committed `sim.json`; check the
  deployed file and the Action's fetch-step log (matches count) instead.
