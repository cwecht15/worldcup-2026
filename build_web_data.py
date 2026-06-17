"""Build web/data/sim.json for the live + simulated World Cup pool site.

Reuses the existing Monte Carlo engine (wcpool) wholesale:
  - run.build_sim()  -> calibrated simulation of the real 2026 bracket
  - the live Google-Sheet CSV  -> each pool entry's 6 tier picks + Golden Boot
For every entry we compute, from the SAME simulated tournaments, its probability
of winning the pool (Golden-Boot tiebreak included), expected final points, a
"path to victory" breakdown, plus the best projected picks per tier and a champion
projection.  The compact JSON is read by the static frontend in web/.

Usage:
    python build_web_data.py                 # full run (N=200k), no odds refresh
    python build_web_data.py --refresh-odds  # pull fresh DraftKings odds first
    python build_web_data.py --quick         # fast smoke test
"""

import argparse
import csv
import datetime as dt
import difflib
import io
import json
import os
import sys
import unicodedata
import urllib.request

import numpy as np

from run import build_sim
from wcpool import model, scoring, optimize, golden_boot, tiebreaker, wcdata
import fetch_odds


HERE = os.path.dirname(os.path.abspath(__file__))

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTQPFnmAZG7QiOJTpcCWUSZQb"
    "kN90EAJfJQaf8BacjBBRkNzloun10HnMdLBzFWZt-qU4JHZaVb3I80/pub"
    "?gid=1567052873&single=true&output=csv"
)

# sheet header (normalized) -> our canonical team name, where they differ.
# normalize = lowercase, strip, drop non-alphanumerics.
TEAM_ALIASES = {
    "unitedstates": "USA", "usa": "USA", "us": "USA",
    "koreareplublic": "South Korea", "korearepublic": "South Korea",
    "southkorea": "South Korea", "skorea": "South Korea",
    "czechrepublic": "Czechia", "czechia": "Czechia",
    "cotedivoire": "Ivory Coast", "ivorycoast": "Ivory Coast",
    "turkiye": "Turkey", "turkey": "Turkey",
    "bosniaandherzegovina": "Bosnia", "bosniaherzegovina": "Bosnia",
    "bosnia": "Bosnia",
    "congodr": "DR Congo", "drcongo": "DR Congo", "democraticrepublicofcongo": "DR Congo",
    "curacao": "Curacao",
    "uzebekistan": "Uzbekistan", "uzbekistan": "Uzbekistan",
}

