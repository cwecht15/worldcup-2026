/* Live + simulated World Cup pool dashboard.
   - sim.json (this folder): Monte Carlo projections, refreshed by a scheduled build.
   - Google-Sheet CSV: the pool's live points, polled every 60s.
   Projections are joined to live standings by entry name. */

const SHEET_CSV_URL =
  "https://docs.google.com/spreadsheets/d/e/2PACX-1vTQPFnmAZG7QiOJTpcCWUSZQb" +
  "kN90EAJfJQaf8BacjBBRkNzloun10HnMdLBzFWZt-qU4JHZaVb3I80/pub" +
  "?gid=1567052873&single=true&output=csv";
const SIM_URL = "./data/sim.json";
const REFRESH = 60; // seconds, live sheet poll

const state = {
  sim: null, live: null, merged: [],
  sort: "live", tierFilter: "all", teamSort: "ev",
  open: new Set(), countdown: REFRESH,
};

/* ---------- helpers ---------- */
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x, d = 1) => (x * 100).toFixed(d) + "%";
const normName = (s) =>
  String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

function parseCSV(text) {
  // quote-aware CSV -> array of arrays
  const rows = [];
  let row = [], field = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else q = false; }
      else field += c;
    } else if (c === '"') q = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); field = "";
      if (row.some((x) => x.trim() !== "")) rows.push(row);
      row = [];
    } else field += c;
  }
  if (field !== "" || row.length) { row.push(field); if (row.some((x) => x.trim() !== "")) rows.push(row); }
  return rows;
}

function colFind(headers, ...cands) {
  const nh = headers.map(normName);
  for (const c of cands) { const i = nh.indexOf(normName(c)); if (i >= 0) return i; }
  return -1;
}

/* ---------- data loading ---------- */
async function loadSim() {
  const r = await fetch(SIM_URL + "?t=" + Date.now(), { cache: "no-store" });
  if (!r.ok) throw new Error("sim.json " + r.status);
  return r.json();
}

