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
from wcpool import model, scoring, optimize, golden_boot, tiebreaker, wcdata, results
import fetch_odds


HERE = os.path.dirname(os.path.abspath(__file__))

# Refresh cadence (UTC hours) — mirrors .github/workflows/build-and-deploy.yml.
# Embedded in sim.json so the frontend can show the next update time in local zone.
RESULTS_SWEEP_UTC = [19, 21, 23, 1, 3, 5, 7]   # results fetched on every sweep
ODDS_REFRESH_UTC = [19, 5]                      # odds refreshed twice a day

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTQPFnmAZG7QiOJTpcCWUSZQb"
    "kN90EAJfJQaf8BacjBBRkNzloun10HnMdLBzFWZt-qU4JHZaVb3I80/pub"
    "?gid=1567052873&single=true&output=csv"
)
# previously-deployed sim.json: lets us compute win-odds momentum (climbers/fallers)
PREV_SIM_URL = "https://cwecht15.github.io/worldcup-2026/data/sim.json"

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
        # chief rival: who leads when this entry is the runner-up, and WHY
        rival = None
        runner = sb[e] == 1
        if runner.any():
            rl = leader[runner]
            vals, counts = np.unique(rl, return_counts=True)
            ri = int(vals[np.argmax(counts)])
            ri_mask = runner & (leader == ri)
            ePicks, rPicks = ent["picks"], entries[ri]["picks"]
            eset = {i for i in ePicks if i is not None}
            rset = {i for i in rPicks if i is not None}
            shared = [teams.names[i] for i in eset & rset]
            rival_edge = [teams.names[i] for i in rset - eset]
            your_edge = [teams.names[i] for i in eset - rset]
            # decisive tier: where the rival most out-scores you when they beat you
            decisive, best_gap = None, 0.0
            for t in range(6):
                pe, pr = ePicks[t], rPicks[t]
                if pe is None or pr is None or pe == pr:
                    continue
                gap = (float((sim.total_pts[ri_mask, pr] - sim.total_pts[ri_mask, pe]).mean())
                       if ri_mask.any() else 0.0)
                if gap > best_gap:
                    best_gap = gap
                    decisive = {"tier": t + 1, "rival_team": teams.names[pr],
                                "your_team": teams.names[pe], "gap": round(gap, 1)}
            rival = {"name": entries[ri]["name"],
                     "pct": round(100.0 * counts.max() / runner.sum(), 1),
                     "shared": shared, "rival_edge": rival_edge,
                     "your_edge": your_edge, "decisive": decisive}
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