# sheet Golden-Boot spelling (folded) -> our players.csv name, where they differ.
PLAYER_ALIASES = {
    "viniciousjunior": "Vinicius Junior", "vinicius": "Vinicius Junior",
    "vinijr": "Vinicius Junior",
}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------
def _fold(s):
    """lowercase, strip accents, drop non-alphanumerics -> a join key."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return "".join(ch for ch in s.lower() if ch.isalnum())


def normalize_team(name, teams):
    """Sheet team string -> teams index, or None if blank/unknown."""
    if name is None:
        return None
    raw = str(name).strip()
    if not raw or raw.lower() in ("tbd", "n/a", "na", "-"):
        return None
    if raw in teams.idx:                       # exact, fast path
        return teams.idx[raw]
    key = _fold(raw)
    # exact fold against canonical names
    for i, nm in enumerate(teams.names):
        if _fold(nm) == key:
            return i
    # alias table
    canon = TEAM_ALIASES.get(key)
    if canon and canon in teams.idx:
        return teams.idx[canon]
    # fuzzy fallback (catches typos like "Uzebekistan" -> "Uzbekistan")
    folded = {_fold(nm): i for i, nm in enumerate(teams.names)}
    close = difflib.get_close_matches(key, list(folded), n=1, cutoff=0.85)
    if close:
        return folded[close[0]]
    return None


def normalize_player(name, players_df):
    """Sheet Golden-Boot string -> column index into players_df, or None."""
    if name is None:
        return None
    raw = str(name).strip()
    if not raw or raw.lower() in ("tbd", "n/a", "na", "-"):
        return None
    folded = {_fold(p): j for j, p in enumerate(players_df["player"].tolist())}
    key = _fold(raw)
    if key in folded:
        return folded[key]
    canon = PLAYER_ALIASES.get(key)
    if canon is not None:
        cf = _fold(canon)
        if cf in folded:
            return folded[cf]
    # last-name fallback (e.g. "Mbappe" -> "Kylian Mbappe")
    for fk, j in folded.items():
        if fk.endswith(key) or key in fk:
            return j
    # fuzzy fallback for typos (e.g. "Vinicious" -> "Vinicius Junior")
    close = difflib.get_close_matches(key, list(folded), n=1, cutoff=0.82)
    if close:
        return folded[close[0]]
    return None


def _to_int(s):
    """Parse the integer part of a sheet cell ('3', '3 pts', '') -> int or None."""
    if s is None:
        return None
    digits = "".join(ch for ch in str(s) if ch.isdigit() or ch == "-")
    if digits in ("", "-"):
        return None
    try:
        return int(digits)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Live sheet
# ---------------------------------------------------------------------------
def _colfind(norm_headers, *candidates):
    """Return the index of the first header whose folded form matches a candidate."""
    for cand in candidates:
        c = _fold(cand)
        for i, h in enumerate(norm_headers):
            if h == c:
                return i
    return None


def fetch_sheet_rows(url):
    """GET the published CSV and return (headers, list-of-row-lists)."""
    req = urllib.request.Request(url, headers={"User-Agent": "wc-pool-builder"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(raw))
    rows = [row for row in reader]
    if not rows:
        sys.exit("[sheet] empty CSV response")
    return rows[0], rows[1:]


def parse_entries(headers, rows, teams, players_df):
    """Map each non-blank sheet row to a structured entry dict."""
    nh = [_fold(h) for h in headers]
    ci_name = _colfind(nh, "Name", "Player")
    ci_tier = [_colfind(nh, f"Tier {t}", f"Tier{t}", f"T{t} Team") for t in range(1, 7)]
    ci_tpts = [_colfind(nh, f"T{t} Points", f"T{t}Points", f"Tier{t}Points",
                        f"T{t} Pts") for t in range(1, 7)]
    ci_total = _colfind(nh, "Total", "TotalPts", "TotalPoints", "Total Points")
    ci_boot = _colfind(nh, "Golden Boot Projection", "GoldenBoot", "GB Pick",
                       "Golden Boot")
    ci_bootg = _colfind(nh, "Golden Boot Total Goals", "GoldenBootTotal",
                        "GB Goals", "Golden Boot Goals")
    if ci_name is None or any(c is None for c in ci_tier):
        sys.exit(f"[sheet] could not locate required columns in headers: {headers}")

    def cell(row, i):
        return row[i] if (i is not None and i < len(row)) else ""

    entries = []
    unmapped_log = []
    for row in rows:
        name = cell(row, ci_name).strip()
        if not name:
            continue
        picks, pick_names, unmapped = [], [], []
        for t in range(6):
            raw = cell(row, ci_tier[t]).strip()
            idx = normalize_team(raw, teams)
            picks.append(idx)
            pick_names.append(teams.names[idx] if idx is not None else (raw or None))
            if idx is None and raw:
                unmapped.append(t + 1)
                unmapped_log.append(("team", name, raw))
        boot_raw = cell(row, ci_boot).strip()
        boot_idx = normalize_player(boot_raw, players_df)
        if boot_idx is None and boot_raw:
            unmapped_log.append(("player", name, boot_raw))
        live_tier_pts = [_to_int(cell(row, ci_tpts[t])) or 0 for t in range(6)]
        live_total = _to_int(cell(row, ci_total))
        if live_total is None:
            live_total = sum(live_tier_pts)
        entries.append({
            "name": name,
            "picks": picks,
            "pick_names": pick_names,
            "unmapped_tiers": unmapped,
            "boot_idx": boot_idx,
            "boot_pick_name": (players_df["player"].iloc[boot_idx]
                               if boot_idx is not None else (boot_raw or None)),
            "boot_goal": _to_int(cell(row, ci_bootg)),
            "live_total": live_total,
            "live_tier_pts": live_tier_pts,
        })
    return entries, unmapped_log


# ---------------------------------------------------------------------------
# Per-entry pool resolution (the core math)
# ---------------------------------------------------------------------------
def build_entry_scores(entries, sim):
    """scores[E, N] float32 = each entry's per-sim total pool points."""
    E, N = len(entries), sim.n
    scores = np.zeros((E, N), dtype=np.float32)
    for e, ent in enumerate(entries):
        valid = [i for i in ent["picks"] if i is not None]
        if valid:
            scores[e] = scoring.portfolio_scores(sim.total_pts, valid)
    return scores