async function loadLive() {
  try {
    const r = await fetch(SHEET_CSV_URL + "&t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) return null;
    const rows = parseCSV(await r.text());
    if (rows.length < 2) return null;
    const h = rows[0];
    const ci = {
      name: colFind(h, "Name", "Player"),
      total: colFind(h, "Total", "TotalPts", "Total Points"),
      tp: [1, 2, 3, 4, 5, 6].map((t) => colFind(h, `T${t} Points`, `T${t}Points`, `Tier${t}Points`)),
    };
    if (ci.name < 0) return null;
    const out = [];
    for (let i = 1; i < rows.length; i++) {
      const row = rows[i];
      const name = (row[ci.name] || "").trim();
      if (!name) continue;
      const tp = ci.tp.map((c) => (c >= 0 ? parseInt((row[c] || "").replace(/[^0-9-]/g, "")) || 0 : 0));
      let total = ci.total >= 0 ? parseInt((row[ci.total] || "").replace(/[^0-9-]/g, "")) : NaN;
      if (isNaN(total)) total = tp.reduce((a, b) => a + b, 0);
      out.push({ name, total, tp });
    }
    return out;
  } catch (e) { return null; } // sheet optional; sim.json carries a snapshot
}

function merge() {
  const sim = state.sim;
  if (!sim) return;
  const liveByName = new Map();
  (state.live || []).forEach((r) => liveByName.set(normName(r.name), r));
  const seen = new Set();
  const merged = sim.entries.map((e) => {
    const lv = liveByName.get(normName(e.name));
    seen.add(normName(e.name));
    return {
      ...e,
      live_total: lv ? lv.total : e.live_total,
      live_tier_pts: lv ? lv.tp : e.live_tier_pts,
      hasSim: true,
    };
  });
  // live-only entries (added to the sheet since the last build)
  (state.live || []).forEach((r) => {
    if (!seen.has(normName(r.name)))
      merged.push({
        name: r.name, picks: [], live_total: r.total, live_tier_pts: r.tp,
        win_prob: null, proj_total: null, exp_finish: null, p_top3: null,
        boot_pick: null, unmapped_tiers: [], path: null, hasSim: false,
      });
  });
  state.merged = merged;
}

/* ---------- render ---------- */
function render() {
  if (!state.sim) return;
  renderProvenance();
  renderStatStrip();
  renderChampion();
  renderBestPicks();
  renderLeaderboard();
  renderTeams();
  renderBoot();
  renderFooter();
}

function renderProvenance() {
  const m = state.sim.meta;
  let ago = "";
  const t = Date.parse(m.generated_at);
  if (!isNaN(t)) {
    const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
    ago = mins < 60 ? `${mins}m ago` : `${Math.round(mins / 60)}h ago`;
  }
  $("#provenance").textContent =
    `${(m.n_sims / 1000).toLocaleString()}k sims · updated ${ago}`;
}

function fmtPicksFlags(e) {
  if (!e.picks || !e.picks.length)
    return `<span class="muted" style="font-size:12px">live only</span>`;
  return `<div class="picks">${e.picks
    .map((p, i) => {
      const miss = e.unmapped_tiers && e.unmapped_tiers.includes(i + 1);
      return `<span class="pk${miss ? " miss" : ""}" title="${esc(p || "—")}">${getFlag(p)}</span>`;
    })
    .join("")}</div>`;
}

function renderStatStrip() {
  const sim = state.sim, f = sim.field;
  const leader = state.merged.reduce(
    (a, b) => (b.live_total > (a ? a.live_total : -1) ? b : a), null);
  const avg = state.merged.length
    ? (state.merged.reduce((s, e) => s + (e.live_total || 0), 0) / state.merged.length).toFixed(1)
    : "0";
  const topBoot = sim.golden_boot.race[0];
  const fav = state.merged
    .filter((e) => e.win_prob != null)
    .reduce((a, b) => (b.win_prob > (a ? a.win_prob : -1) ? b : a), null);
  const cards = [
    { k: "Entries", v: f.n_entries, s: `fair share ${f.fair_share_pct}%` },
    { k: "Live leader", v: leader ? leader.name.split(" ")[0] : "—",
      s: leader ? `${leader.live_total} pts` : "" },
    { k: "Avg score", v: avg, s: "live points", cls: "cyan" },
    { k: "Win % favorite", v: fav ? fav.name.split(" ")[0] : "—",
      s: fav ? pct(fav.win_prob) + " to win pool" : "", cls: "" },
    { k: "Boot leader", v: topBoot ? topBoot.player.split(" ").slice(-1)[0] : "—",
      s: topBoot ? pct(topBoot.win / 100) + " · " + topBoot.exp_goals + " xG" : "", cls: "amber" },
  ];
  $("#statstrip").innerHTML = cards
    .map((c) => `<div class="stat ${c.cls || ""}"><div class="k">${esc(c.k)}</div>
      <div class="v">${esc(c.v)}</div><div class="s">${esc(c.s)}</div></div>`)
    .join("");
}

function renderChampion() {
  const c = state.sim.champion;
  const top = c.title_odds[0];
  const max = top.title || 1;
  const body = `
    <div class="champ-hero">
      <span class="flag">${getFlag(c.projected)}</span>
      <div><span class="lbl">projected champion</span>
        <div class="who">${esc(c.projected)}</div></div>
      <div class="pct"><b>${top.title.toFixed(1)}%</b><span>title odds</span></div>
    </div>
    <div class="odds-list">
      ${c.title_odds.slice(0, 8).map((o) => `
        <div class="oddsrow">
          <span class="fl">${getFlag(o.name)}</span>
          <span class="nm">${esc(o.name)}</span>
          <span class="bar"><i style="width:${Math.max(3, (o.title / max) * 100)}%"></i></span>
          <span class="val">${o.title.toFixed(1)}%</span>
        </div>`).join("")}
    </div>
    <div class="finalists">
      <span class="muted" style="font-size:10px;align-self:center;font-family:var(--mono);letter-spacing:1px">REACH FINAL</span>
      ${c.finalists.slice(0, 5).map((x) =>
        `<span class="chip">${getFlag(x.name)} ${esc(x.name)} <b>${x.reach_final}%</b></span>`).join("")}
    </div>`;
  $("#champion-body").innerHTML = body;
}

function renderBestPicks() {
  const bp = state.sim.best_picks;
  const recSet = new Set(bp.recommended.lineup);
  const tiers = bp.by_tier.map((t) => `
    <div class="bp-tier"><div class="t">T${t.tier}</div>
      <div class="bp-opts">${t.options.map((o) => `
        <div class="bp-opt${recSet.has(o.name) ? " best" : ""}">
          <span class="fl">${getFlag(o.name)}</span>
          <span class="nm">${esc(o.name)}</span>
          <span class="meta">EV ${o.ev} · ${o.title}%</span>
        </div>`).join("")}</div>
    </div>`).join("");
  const lineup = (l) => l.teams.map((t) => `<span>${getFlag(t)} ${esc(t)}</span>`).join("");
  const r = bp.recommended, em = bp.ev_max;
  $("#bestpicks-body").innerHTML = tiers + `
    <div class="bp-lineups">
      <div class="bp-line"><div class="h"><span class="lbl">Win%-max lineup</span>
        <span class="w">${r.win_pct}%</span></div>
        <div class="teams">${r.lineup.map((t) => `<span>${getFlag(t)} ${esc(t)}</span>`).join("")}</div></div>
      <div class="bp-line"><div class="h"><span class="lbl">EV-max lineup</span>
        <span class="w">${em.win_pct}%</span></div>
        <div class="teams">${em.lineup.map((t) => `<span>${getFlag(t)} ${esc(t)}</span>`).join("")}</div></div>
    </div>`;
}

function renderLeaderboard() {
  const rows = [...state.merged];
  if (state.sort === "win")
    rows.sort((a, b) => (b.win_prob ?? -1) - (a.win_prob ?? -1) || b.live_total - a.live_total);
  else
    rows.sort((a, b) => b.live_total - a.live_total || (b.win_prob ?? -1) - (a.win_prob ?? -1));
  const maxWin = Math.max(0.01, ...rows.map((r) => r.win_prob || 0));

  const html = rows.map((e, i) => {
    const rank = i + 1;
    const g = rank <= 3 ? ` g${rank}` : "";
    const win = e.win_prob != null
      ? `<div class="winbar"><span class="track"><i style="width:${(e.win_prob / maxWin) * 100}%"></i></span>
         <span class="wv">${pct(e.win_prob)}</span></div>`
      : `<div class="winbar"><span class="wv muted">—</span></div>`;
    const warn = e.unmapped_tiers && e.unmapped_tiers.length
      ? `<span class="warn" title="Tier(s) ${e.unmapped_tiers.join(",")} not matched to a team">!${e.unmapped_tiers.length}</span>` : "";
    const boot = e.boot_pick
      ? `<span class="fl">${getFlag((state.sim.golden_boot.race.find((p) => p.player === e.boot_pick) || {}).team)}</span>${esc(e.boot_pick)}`
      : "—";
    const main = `<tr class="row${state.open.has(e.name) ? " open" : ""}" data-name="${esc(e.name)}">
      <td class="c-rank"><span class="rankbadge${g}">${rank}</span></td>
      <td class="c-name"><div class="ename"><span class="caret">▶</span>${esc(e.name)} ${warn}</div></td>
      <td class="c-picks">${fmtPicksFlags(e)}</td>
      <td class="num">${e.live_total}</td>
      <td class="num proj">${e.proj_total != null ? e.proj_total : "—"}</td>
      <td class="wincell">${win}</td>
      <td class="bootcell">${boot}</td>
    </tr>`;
    const detail = state.open.has(e.name) ? renderDetail(e) : "";
    return main + detail;
  }).join("");
  $("#lb-body").innerHTML = html || `<tr><td colspan="7" class="skeleton">No entries.</td></tr>`;
  $("#lb-disclaimer").innerHTML =
    `Sorted by <b>${state.sort === "win" ? "modeled win %" : "live points"}</b>. ` +
    `Click any entry for its path to victory. Win % assumes the full tournament is replayed from current odds.`;
}

function renderDetail(e) {
  if (!e.path)
    return `<tr class="detail"><td colspan="7"><div class="detail-inner">
      <div class="path-summary"><span class="lead">Path to victory</span>
      No projection yet — this entry was added to the sheet after the last model run.</div></div></td></tr>`;
  const p = e.path;
  const maxC = Math.max(0.1, ...(p.carries || []).map((c) => c.cond_pts));
  const carries = (p.carries || []).map((c) => {
    const linch = p.linchpin && c.team === p.linchpin.team;
    return `<div class="carry${linch ? " linch" : ""}">
      <span class="fl">${getFlag(c.team)}</span><span class="nm">${esc(c.team)}${linch ? " ⚡" : ""}</span>
      <span class="cbar"><i style="width:${(c.cond_pts / maxC) * 100}%"></i></span>
      <span class="cv">${c.cond_pts}</span></div>`;
  }).join("");
  // each .v is pre-escaped safe HTML (may include a flag <img>)
  const stats = [
    e.win_prob != null ? { k: "Win pool", v: pct(e.win_prob), acc: 1 } : null,
    e.p_top3 != null ? { k: "Top 3", v: pct(e.p_top3) } : null,
    e.exp_finish != null ? { k: "Avg finish", v: String(e.exp_finish) } : null,
    p.champion_when_win ? { k: "Champ when you win", v: `${getFlag(p.champion_when_win.team)} ${esc(p.champion_when_win.team)} ${p.champion_when_win.pct}%` } : null,
    p.typical_winning_score ? { k: "Typical winning score", v: String(p.typical_winning_score) } : null,
    p.chief_rival ? { k: "Chief rival", v: `${esc(p.chief_rival.name)} (${p.chief_rival.pct}%)` } : null,
  ].filter(Boolean);
  return `<tr class="detail"><td colspan="7"><div class="detail-inner">
    <div>
      <div class="path-summary"><span class="lead">Path to victory</span>${esc(p.summary)}</div>
      <div class="path-stats">${stats.map((s) =>
        `<div class="pstat"><div class="k">${esc(s.k)}</div><div class="v${s.acc ? " acc" : ""}">${s.v}</div></div>`).join("")}</div>
    </div>
    <div class="carries"><div class="ch">Points carried (when you win)</div>${carries}</div>
  </div></td></tr>`;
}

function renderTeams() {
  let teams = state.sim.teams.slice();
  if (state.tierFilter !== "all")
    teams = teams.filter((t) => t.tier === +state.tierFilter);
  const lev = (t) => t.implied_own - t.actual_own;
  const sorters = {
    ev: (a, b) => b.ev - a.ev,
    title: (a, b) => b.title - a.title,
    leverage: (a, b) => lev(b) - lev(a),
  };
  teams.sort(sorters[state.teamSort]);
  const maxOwn = Math.max(0.01, ...state.sim.teams.map((t) => Math.max(t.actual_own, t.implied_own)));
  $("#teams-body").innerHTML = teams.map((t) => {
    const l = lev(t);
    const levCls = t.actual_own > t.implied_own ? "over" : "under";
    const levTxt = t.actual_own > t.implied_own
      ? `over-owned +${((t.actual_own - t.implied_own) * 100).toFixed(0)}pp`
      : `value −${((t.implied_own - t.actual_own) * 100).toFixed(0)}pp`;
    return `<div class="tcard">
      <div class="top"><span class="fl">${getFlag(t.name)}</span><span class="nm">${esc(t.name)}</span>
        <span class="tr">T${t.tier} · ${esc(t.group)}</span></div>
      <div class="ev">EV <b>${t.ev}</b> · title ${t.title}% · KO ${t.reachKO}%</div>
      <div class="ownrow actual"><span class="ol">pool</span>
        <span class="ot"><i style="width:${(t.actual_own / maxOwn) * 100}%"></i></span>
        <span class="ov">${(t.actual_own * 100).toFixed(0)}%</span></div>
      <div class="ownrow market"><span class="ol">market</span>
        <span class="ot"><i style="width:${(t.implied_own / maxOwn) * 100}%"></i></span>
        <span class="ov">${(t.implied_own * 100).toFixed(0)}%</span></div>
      <div class="lev ${levCls}">${levTxt}</div>
    </div>`;
  }).join("") || `<div class="skeleton">No teams in this tier.</div>`;
  $("#teams-legend").innerHTML =
    `<b>EV</b> = expected pool points · <b>pool</b> vs <b>market</b> = how often this team is picked in your pool vs the bookmaker-implied rate. ` +
    `Green <b>value</b> = the field is under-picking a strong team (leverage); red = crowded.`;
}

function renderBoot() {
  const gb = state.sim.golden_boot;
  $("#boot-total").textContent =
    `winning total ≈ ${gb.exp_winning_total} goals (median ${gb.median_winning_total})`;
  const max = Math.max(1, ...gb.race.map((r) => r.win));
  $("#boot-body").innerHTML = gb.race.map((r, i) => `
    <div class="boot-row">
      <span class="rk">${i + 1}</span>
      <div class="who"><span class="fl">${getFlag(r.team)}</span>
        <div style="min-width:0"><div class="pn">${esc(r.player)}</div>
          <div class="tn">${esc(r.team)} · <span class="own-tag">${(r.actual_own * 100).toFixed(0)}% owned</span></div></div></div>
      <span class="wbar"><i style="width:${(r.win / max) * 100}%"></i></span>
      <div class="bval">
        <span class="winp">${r.win}%</span>
        <div class="own-tag">${r.exp_goals} xG · ${r.p_6plus}% 6+</div></div>
    </div>`).join("");
}

function renderFooter() {
  const sc = state.sim.meta.scoring;
  const items = [
    ["Group draw", sc.group_draw], ["Group win", sc.group_win], ["R32", sc.R32],
    ["R16", sc.R16], ["QF", sc.QF], ["SF", sc.SF], ["Champion", sc.champion],
  ];
  $("#scoring-legend").innerHTML = items
    .map(([k, v]) => `<span class="sc">${k} <b>${v}</b></span>`).join("");
}

/* ---------- interactions ---------- */
function wire() {
  $("#lb-body").addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr.row");
    if (!tr) return;
    const name = tr.dataset.name;
    if (state.open.has(name)) state.open.delete(name); else state.open.add(name);
    renderLeaderboard();
  });
  document.querySelectorAll(".sort-toggle button").forEach((b) =>
    b.addEventListener("click", () => {
      state.sort = b.dataset.sort;
      document.querySelectorAll(".sort-toggle button").forEach((x) => x.classList.toggle("active", x === b));
      renderLeaderboard();
    }));
  $("#team-sort").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    state.teamSort = b.dataset.tsort;
    $("#team-sort").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    renderTeams();
  });
  $("#tier-filter").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    state.tierFilter = b.dataset.tier;
    $("#tier-filter").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    renderTeams();
  });
}