def fetch_prev(url):
    """Fetch the previously-deployed sim.json to diff win odds / ranks against.

    Returns {"map": {folded_name: {win_prob, rank, proj}}, "as_of": iso} or None.
    Rank is by win probability (1 = best)."""
    try:
        u = url + ("&" if "?" in url else "?") + "cb=1"
        req = urllib.request.Request(u, headers={"User-Agent": "wc-pool-builder"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except Exception as e:
        print(f"[movers] no previous sim.json to diff ({e}); momentum starts fresh")
        return None
    ents = d.get("entries", [])
    order = sorted(ents, key=lambda e: -(e.get("win_prob") or 0))
    rank = {results._fold(e["name"]): i + 1 for i, e in enumerate(order)}
    pmap = {results._fold(e["name"]): {"win_prob": e.get("win_prob"),
                                       "rank": rank[results._fold(e["name"])],
                                       "proj": e.get("proj_total")} for e in ents}
    meta = d.get("meta", {}) or {}
    return {"map": pmap, "as_of": meta.get("generated_at"),
            "odds_at": meta.get("odds_at")}


def compute_finals(entries, sim):
    """Per-entry guaranteed (floor) and best-case (ceiling) final totals, plus a
    mathematical 'blocked from winning' flag: an entry is blocked when its ceiling
    can't reach the current leader's locked-in floor.  Uses the conditional sim's
    per-team min/max, so it is automatically all-False before any results exist."""
    tp_min = sim.total_pts.min(axis=0)
    tp_max = sim.total_pts.max(axis=0)
    floors, ceils = [], []
    for ent in entries:
        picks = [i for i in ent["picks"] if i is not None]
        floors.append(float(tp_min[picks].sum()) if picks else 0.0)
        ceils.append(float(tp_max[picks].sum()) if picks else 0.0)
    leader_floor = max(floors) if floors else 0.0
    blocked = [c < leader_floor for c in ceils]
    return {"floors": floors, "ceils": ceils, "blocked": blocked,
            "leader_floor": leader_floor}


# ---------------------------------------------------------------------------
# Aggregate sim outputs
# ---------------------------------------------------------------------------
def build_team_table(sim, teams, entries, gamma, results_present=False):
    ev_df = optimize.ev_table(sim, teams)
    pop = optimize.popularity(teams, gamma)                        # market-implied own
    n = max(len(entries), 1)
    tp_min = sim.total_pts.min(axis=0)     # points a team has locked in (accrued)
    tp_max = sim.total_pts.max(axis=0)     # best-case final for that team
    actual = np.zeros(teams.n)
    for ent in entries:
        for idx in ent["picks"]:
            if idx is not None:
                actual[idx] += 1
    out = []
    for _, r in ev_df.iterrows():
        i = teams.idx[r["team"]]
        # eliminated = no remaining upside (only meaningful once results exist)
        eliminated = bool(results_present and tp_max[i] <= tp_min[i])
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
            "accrued": round(float(tp_min[i]), 1),
            "out": eliminated,
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


def build_golden_boot(sim, teams, players_df, entries,
                      real_team_goals=None, real_player_goals=None):
    gb = golden_boot.simulate_golden_boot(
        sim, teams, players_df,
        real_team_goals=real_team_goals, real_player_goals=real_player_goals)
    n = max(len(entries), 1)
    own = {}
    for ent in entries:
        if ent["boot_pick_name"]:
            own[ent["boot_pick_name"]] = own.get(ent["boot_pick_name"], 0) + 1
    real_by_fold = {results._fold(k): (v.get("goals", 0) or 0)
                    for k, v in (real_player_goals or {}).items()}
    race = []
    for _, r in gb["table"].head(12).iterrows():
        total = round(float(r["exp_goals"]), 2)             # current + expected remaining
        cur = int(real_by_fold.get(results._fold(r["player"]), 0))
        race.append({"player": r["player"], "team": r["team"],
                     "win": round(float(r["win%"]), 1),
                     "exp_goals": total, "current": cur,
                     "remaining": round(max(0.0, total - cur), 2),
                     "p_6plus": round(float(r["p_6plus"]), 1),
                     "actual_own": round(own.get(r["player"], 0) / n, 4)})
    return {"exp_winning_total": round(gb["exp_winning_total"], 1),
            "median_winning_total": int(gb["median_winning_total"]),
            "conditional": bool(real_player_goals), "race": race}


def build_groups(teams, sim, res):
    """Per-group standings from real results + each team's chance to advance.

    Standings (P/W/D/L/GF/GA/GD/Pts) come from completed GROUP matches; `reach`
    is the modeled chance to reach the knockouts; `status` is in (advanced) /
    out (eliminated) / live, derived from the conditional sim's locked points."""
    reach = sim.reach_prob() * 100.0
    tp_min = sim.total_pts.min(axis=0)
    tp_max = sim.total_pts.max(axis=0)
    rec = {i: {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0} for i in range(teams.n)}
    scores = {g: [] for g in teams.groups}
    if res:
        for m in res.matches:
            if m["round"] != "GROUP":
                continue
            i, j, gi, gj = m["i"], m["j"], m["gi"], m["gj"]
            for a, gfa, gaa in ((i, gi, gj), (j, gj, gi)):
                r = rec[a]
                r["p"] += 1; r["gf"] += gfa; r["ga"] += gaa
                r["w"] += gfa > gaa; r["d"] += gfa == gaa; r["l"] += gfa < gaa
            scores[teams.group[i]].append(
                {"home": teams.names[i], "away": teams.names[j], "hg": gi, "ag": gj})

    groups = []
    for g in sorted(teams.groups):
        rows = []
        for i in teams.groups[g]:
            r = rec[i]
            pts = r["w"] * 3 + r["d"]
            if res and tp_max[i] <= tp_min[i]:
                status = "out"
            elif res and reach[i] >= 99.9:
                status = "in"
            else:
                status = "live"
            rows.append({"name": teams.names[i], "p": r["p"], "w": r["w"], "d": r["d"],
                         "l": r["l"], "gf": r["gf"], "ga": r["ga"],
                         "gd": r["gf"] - r["ga"], "pts": pts,
                         "reach": round(float(reach[i])), "status": status})
        rows.sort(key=lambda x: (-x["pts"], -x["gd"], -x["gf"], -x["reach"], x["name"]))
        groups.append({"letter": g, "teams": rows, "matches": scores[g]})
    return groups


def _group_letter(g):
    """'GROUP_A' / 'Group A' -> 'A'; anything else -> None."""
    if not g:
        return None
    s = str(g).upper().replace("GROUP_", "").replace("GROUP ", "").strip()
    return s[:1] if s else None


def build_recent_results(teams, res, limit=18):
    """Completed matches, newest-first, with dates/round/group for the Recent
    Results card.  The frontend derives who each game helped/hurt from ownership."""
    if not res:
        return []
    out = []
    for m in res.matches:
        i, j = m["i"], m["j"]
        out.append({
            "date": m.get("date"),
            "round": m["round"],
            "group": _group_letter(m.get("group")),
            "home": teams.names[i], "away": teams.names[j],
            "hg": int(m["gi"]), "ag": int(m["gj"]),
        })
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out[:limit]


def load_upcoming(teams, limit=30):
    """Upcoming fixtures (kickoff time + matchup) from data/results.json's
    'upcoming' list, with team names resolved to our canonical spellings."""
    path = os.path.join(HERE, "data", "results.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for u in data.get("upcoming", []):
        rk = results.STAGE_MAP.get(str(u.get("stage", "GROUP_STAGE")).upper(), "GROUP")
        if rk is None:                                  # 3rd-place playoff: no pool points
            continue
        i = results.resolve_team(u.get("home"), teams)
        j = results.resolve_team(u.get("away"), teams)
        home = teams.names[i] if i is not None else u.get("home")
        away = teams.names[j] if j is not None else u.get("away")
        if not home or not away:
            continue
        out.append({"date": u.get("utcDate"), "round": rk,
                    "group": _group_letter(u.get("group")),
                    "home": home, "away": away,
                    "i": i, "j": j})       # resolved indices (None if unmatched)
    out.sort(key=lambda x: x["date"] or "")
    return out[:limit]


def remaining_group_fixtures(teams, res):
    """Fallback when the feed carries no 'upcoming' list: the group round-robin
    games not yet played, shaped like load_upcoming() entries (undated).  Mirrors
    the frontend's upcomingList() fallback so "what to root for" still has data."""
    played = set()
    if res:
        for m in res.matches:
            if m["round"] != "GROUP":
                continue
            i, j = m["i"], m["j"]
            played.add((i, j) if i < j else (j, i))
    out = []
    for g in sorted(teams.groups):
        members = list(teams.groups[g])
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                i, j = int(members[a]), int(members[b])
                if ((i, j) if i < j else (j, i)) in played:
                    continue
                out.append({"date": None, "round": "GROUP", "group": g,
                            "home": teams.names[i], "away": teams.names[j],
                            "i": i, "j": j})
    return out


def build_track(upcoming, teams, max_days=2, cap=14):
    """From load_upcoming() output, pick the next `max_days` distinct match-days
    of fully-resolved games (capped at `cap`) for the "what to root for" analysis.

    Returns (track, games_meta) — parallel lists.  `track` feeds simulate(track=);
    `games_meta` carries the public matchup info aligned to sim.tracked_outcomes."""
    nteam = teams.n
    track, games_meta, day_keys = [], [], []
    for u in upcoming:
        i, j = u.get("i"), u.get("j")
        if i is None or j is None:          # both teams must be known to analyze
            continue
        day = (u["date"] or "")[:10]        # UTC calendar day
        if day:
            if day not in day_keys:
                if len(day_keys) >= max_days:
                    continue                # past the first max_days match-days
                day_keys.append(day)
        lo, hi = (i, j) if i < j else (j, i)
        track.append({"code": lo * nteam + hi, "home_idx": i, "away_idx": j})
        games_meta.append({"date": u["date"], "round": u["round"],
                           "group": u["group"], "home": u["home"], "away": u["away"]})
        if len(track) >= cap:
            break
    return track, games_meta


def build_rooting(entries, pool, sim, games_meta):
    """Per-entry 'what to root for' for each tracked upcoming game.

    Conditions the existing Monte Carlo sample on each game's outcome: the win %
    given an outcome O is just the mean win-share over the sims where O happened.
    No re-simulation — sim.tracked_outcomes already recorded each game's per-sim
    result (0 home / 1 draw / 2 away / -1 the two teams didn't meet)."""
    oc = sim.tracked_outcomes
    if oc is None or oc.shape[1] == 0 or not games_meta:
        return None
    share = pool["share"]                                   # [E, N]
    E = len(entries)
    games = []
    rows = [[] for _ in range(E)]                           # by_entry rows, per game
    for g, meta in enumerate(games_meta):
        col = oc[:, g]
        is_ko = meta["round"] != "GROUP"
        n_def = int((col != -1).sum())                      # sims where they met
        gmeta = {"date": meta["date"], "round": meta["round"],
                 "group": meta["group"], "home": meta["home"], "away": meta["away"],
                 "p_home": None, "p_draw": None, "p_away": None}
        keys = {0: "p_home", 1: "p_draw", 2: "p_away"}
        cond = {}                                           # outcome -> [E] or None
        for o in (0, 1, 2):
            if o == 1 and is_ko:                            # no draws in knockouts
                cond[o] = None
                continue
            mask = col == o
            n_o = int(mask.sum())
            gmeta[keys[o]] = round(n_o / n_def, 4) if n_def else 0.0
            cond[o] = share[:, mask].mean(axis=1) if n_o else None
        games.append(gmeta)
        for e in range(E):
            rows[e].append([round(float(cond[o][e]), 4) if cond[o] is not None else None
                            for o in (0, 1, 2)])
    by_entry = {results._fold(ent["name"]): rows[e] for e, ent in enumerate(entries)}
    return {"games": games, "by_entry": by_entry}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build_payload(entries, scores, pool, paths, teams_tbl, best, champ, gb,
                  actual_own, teams, beta, n_full, now_iso, finals=None, res=None,
                  groups=None, prev=None, recent=None, upcoming=None, odds_at=None,
                  rooting=None):
    E = len(entries)
    # current rank by win probability (1 = best), for momentum vs the last build
    win_order = sorted(range(E), key=lambda i: -float(pool["win_prob"][i]))
    cur_rank = {i: r + 1 for r, i in enumerate(win_order)}
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
        prevrec = prev["map"].get(results._fold(ent["name"])) if prev else None
        win_now = float(pool["win_prob"][e])
        wp = prevrec["win_prob"] if prevrec else None
        delta_win = round(win_now - wp, 4) if (prevrec and wp is not None) else None
        rank_delta = (prevrec["rank"] - cur_rank[e]) if (prevrec and prevrec.get("rank")) else None
        out_entries.append({
            "name": ent["name"],
            "delta_win": delta_win,        # change in win prob since the last build (+ = up)
            "rank_delta": rank_delta,      # win-rank positions gained (+ = climbed)
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
            "blocked": bool(finals["blocked"][e]) if finals else False,
            "max_final": round(finals["ceils"][e], 1) if finals else None,
            "min_final": round(finals["floors"][e], 1) if finals else None,
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
            "results": ({"conditional": True, "as_of": res.as_of,
                         "matches_played": res.n_matches} if res
                        else {"conditional": False}),
            "prev_at": (prev["as_of"] if prev else None),
            "odds_at": odds_at,
            "schedule": {"results_utc_hours": RESULTS_SWEEP_UTC,
                         "odds_utc_hours": ODDS_REFRESH_UTC},
        },
        "entries": out_entries,
        "teams": teams_tbl,
        "best_picks": best,
        "champion": champ,
        "golden_boot": gb,
        "groups": groups or [],
        "recent_results": recent or [],
        "schedule_upcoming": upcoming or [],
        "rooting": rooting,
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
            "n_blocked": int(sum(finals["blocked"])) if finals else 0,
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
    ap.add_argument("--no-results", action="store_true",
                    help="ignore data/results.json (force the pre-tournament sim)")
    ap.add_argument("--prev-url", default=PREV_SIM_URL,
                    help="previous sim.json URL to diff win odds against (momentum)")
    ap.add_argument("--no-prev", action="store_true",
                    help="skip the momentum diff against the previous build")
    args = ap.parse_args()

    odds_refreshed = False
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
            odds_refreshed = True

    # live results (if present) condition the simulation on matches already played
    teams0 = model.load_teams()
    res = None if args.no_results else results.load_results(teams0)
    fixed = res.fixed() if res else None
    if res:
        print(f"[results] conditioning on {res.n_matches} completed matches "
              f"(as of {res.as_of})")
        if res.unmatched:
            print(f"[results] unmatched team names (ignored): {sorted(set(res.unmatched))}")
    else:
        print("[results] no data/results.json -> full-tournament (pre-results) sim")

    # upcoming fixtures + the games to analyze for "what to root for" (next two
    # match-days).  Built before the sim so it can record their per-sim outcomes.
    upcoming = load_upcoming(teams0)
    track, rooting_games = build_track(upcoming, teams0)
    if not track:                       # feed had no upcoming list -> derive fixtures
        track, rooting_games = build_track(
            remaining_group_fixtures(teams0, res), teams0)
    if track:
        print(f"[rooting] tracking {len(track)} upcoming games for win% conditioning")

    n_full = args.n if args.n is not None else (8000 if args.quick else 200_000)
    print(f"[build] running engine (n_sims={n_full:,})...")
    teams, players, _third, beta, _target, _strength, sim = build_sim(
        quick=args.quick, n_full=n_full, verbose=True, fixed=fixed, track=track)

    # canonical Golden-Boot goals/players (filtered + reset), goals-aware if live
    rtg = res.real_team_goals if res else None
    rpg = res.real_player_goals if res else None
    gb_goals, gb_players = golden_boot.player_goals(
        sim, teams, players, real_team_goals=rtg, real_player_goals=rpg)

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
    finals = compute_finals(entries, sim)
    rooting = build_rooting(entries, pool, sim, rooting_games)

    teams_tbl, ev_df, actual_own = build_team_table(
        sim, teams, entries, args.gamma, results_present=bool(res))
    best = build_best_picks(sim, teams, ev_df, args.M, args.gamma, args.quick)
    champ = build_champion(sim, teams)
    gb = build_golden_boot(sim, teams, players, entries,
                           real_team_goals=rtg, real_player_goals=rpg)
    groups = build_groups(teams, sim, res)
    recent = build_recent_results(teams, res)
    # public schedule list drops the internal resolved indices used for tracking
    upcoming_public = [{k: v for k, v in u.items() if k not in ("i", "j")}
                       for u in upcoming]
    prev = None if args.no_prev else fetch_prev(args.prev_url)

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    # last odds refresh: now if we just pulled fresh odds, else carry forward the
    # timestamp from the previously-deployed sim.json (None until a refresh records one)
    odds_at = now_iso if odds_refreshed else (prev.get("odds_at") if prev else None)
    payload = build_payload(entries, scores, pool, paths, teams_tbl, best, champ,
                            gb, actual_own, teams, beta, n_full, now_iso,
                            finals=finals, res=res, groups=groups, prev=prev,
                            recent=recent, upcoming=upcoming_public, odds_at=odds_at,
                            rooting=rooting)

    # ---- self-checks ----
    wsum = float(pool["win_prob"].sum())
    assert abs(wsum - 1.0) < 1e-6, f"win_prob sums to {wsum}, expected 1.0"
    proj = champ["projected"]
    assert proj == teams.names[int(np.argmax(sim.title_prob()))]
    print(f"[check] sum(win_prob)={wsum:.6f}  projected champion={proj}")
    if res:
        nb = sum(finals["blocked"])
        elim = sum(1 for t in teams_tbl if t["out"])
        print(f"[check] conditional sim: {elim} teams eliminated, "
              f"{nb} entries blocked from winning")
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