def _strictly_better(key, chunk=20000):
    """sb[e, n] = # entries with key strictly greater than entry e in sim n.

    Chunked over sims to bound the [E, E, chunk] comparison tensor.
    """
    E, N = key.shape
    sb = np.zeros((E, N), dtype=np.int32)
    for s in range(0, N, chunk):
        k = key[:, s:s + chunk]                                  # [E, C]
        greater = k[:, None, :] > k[None, :, :]                  # [f, e, C]
        sb[:, s:s + chunk] = greater.sum(axis=0)
    return sb


def resolve_pool(entries, scores, sim, gb_goals, gb_players):
    """Per-entry win%, P(top3), expected finish, and a leader/runner-up map.

    Resolves the pool's real tiebreak (points -> named the Golden-Boot winner ->
    closest to that player's goal total) by collapsing it into one sortable key.
    """
    E, N = scores.shape
    bw, bwg = tiebreaker.actual_boot_winner(gb_goals, gb_players)   # [N], [N]

    boot_idx = np.array([e["boot_idx"] if e["boot_idx"] is not None else -1
                         for e in entries])
    boot_goal = np.array([e["boot_goal"] if e["boot_goal"] is not None else np.inf
                          for e in entries], dtype=np.float64)

    pts = np.rint(scores).astype(np.float64)                       # [E, N]
    named = ((boot_idx[:, None] >= 0) & (boot_idx[:, None] == bw[None, :]))
    gerr = np.abs(boot_goal[:, None] - bwg[None, :])               # inf where no pick
    key = pts * 1e6 + named.astype(np.float64) * 1e3 - np.clip(gerr, 0.0, 999.0)

    kmax = key.max(axis=0)
    winners = key == kmax[None, :]                                 # [E, N]
    nwin = winners.sum(axis=0)
    share = winners / nwin[None, :]                                # ties split evenly
    leader = np.argmax(key, axis=0)                                # [N]

    sb = _strictly_better(key)
    win_prob = share.mean(axis=1)
    p_top3 = (sb < 3).mean(axis=1)
    exp_finish = (1 + sb).mean(axis=1)
    exp_points = scores.mean(axis=1)

    return {
        "win_prob": win_prob, "p_top3": p_top3, "exp_finish": exp_finish,
        "exp_points": exp_points, "share": share, "sb": sb, "leader": leader,
    }