function buildTierFilter() {
  const opts = [["all", "All"], ["1", "T1"], ["2", "T2"], ["3", "T3"], ["4", "T4"], ["5", "T5"], ["6", "T6"]];
  $("#tier-filter").innerHTML = opts
    .map(([v, l]) => `<button data-tier="${v}" class="${v === "all" ? "active" : ""}">${l}</button>`).join("");
}

/* ---------- boot / loop ---------- */
function tick() {
  state.countdown--;
  if (state.countdown <= 0) {
    state.countdown = REFRESH;
    loadLive().then((live) => { if (live) { state.live = live; merge(); render(); } });
  }
  const el = $("#countdown"); if (el) el.textContent = state.countdown;
}

async function init() {
  buildTierFilter();
  wire();
  try {
    state.sim = await loadSim();
  } catch (e) {
    $("#statstrip").innerHTML = `<div class="err">Could not load projections (sim.json). ${esc(e.message)}</div>`;
    return;
  }
  merge();
  render();
  // deep link: #e=<entry name> opens that entry's path to victory
  if (location.hash.startsWith("#e=")) {
    const nm = decodeURIComponent(location.hash.slice(3));
    const hit = state.merged.find((m) => normName(m.name) === normName(nm));
    if (hit) { state.open.add(hit.name); renderLeaderboard(); $("#leaderboard").scrollIntoView(); }
  }
  // live sheet in the background (non-blocking; sim.json already has a snapshot)
  loadLive().then((live) => { if (live) { state.live = live; merge(); render(); } });
  setInterval(tick, 1000);
  // periodically refetch projections too (in case a build landed)
  setInterval(() => loadSim().then((s) => { state.sim = s; merge(); render(); }).catch(() => {}), 10 * 60 * 1000);
}

document.addEventListener("DOMContentLoaded", init);