def build_paths(entries, scores, pool, sim, teams):
    """Per-entry 'path to victory': linchpin team, modal champion when winning,
    typical winning score, and chief rival."""
    share, sb, leader = pool["share"], pool["sb"], pool["leader"]
    base_pts = sim.total_pts.mean(axis=0)                          # [48] unconditional
    paths = []
    for e, ent in enumerate(entries):
        win_mask = share[e] > 0
        target = win_mask if win_mask.any() else (sb[e] == sb[e].min())
        # carries: conditional points from each mapped pick when the entry wins
        carries = []
        for idx, nm in zip(ent["picks"], ent["pick_names"]):
            if idx is None:
                continue
            cond = float(sim.total_pts[target, idx].mean())
            carries.append({"team": nm, "cond_pts": round(cond, 2),
                            "base_pts": round(float(base_pts[idx]), 2)})
        carries.sort(key=lambda c: -c["cond_pts"])
        linchpin = max(carries, key=lambda c: c["cond_pts"] - c["base_pts"]) \
            if carries else None
        # champion in the entry's winning sims
        champs = sim.champion[target]
        champ_when = None
        if len(champs):
            vals, counts = np.unique(champs, return_counts=True)
            top = vals[np.argmax(counts)]
            champ_when = {"team": teams.names[int(top)],
                          "pct": round(100.0 * counts.max() / len(champs), 1)}
        twin = float(np.median(scores[e][target])) if target.any() else 0.0
        # chief rival: who leads when this entry is the runner-up
        rival = None
        runner = sb[e] == 1
        if runner.any():
            rl = leader[runner]
            vals, counts = np.unique(rl, return_counts=True)
            ri = int(vals[np.argmax(counts)])
            rival = {"name": entries[ri]["name"],
                     "pct": round(100.0 * counts.max() / runner.sum(), 1)}
        paths.append({
            "linchpin": linchpin, "carries": carries[:6],
            "champion_when_win": champ_when,
            "typical_winning_score": round(twin, 1),
            "chief_rival": rival,
            "summary": _path_summary(ent, linchpin, champ_when, rival,
                                     bool(win_mask.any())),
        })
    return paths


def _path_summary(ent, linchpin, champ, rival, can_win):
    bits = []
    if linchpin:
        bits.append(f"Your wins run through {linchpin['team']}")
    if champ:
        bits.append(f"you usually need {champ['team']} to lift the trophy "
                    f"({champ['pct']}% of your winning sims)")
    if rival:
        bits.append(f"{rival['name']} is most often the one to beat")
    if not can_win:
        return ("Long shot: only wins in chaos scenarios. "
                + ("; ".join(bits) + "." if bits else ""))
    return ("; ".join(bits) + ".") if bits else "Balanced lineup with no single linchpin."


# ---------------------------------------------------------------------------
# Aggregate sim outputs
# ---------------------------------------------------------------------------
def build_team_table(sim, teams, entries, gamma):
    ev_df = optimize.ev_table(sim, teams)
    pop = optimize.popularity(teams, gamma)                        # market-implied own
    n = max(len(entries), 1)
    actual = np.zeros(teams.n)
    for ent in entries:
        for idx in ent["picks"]:
            if idx is not None:
                actual[idx] += 1
    out = []
    for _, r in ev_df.iterrows():
        i = teams.idx[r["team"]]
        out.append({
            "name": r["team"], "tier": int(r["tier"]), "group": r["group"],
            "ev": round(float(r["ev"]), 2), "title": round(float(r["title%"]), 2),
            "reachKO": round(float(r["reachKO%"]), 1),
            "winR32": round(float(r["winR32%"]), 1),
            "winR16": round(float(r["winR16%"]), 1),
            "winQF": round(float(r["winQF%"]), 1),
            "winSF": round(float(r["winSF%"]), 1),
            "implied_own": round(float(pop[i]), 4),
            "actual_own": round(actual[i] / n, 4),
        })
    return out, ev_df, actual


def build_best_picks(sim, teams, ev_df, M, gamma, quick):
    by_tier = []
    for t in range(1, 7):
        sub = ev_df[ev_df["tier"] == t].head(3)
        by_tier.append({"tier": t, "options": [
            {"name": r["team"], "ev": round(float(r["ev"]), 2),
             "title": round(float(r["title%"]), 2),
             "reachKO": round(float(r["reachKO%"]), 1)}
            for _, r in sub.iterrows()]})
    res = optimize.optimize(sim, teams, M=M, gamma=gamma,
                            n_sub=(4000 if quick else 60000), topk=4)
    combo, wp, ev = res["ranked"][0]
    ev_idx, ev_wp, ev_ev = res["ev_max"]

    def lineup(idxs):
        return [teams.names[i] for i in sorted(idxs, key=lambda i: teams.tier[i])]

    return {
        "by_tier": by_tier,
        "recommended": {"lineup": lineup(combo), "win_pct": round(wp * 100, 2),
                        "ev": round(ev, 1)},
        "ev_max": {"lineup": lineup(ev_idx), "win_pct": round(ev_wp * 100, 2),
                   "ev": round(ev_ev, 1)},
    }


def build_champion(sim, teams):
    tp = sim.title_prob()
    reach_final = sim.round_prob("SF")                            # win SF == reach final
    order = np.argsort(-tp)
    title_odds = [{"name": teams.names[i], "title": round(float(tp[i]) * 100, 2)}
                  for i in order[:12]]
    forder = np.argsort(-reach_final)
    finalists = [{"name": teams.names[i],
                  "reach_final": round(float(reach_final[i]) * 100, 1)}
                 for i in forder[:6]]
    return {"projected": teams.names[int(order[0])], "title_odds": title_odds,
            "finalists": finalists}


def build_golden_boot(sim, teams, players_df, entries):
    gb = golden_boot.simulate_golden_boot(sim, teams, players_df)
    n = max(len(entries), 1)
    own = {}
    for ent in entries:
        if ent["boot_pick_name"]:
            own[ent["boot_pick_name"]] = own.get(ent["boot_pick_name"], 0) + 1
    race = [{"player": r["player"], "team": r["team"],
             "win": round(float(r["win%"]), 1),
             "exp_goals": round(float(r["exp_goals"]), 2),
             "p_6plus": round(float(r["p_6plus"]), 1),
             "actual_own": round(own.get(r["player"], 0) / n, 4)}
            for _, r in gb["table"].head(12).iterrows()]
    return {"exp_winning_total": round(gb["exp_winning_total"], 1),
            "median_winning_total": int(gb["median_winning_total"]), "race": race}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build_payload(entries, scores, pool, paths, teams_tbl, best, champ, gb,
                  actual_own, teams, beta, n_full, now_iso):
    E = len(entries)
    # most-picked teams (by name)
    counts = {}
    for ent in entries:
        for nm in ent["pick_names"]:
            if nm:
                counts[nm] = counts.get(nm, 0) + 1
    most_picked = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    leader_live = max(entries, key=lambda e: e["live_total"]) if entries else None

    out_entries = []
    for e, ent in enumerate(entries):
        out_entries.append({
            "name": ent["name"],
            "picks": [nm for nm in ent["pick_names"]],
            "pick_tiers": [1, 2, 3, 4, 5, 6],
            "unmapped_tiers": ent["unmapped_tiers"],
            "boot_pick": ent["boot_pick_name"],
            "boot_goal": ent["boot_goal"],
            "live_total": ent["live_total"],
            "live_tier_pts": ent["live_tier_pts"],
            "win_prob": round(float(pool["win_prob"][e]), 4),
            "p_top3": round(float(pool["p_top3"][e]), 4),
            "exp_finish": round(float(pool["exp_finish"][e]), 2),
            "exp_points": round(float(pool["exp_points"][e]), 1),
            "proj_total": round(float(pool["exp_points"][e]), 1),
            "path": paths[e],
        })

    return {
        "meta": {
            "generated_at": now_iso,
            "n_sims": int(n_full),
            "beta": round(float(beta), 3),
            "engine_version": "wcpool",
            "sheet_rows": E,
            "scoring": {"group_draw": wcdata.GROUP_DRAW_PTS,
                        "group_win": wcdata.GROUP_WIN_PTS,
                        "R32": 5, "R16": 7, "QF": 10, "SF": 15, "champion": 20},
            "max_single_team": int(scoring.MAX_SINGLE_TEAM),
        },
        "entries": out_entries,
        "teams": teams_tbl,
        "best_picks": best,
        "champion": champ,
        "golden_boot": gb,
        "field": {
            "n_entries": E,
            "most_picked": [{"team": t, "count": c,
                             "tier": int(teams.tier[teams.idx[t]])
                             if t in teams.idx else None}
                            for t, c in most_picked],
            "leader_live": ({"name": leader_live["name"],
                             "total": leader_live["live_total"]}
                            if leader_live else None),
            "fair_share_pct": round(100.0 / E, 2) if E else 0.0,
        },
    }


def write_json(out_path, payload):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--refresh-odds", action="store_true")
    ap.add_argument("--n", type=int, default=None, help="override n_sims")
    ap.add_argument("--out", default=os.path.join(HERE, "web", "data", "sim.json"))
    ap.add_argument("--sheet-csv", default=SHEET_CSV_URL)
    ap.add_argument("--M", type=int, default=20)
    ap.add_argument("--gamma", type=float, default=1.5)
    ap.add_argument("--odds-reserve", type=int, default=20,
                    help="keep at least this many Odds API requests in reserve; "
                         "skip the refresh if the monthly budget would drop below it")
    args = ap.parse_args()

    if args.refresh_odds:
        # free-tier guard: a refresh costs 2 requests; check the (free) budget first
        NEED = 2
        remaining = fetch_odds.requests_remaining()
        if remaining is not None and remaining < NEED + args.odds_reserve:
            print(f"[odds] only {remaining} requests left this month "
                  f"(need {NEED} + {args.odds_reserve} reserve) -> SKIPPING refresh; "
                  f"building from existing odds in data/teams.csv")
        else:
            if remaining is not None:
                print(f"[odds] {remaining} requests remaining; refreshing (uses {NEED})...")
            else:
                print("[odds] refreshing from The Odds API...")
            fetch_odds.update_csv(fetch_odds.fetch())
            fetch_odds.fetch_match_odds()

    n_full = args.n if args.n is not None else (8000 if args.quick else 200_000)
    print(f"[build] running engine (n_sims={n_full:,})...")
    teams, players, _third, beta, _target, _strength, sim = build_sim(
        quick=args.quick, n_full=n_full, verbose=True)

    # canonical Golden-Boot goals/players (filtered + reset) used everywhere below
    gb_goals, gb_players = golden_boot.player_goals(sim, teams, players)

    print("[sheet] fetching live entries...")
    headers, rows = fetch_sheet_rows(args.sheet_csv)
    entries, unmapped_log = parse_entries(headers, rows, teams, gb_players)
    print(f"[sheet] parsed {len(entries)} entries")
    if unmapped_log:
        print(f"[sheet] {len(unmapped_log)} unmapped picks (scored as 0):")
        for kind, who, raw in unmapped_log[:20]:
            print(f"         {who}: {kind} '{raw}'")

    scores = build_entry_scores(entries, sim)
    pool = resolve_pool(entries, scores, sim, gb_goals, gb_players)
    paths = build_paths(entries, scores, pool, sim, teams)

    teams_tbl, ev_df, actual_own = build_team_table(sim, teams, entries, args.gamma)
    best = build_best_picks(sim, teams, ev_df, args.M, args.gamma, args.quick)
    champ = build_champion(sim, teams)
    gb = build_golden_boot(sim, teams, players, entries)

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    payload = build_payload(entries, scores, pool, paths, teams_tbl, best, champ,
                            gb, actual_own, teams, beta, n_full, now_iso)

    # ---- self-checks ----
    wsum = float(pool["win_prob"].sum())
    assert abs(wsum - 1.0) < 1e-6, f"win_prob sums to {wsum}, expected 1.0"
    proj = champ["projected"]
    assert proj == teams.names[int(np.argmax(sim.title_prob()))]
    print(f"[check] sum(win_prob)={wsum:.6f}  projected champion={proj}")
    top = sorted(payload["entries"], key=lambda e: -e["win_prob"])[:5]
    print("[check] top 5 by pool win%:")
    for e in top:
        print(f"         {e['name']:<24} win={e['win_prob']*100:5.2f}%  "
              f"proj={e['proj_total']:.1f}  live={e['live_total']}")

    write_json(args.out, payload)
    size = os.path.getsize(args.out) / 1024
    print(f"[done] wrote {args.out} ({size:.0f} KB)")


if __name__ == "__main__":
    main()
