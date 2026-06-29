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

// Refresh cadence (UTC hours), mirrors .github/workflows/build-and-deploy.yml.
// Overridden by sim.meta.schedule when the builder emits it. Results sweep ~every
// 2h across the live window; odds refresh only twice a day.
const RESULTS_SWEEP_UTC = [19, 21, 23, 1, 3, 5, 7];
const ODDS_REFRESH_UTC = [19, 5];
const ROUND_LABEL = { GROUP: "Group", R32: "Round of 32", R16: "Round of 16",
  QF: "Quarterfinal", SF: "Semifinal", FINAL: "Final" };
// knockout rounds, the pool points for winning each, and the per-team win-odds
// field on the teams table (P(team wins that round), conditioned on results so far)
const KO_ROUNDS = ["R32", "R16", "QF", "SF", "FINAL"];
const KO_PTS = { R32: 5, R16: 7, QF: 10, SF: 15, FINAL: 20 };
const KO_COL = { R32: "R32", R16: "R16", QF: "QF", SF: "SF", FINAL: "Champ" };
const KO_PROB_KEY = { R32: "winR32", R16: "winR16", QF: "winQF", SF: "winSF", FINAL: "title" };
const MEET_LABEL = ["the Round of 32", "the Round of 16", "the Quarterfinals",
  "the Semifinals", "the Final"];

const MY_ENTRY_KEY = "wc_my_entry"; // localStorage: the visitor's starred entry

const state = {
  sim: null, live: null, merged: [],
  sortKey: "live", sortDir: "desc",
  tierFilter: "all", teamSort: "ev",
  open: new Set(), countdown: REFRESH,
  myEntry: null, // display name of the entry the visitor starred as "mine"
  h2hA: null, h2hB: null, // head-to-head compare selections (display names)
  bracketTeam: null, // team whose path is highlighted in the knockout bracket
};

// how each sortable column reads a value off an entry
const SORT_VAL = {
  name: (e) => (e.name || "").toLowerCase(),
  live: (e) => e.live_total ?? -1,
  proj: (e) => e.proj_total ?? -1,
  win: (e) => e.win_prob ?? -1,
  boot: (e) => (e.boot_pick || "~").toLowerCase(), // blanks sort last
};
// default direction when you first click a column
const SORT_DEFAULT_DIR = { name: "asc", boot: "asc", live: "desc", proj: "desc", win: "desc" };

/* ---------- helpers ---------- */
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x, d = 1) => (x * 100).toFixed(d) + "%";
const normName = (s) =>
  String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

/* ---------- time helpers (for the update-cadence + schedule cards) ---------- */
// next occurrence (after `from`) of any of these UTC hours, as a Date
function nextUtcHour(hours, from) {
  const sorted = [...hours].sort((a, b) => a - b);
  for (let d = 0; d < 2; d++)
    for (const h of sorted) {
      const c = new Date(Date.UTC(from.getUTCFullYear(), from.getUTCMonth(),
        from.getUTCDate() + d, h, 0, 0));
      if (c.getTime() > from.getTime()) return c;
    }
  return null;
}
// most recent occurrence (at or before `from`) of any of these UTC hours
function prevUtcHour(hours, from) {
  const sorted = [...hours].sort((a, b) => b - a);
  for (let d = 0; d < 2; d++)
    for (const h of sorted) {
      const c = new Date(Date.UTC(from.getUTCFullYear(), from.getUTCMonth(),
        from.getUTCDate() - d, h, 0, 0));
      if (c.getTime() <= from.getTime()) return c;
    }
  return null;
}
function relParts(ms) {
  ms = Math.max(0, ms);
  const m = Math.round(ms / 60000);
  if (m < 1) return "<1m";
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  if (h < 48) return h + "h";
  return Math.round(h / 24) + "d";
}
const fmtClock = (d) =>
  d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const fmtDay = (d) =>
  d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
const fmtTime = (d) =>
  d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
// "Today" / "Tomorrow" / weekday for a fixture date, in the visitor's local zone
function dayLabel(date) {
  if (!date) return "Upcoming";
  const d = new Date(Date.parse(date));
  if (isNaN(d)) return "Upcoming";
  const ymd = (x) => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
  const now = new Date();
  const tmrw = new Date(now); tmrw.setDate(now.getDate() + 1);
  if (ymd(d) === ymd(now)) return "Today";
  if (ymd(d) === ymd(tmrw)) return "Tomorrow";
  return fmtDay(d);
}

/* ---------- match odds (model + market) for upcoming fixtures ---------- */
// directed pair "home>away" -> {model:[h,d,a]|null, market:[h,d,a]|null}; both
// orientations stored so a fixture resolves regardless of which side is "home".
function buildOddsIndex() {
  const idx = new Map();
  const trip = (h, d, a) => (h == null ? null : [h, d, a]);
  const swap = (t) => (t ? [t[2], t[1], t[0]] : null);
  const add = (home, away, model, market) => {
    if (!model && !market) return;
    const k = normName(home) + ">" + normName(away);
    if (!idx.has(k)) idx.set(k, { model, market });
    const rk = normName(away) + ">" + normName(home);
    if (!idx.has(rk)) idx.set(rk, { model: swap(model), market: swap(market) });
  };
  const sim = state.sim || {};
  ((sim.rooting && sim.rooting.games) || []).forEach((g) =>
    add(g.home, g.away, trip(g.p_home, g.p_draw, g.p_away), trip(g.m_home, g.m_draw, g.m_away)));
  (sim.schedule_upcoming || []).forEach((s) =>
    add(s.home, s.away, trip(s.p_home, s.p_draw, s.p_away), trip(s.m_home, s.m_draw, s.m_away)));
  return idx;
}
// labeled win/draw/win, e.g. "Mexico 53% · Draw 26% · Czechia 20%" (no Draw for
// knockouts); null if the fixture has no data for this source
function fmtOdds(home, away, t) {
  if (!t || t[0] == null) return null;
  const p = (x) => Math.round(x * 100) + "%";
  const seg = (label, v) => `<span class="ov"><span class="on">${label}</span> ${v}</span>`;
  const parts = [seg(esc(home), p(t[0]))];
  if (t[1] != null) parts.push(seg("Draw", p(t[1])));
  parts.push(seg(esc(away), p(t[2])));
  return parts.join('<span class="osep">·</span>');
}
// market + model win/draw/win readout for a fixture (empty string if unknown)
function oddsWidget(home, away) {
  const o = state.odds && state.odds.get(normName(home) + ">" + normName(away));
  if (!o) return "";
  const mk = fmtOdds(home, away, o.market), md = fmtOdds(home, away, o.model);
  if (!mk && !md) return "";
  const row = (lbl, v, cls) =>
    v ? `<span class="od-r ${cls}"><span class="od-l">${lbl}</span>${v}</span>` : "";
  return `<span class="odds" title="Chance of each result — home win / draw / away win. ` +
    `MKT = DraftKings (de-vigged) · MDL = this model.">${row("MKT", mk, "mkt")}${row("MDL", md, "mdl")}</span>`;
}

// pool entries (from the merged leaderboard) who picked a given team
function ownersOf(teamName) {
  const key = normName(teamName);
  return state.merged.filter((e) => (e.picks || []).some((p) => p && normName(p) === key));
}

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
  state.elim = new Set((state.sim.teams || []).filter((t) => t.out).map((t) => t.name));
  state.teamByName = new Map((state.sim.teams || []).map((t) => [normName(t.name), t]));
  state.odds = buildOddsIndex();
  renderProvenance();
  renderResultsBanner();
  renderUpdateStatus();
  renderStatStrip();
  renderRooting();
  renderChampion();
  renderBestPicks();
  renderLeaderboard();
  renderH2H();
  renderMovers();
  renderRecentResults();
  renderBracket();
  renderGroups();
  renderUpcoming();
  renderBigGames();
  renderTeams();
  renderBoot();
  renderFooter();
}

function renderMovers() {
  const sec = $("#movers");
  const ents = state.merged.filter((e) => e.delta_win != null);
  if (!ents.length) { sec.hidden = true; return; }
  sec.hidden = false;
  const pa = state.sim.meta.prev_at ? Date.parse(state.sim.meta.prev_at) : NaN;
  if (!isNaN(pa)) {
    const mins = Math.max(0, Math.round((Date.now() - pa) / 60000));
    $("#movers-tag").textContent = "since " + (mins < 90 ? `${mins}m ago` : `${Math.round(mins / 60)}h ago`);
  }
  const sorted = [...ents].sort((a, b) => b.delta_win - a.delta_win);
  const up = sorted.filter((e) => e.delta_win > 0.0002).slice(0, 6);
  const down = sorted.filter((e) => e.delta_win < -0.0002).slice(-6).reverse();
  const row = (e) => `<div class="mv-row${isMe(e.name) ? " mine" : ""}">` +
    `<span class="mv-name">${isMe(e.name) ? "★ " : ""}${esc(e.name)}</span>` +
    `<span class="mv-win">${pct(e.win_prob)}</span>` +
    `<span class="mv-d ${e.delta_win > 0 ? "up" : "down"}">${e.delta_win > 0 ? "▲" : "▼"}` +
    `${Math.abs(e.delta_win * 100).toFixed(1)}pp${e.rank_delta ? ` · ${e.rank_delta > 0 ? "+" : ""}${e.rank_delta}` : ""}</span></div>`;
  $("#movers-body").innerHTML =
    `<div class="mv-col"><div class="mv-h up">▲ Climbing</div>` +
    `${up.map(row).join("") || '<div class="gnone">no risers this update</div>'}</div>` +
    `<div class="mv-col"><div class="mv-h down">▼ Falling</div>` +
    `${down.map(row).join("") || '<div class="gnone">no fallers this update</div>'}</div>`;
}

/* ---------- knockout bracket (path to the title) ---------- */
// per-team model row (reach-round / title odds), keyed by name
function teamInfo(name) {
  return (state.teamByName && state.teamByName.get(normName(name))) || null;
}

// R32 slot index per team (from the resolved bracket); two teams are on a
// collision course — meeting at the round where their slot paths converge
// (slotA>>L === slotB>>L) — so only one can advance past that round
function teamSlots() {
  const b = state.sim.bracket, m = new Map();
  if (b && b.rounds && b.rounds.R32)
    b.rounds.R32.forEach((n, k) => {
      if (n.home) m.set(normName(n.home), k);
      if (n.away) m.set(normName(n.away), k);
    });
  return m;
}
function meetLevel(sa, sb) {
  if (sa == null || sb == null) return null;
  for (let L = 0; L < KO_ROUNDS.length; L++)
    if ((sa >> L) === (sb >> L)) return L;
  return null;
}
// collision pairs among a list of team names, earliest meeting first
function collisionsAmong(names) {
  const slots = teamSlots(), out = [];
  for (let x = 0; x < names.length; x++)
    for (let y = x + 1; y < names.length; y++) {
      const L = meetLevel(slots.get(normName(names[x])), slots.get(normName(names[y])));
      if (L != null) out.push({ a: names[x], b: names[y], L });
    }
  return out.sort((p, q) => p.L - q.L);
}

/* ---------- "what your entry needs" (per-stage + alive/clinch status) ---------- */
// highest locked-in floor in the field; an entry whose best case can't reach it
// can no longer win the pool ("blocked")
function leaderFloor() {
  const f = state.sim.field || {};
  if (f.leader_floor != null) return f.leader_floor;
  return (state.sim.entries || []).reduce(
    (m, e) => (e.min_final != null ? Math.max(m, e.min_final) : m), 0);
}
// {alive, cushion, html} — is this entry still able to win, and by how much margin
function aliveStatus(e) {
  const lf = leaderFloor();
  if (e.blocked) {
    let why;
    if (e.block_reason === "coverage" && e.blocked_by)
      why = `every team you have left is also <b>${esc(e.blocked_by)}</b>'s — they're ahead and ` +
        `gain whenever you do, so you can't pass them.`;
    else if (e.blocked_by)
      why = `even your best case <b>${e.max_final}</b> can't catch <b>${esc(e.blocked_by)}</b>'s locked <b>${lf}</b>.`;
    else
      why = `best case <b>${e.max_final}</b> can't catch the leader's locked <b>${lf}</b>.`;
    return { alive: false, cushion: 0, html:
      `<span class="as-pill out">✖ out of the pool</span><span class="as-txt">${why}</span>` };
  }
  const cush = e.max_final != null ? e.max_final - lf : null;
  return { alive: true, cushion: cush, html:
    `<span class="as-pill live">✓ still alive</span>` +
    (cush != null
      ? `<span class="as-txt">best case <b>${e.max_final}</b> vs leader's locked <b>${lf}</b> — ` +
        `<b>${cush}</b>-pt cushion before elimination.</span>`
      : "") };
}
// the model's chance an entry's pick wins a given knockout round (banks the points)
function pickRoundProb(name, rnd) {
  const info = teamInfo(name);
  const v = info ? info[KO_PROB_KEY[rnd]] : null;
  return v == null ? null : v;
}
// stage-by-stage ladder: for each of the entry's picks, its odds to win each
// remaining round (= what the entry needs at every stage to keep banking points)
function stageNeedsHTML(e) {
  if (!e || !e.picks || !e.picks.length || !e.path) return "";
  const elim = state.elim || new Set();
  const path = e.path || {};
  const linch = path.linchpin && path.linchpin.team;
  const champWin = path.champion_when_win && path.champion_when_win.team;
  const head = `<div class="sn-row sn-head"><span class="sn-team">your pick</span>` +
    KO_ROUNDS.map((r) => `<span class="sn-c">${KO_COL[r]}<small>+${KO_PTS[r]}</small></span>`).join("") +
    `</div>`;
  const alive = [], dead = [];
  e.picks.forEach((nm, i) => { if (nm) (elim.has(nm) ? dead : alive).push({ nm, tier: i + 1 }); });
  const rowFor = ({ nm, tier }) => {
    const isLinch = linch && normName(nm) === normName(linch);
    const cells = KO_ROUNDS.map((r) => {
      const p = pickRoundProb(nm, r);
      if (p == null) return `<span class="sn-c"><span class="sn-na">–</span></span>`;
      const need = champWin && r === "FINAL" && normName(nm) === normName(champWin);
      return `<span class="sn-c${need ? " need" : ""}"><span class="sn-bar"><i style="width:${Math.min(100, p)}%"></i></span>` +
        `<span class="sn-p">${Math.round(p)}%</span></span>`;
    }).join("");
    return `<div class="sn-row${isLinch ? " sn-linch" : ""}"><span class="sn-team">${getFlag(nm)} ` +
      `<span class="sn-nm">${esc(nm)}${isLinch ? " ⚡" : ""}</span><small>T${tier}</small></span>${cells}</div>`;
  };
  const deadRow = ({ nm, tier }) =>
    `<div class="sn-row sn-dead"><span class="sn-team">${getFlag(nm)} ` +
    `<span class="sn-nm">${esc(nm)}</span><small>T${tier}</small></span>` +
    `<span class="sn-out">out — group points only</span></div>`;
  const sum = path.summary ? `<p class="sn-sum">${esc(path.summary)}</p>` : "";
  const coll = collisionsAmong(alive.map((x) => x.nm));
  const collNote = coll.length
    ? `<p class="sn-coll">⚔ Your picks that collide: ` +
      coll.slice(0, 3).map((c) => `<b>${esc(c.a)}</b> v <b>${esc(c.b)}</b> in ${MEET_LABEL[c.L]}`).join(" · ") +
      (coll.length > 3 ? ` · +${coll.length - 3} more` : "") +
      ` — only one survives each, so you can't bank both past that round.</p>` : "";
  return `<div class="stage-needs">${sum}<div class="sn-table">${head}` +
    alive.map(rowFor).join("") + dead.map(deadRow).join("") + `</div>${collNote}` +
    `<p class="sn-note">Each % is the model's chance your pick <i>wins that round</i> (and banks the ` +
    `points), given results so far. ⚡ = your linchpin · highlighted = the title you usually need.</p></div>`;
}

// winning the pool also needs RIVALS' teams to fail: rank the still-alive teams
// the entry doesn't hold by how much they threaten it (sum of P(owner finishes
// ahead of you) over the entries that hold each team)
function rivalThreats(e) {
  const sim = state.sim, h = sim.h2h;
  if (!h || !h.ahead) return [];
  const mi = (sim.entries || []).findIndex((x) => normName(x.name) === normName(e.name));
  if (mi < 0) return [];
  const elim = state.elim || new Set();
  const mine = new Set((e.picks || []).filter(Boolean).map(normName));
  const byTeam = new Map();
  sim.entries.forEach((a, ai) => {
    if (ai === mi) return;
    const ah = (h.ahead[ai] && h.ahead[ai][mi]) || 0;
    if (ah <= 0.01) return;                       // this entry barely threatens you
    (a.picks || []).forEach((t) => {
      if (!t) return;
      const k = normName(t);
      if (mine.has(k) || elim.has(t)) return;     // shared (cancels) or already gone
      const rec = byTeam.get(k) || { team: t, threat: 0, owners: [] };
      rec.threat += ah;
      rec.owners.push({ name: a.name, ahead: ah });
      byTeam.set(k, rec);
    });
  });
  const arr = [...byTeam.values()];
  arr.forEach((r) => r.owners.sort((x, y) => y.ahead - x.ahead));
  arr.sort((x, y) => y.threat - x.threat);
  return arr.slice(0, 6);
}

function threatsHTML(e) {
  const threats = rivalThreats(e);
  if (!threats.length) return "";
  const rows = threats.map((r) => {
    const info = teamInfo(r.team);
    const title = info && info.title != null ? `${info.title}% title` : "";
    const top = r.owners[0];
    const more = r.owners.length > 1 ? `<span class="th-more"> +${r.owners.length - 1}</span>` : "";
    return `<div class="th-row"><span class="th-team">${getFlag(r.team)} <span class="th-nm">${esc(r.team)}</span></span>` +
      `<span class="th-who">lifts <b>${esc(top.name)}</b>${more}</span>` +
      `<span class="th-odds">${title}</span></div>`;
  }).join("");
  // collisions among the threats: when two play each other one MUST advance, so
  // you can't need both gone there — root for the lesser threat to knock out the bigger
  const tmap = new Map(threats.map((t) => [normName(t.team), t.threat]));
  const coll = collisionsAmong(threats.map((t) => t.team));
  const collNote = coll.length
    ? `<p class="th-coll">⚔ Two of these meet, so one survives regardless — ` +
      coll.slice(0, 3).map((c) => {
        const lesser = (tmap.get(normName(c.a)) || 0) >= (tmap.get(normName(c.b)) || 0) ? c.b : c.a;
        const bigger = lesser === c.a ? c.b : c.a;
        return `<b>${esc(c.a)}</b> v <b>${esc(c.b)}</b> in ${MEET_LABEL[c.L]} (root for ${esc(lesser)} to bump ${esc(bigger)})`;
      }).join(" · ") + `.</p>` : "";
  return `<div class="threats"><div class="ch">Teams you need to bow out ` +
    `<span class="muted">— rivals' picks you don't hold</span></div>` +
    `<div class="th-list">${rows}</div>${collNote}` +
    `<p class="sn-note">Not your teams — but each deep run lifts a rival above you, so winning needs them to lose. ` +
    `Ranked by how much the entries holding them threaten you.</p></div>`;
}

function renderBracket() {
  const sec = $("#bracket");
  const b = state.sim.bracket;
  if (!b || !b.rounds || !(b.rounds.R32 || []).length) { sec.hidden = true; return; }
  sec.hidden = false;
  const order = b.order || ["R32", "R16", "QF", "SF", "FINAL"];
  const myTeams = myPicksSet();
  const elim = state.elim || new Set();
  const sel = state.bracketTeam ? normName(state.bracketTeam) : null;

  // nodes on the selected team's path = its R32 slot + each ancestor (k >> round)
  const onPath = new Set();
  if (sel) {
    const idx = (b.rounds.R32 || []).findIndex(
      (n) => n && (normName(n.home) === sel || normName(n.away) === sel));
    if (idx >= 0) order.forEach((rnd, ri) => onPath.add(rnd + ":" + (idx >> ri)));
  }

  const teamRow = (name, side, node) => {
    if (!name) return `<span class="bk-team tbd"><span class="bk-nm">—</span></span>`;
    const info = teamInfo(name);
    const isWin = node.played && node.winner && normName(node.winner) === normName(name);
    const lost = node.played && node.winner && !isWin;
    const mine = myTeams.has(normName(name));
    let val = "";
    if (node.played) val = `<span class="bk-sc">${side === "home" ? node.hg : node.ag}</span>`;
    else {
      const p = side === "home" ? node.p_home : node.p_away;
      if (p != null) val = `<span class="bk-p">${Math.round(p * 100)}%</span>`;
    }
    const cls = [isWin ? "win" : "", lost ? "lost" : "", mine ? "mine" : "",
      sel === normName(name) ? "sel" : ""].filter(Boolean).join(" ");
    const tip = esc(name) + (info ? ` · ${info.title}% to win it all` : "");
    return `<button class="bk-team ${cls}" data-team="${esc(name)}" title="${tip}">` +
      `<span class="bk-fl">${getFlag(name)}</span>` +
      `<span class="bk-nm">${esc(name)}${mine ? " ★" : ""}</span>${val}</button>`;
  };

  const nodeHTML = (node, rnd, ni) => {
    const hot = onPath.has(rnd + ":" + ni) ? " hot" : "";
    const done = node.played ? " done" : "";
    const tbd = node.tbd ? " tbd" : "";
    return `<div class="bk-match${done}${tbd}${hot}">` +
      teamRow(node.home, "home", node) + teamRow(node.away, "away", node) + `</div>`;
  };

  const cols = order.map((rnd) => {
    const nodes = b.rounds[rnd] || [];
    return `<div class="bk-col bk-${rnd}"><div class="bk-ch">${ROUND_LABEL[rnd] || rnd}</div>` +
      `<div class="bk-col-in">${nodes.map((n, ni) => nodeHTML(n, rnd, ni)).join("")}</div></div>`;
  }).join("");
  const champCol = `<div class="bk-col bk-trophy-col"><div class="bk-ch">Champion</div>` +
    `<div class="bk-col-in"><div class="bk-trophy">🏆 ${b.champion
      ? `${getFlag(b.champion)} <b>${esc(b.champion)}</b>`
      : `<span class="muted">TBD</span>`}</div></div></div>`;
  $("#bracket-body").innerHTML = `<div class="bk-scroll"><div class="bk-grid">${cols}${champCol}</div></div>`;

  // tag: alive count, or the selected team's title odds
  const nOut = (state.sim.teams || []).filter((t) => t.out).length;
  const alive = (state.sim.teams || []).length - nOut;
  const selInfo = sel ? teamInfo(state.bracketTeam) : null;
  $("#bracket-tag").textContent = selInfo
    ? `${state.bracketTeam} · ${selInfo.title}% to win it all`
    : `${alive} alive · ${nOut} out`;

  // path readout for the selected team
  let note;
  if (selInfo) {
    const stops = [["R16", selInfo.winR32], ["QF", selInfo.winR16], ["SF", selInfo.winQF],
      ["Final", selInfo.winSF], ["Champion", selInfo.title]];
    note = `<span class="bk-path-h">${getFlag(state.bracketTeam)} ${esc(state.bracketTeam)}'s path</span>` +
      stops.map(([k, v]) => `<span class="bk-stop"><span class="bk-stk">${k}</span>` +
        `<span class="bk-stv">${(v ?? 0)}%</span></span>`).join("") +
      `<button class="bk-clear" data-bkclear>clear</button>`;
  } else {
    note = "Tap any team to trace its odds to reach each round and win it all. " +
      "★ = your picks · scores show for finished games, model win% for upcoming.";
  }
  $("#bracket-note").innerHTML = note;
}

function renderGroups() {
  const groups = state.sim.groups || [];
  const cond = state.sim.meta.results && state.sim.meta.results.conditional;
  $("#groups-tag").textContent = cond ? "live standings" : "advance odds";
  if (!groups.length) {
    $("#groups-body").innerHTML = `<div class="skeleton">No group data.</div>`;
    return;
  }
  const myTeams = myPicksSet();
  $("#groups-body").innerHTML = groups.map((g) => {
    const rows = g.teams.map((t, i) => {
      const adv = i < 2 ? " adv" : "";
      const out = t.status === "out" ? " gout" : "";
      const mineT = myTeams.has(normName(t.name)) ? " mineteam" : "";
      const end = t.status === "in"
        ? `<td class="gend in" title="Through to the knockouts">✓ in</td>`
        : t.status === "out"
        ? `<td class="gend out" title="Eliminated">out</td>`
        : `<td class="gend live" title="Modeled chance to reach the knockouts">${t.reach}%</td>`;
      return `<tr class="${adv}${out}${mineT}">
        <td class="pos">${i + 1}</td>
        <td class="gflag">${getFlag(t.name)}</td>
        <td class="gname" title="${esc(t.name)}${mineT ? " · your pick" : ""} · ${t.w}W ${t.d}D ${t.l}L · GF ${t.gf} GA ${t.ga}">${esc(t.name)}${mineT ? ' <span class="mydot" title="your pick">★</span>' : ""}</td>
        <td class="gnum">${t.gd > 0 ? "+" + t.gd : t.gd}</td>
        <td class="gpts">${t.pts}</td>${end}</tr>`;
    }).join("");
    const scores = (g.matches && g.matches.length)
      ? `<div class="gscores">${g.matches.map((m) =>
          `<div class="gscore"><span class="h">${esc(m.home)}${getFlag(m.home)}</span>` +
          `<span class="sc">${m.hg}–${m.ag}</span>` +
          `<span class="a">${getFlag(m.away)}${esc(m.away)}</span></div>`).join("")}</div>`
      : (cond ? `<div class="gnone">no matches played yet</div>` : "");
    return `<div class="gcard"><div class="gh"><span class="gl">Group ${esc(g.letter)}</span>` +
      `<span class="gsub">${cond ? "gd · pts · adv" : "advance %"}</span></div>` +
      `<table class="gtable"><tbody>${rows}</tbody></table>${scores}</div>`;
  }).join("");
}

function renderProvenance() {
  const m = state.sim.meta;
  let ago = "";
  const t = Date.parse(m.generated_at);
  if (!isNaN(t)) {
    const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
    ago = mins < 60 ? `${mins}m ago` : `${Math.round(mins / 60)}h ago`;
  }
  const tag = (m.results && m.results.conditional) ? "live results" : "pre-tournament";
  $("#provenance").textContent =
    `${(m.n_sims / 1000).toLocaleString()}k sims · ${tag} · updated ${ago}`;
}

function renderResultsBanner() {
  const el = $("#results-banner");
  const r = (state.sim.meta && state.sim.meta.results) || {};
  if (!r.conditional) { el.hidden = true; return; }
  el.hidden = false;
  const nOut = state.sim.teams.filter((t) => t.out).length;
  const f = state.sim.field || {};
  const E = f.n_entries || (state.sim.entries || []).length;
  const nAlive = f.n_alive != null ? f.n_alive : E - (f.n_blocked || 0);
  const d = Date.parse(r.as_of);
  const ds = isNaN(d) ? null
    : new Date(d).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  el.innerHTML =
    `<span class="lead">Live</span>` +
    `<span>Projections account for <b>${r.matches_played}</b> completed matches` +
    `${ds ? ` (through <b>${esc(ds)}</b>)` : ""}.</span>` +
    `<span class="sep">•</span><span><b>${nOut}</b> teams eliminated</span>` +
    `<span class="sep">•</span><span><b>${nAlive}</b> of <b>${E}</b> entries can still win the pool</span>`;
}

function renderUpdateStatus() {
  const sec = $("#updates");
  if (!sec || !state.sim) return;
  const m = state.sim.meta;
  const sched = m.schedule || {};
  const resHours = sched.results_utc_hours || RESULTS_SWEEP_UTC;
  const oddsHours = sched.odds_utc_hours || ODDS_REFRESH_UTC;
  const now = new Date();

  const resLast = (m.results && m.results.as_of) ? new Date(Date.parse(m.results.as_of)) : null;
  const resNext = nextUtcHour(resHours, now);
  const oddsExact = !!m.odds_at;
  const oddsLast = oddsExact ? new Date(Date.parse(m.odds_at)) : prevUtcHour(oddsHours, now);
  const oddsNext = nextUtcHour(oddsHours, now);

  const ago = (d) => (d ? relParts(now - d) + " ago" : "—");
  const into = (d) => (d ? "in " + relParts(d - now) : "—");
  const sub = (d) => (d ? `<small>${esc(fmtClock(d))}</small>` : "");
  const card = (ic, title, last, next, approx) => `
    <div class="upd">
      <div class="upd-h"><span class="upd-ic">${ic}</span>${title}</div>
      <div class="upd-row"><span class="k">Updated${approx ? " ≈" : ""}</span>
        <span class="v">${ago(last)}${sub(last)}</span></div>
      <div class="upd-row"><span class="k">Next</span>
        <span class="v">${into(next)}${sub(next)}</span></div>
    </div>`;

  sec.innerHTML =
    `<div class="upd-grid">` +
    card("📊", "Odds", oddsLast, oddsNext, !oddsExact) +
    card("⚽", "Results", resLast, resNext, false) +
    `</div>` +
    `<p class="upd-note">Refresh windows are fixed (UTC); shown in your local time.` +
    `${oddsExact ? "" : " Last-odds time is approximate until the next scheduled build records it."}</p>`;
}

// finished matches, newest first — dated from the builder when present, else the
// group cards' match lists (undated)
function recentMatchList() {
  if (Array.isArray(state.sim.recent_results) && state.sim.recent_results.length)
    return state.sim.recent_results.slice().sort((a, b) =>
      (b.date ? Date.parse(b.date) : 0) - (a.date ? Date.parse(a.date) : 0));
  const out = [];
  (state.sim.groups || []).forEach((g) =>
    (g.matches || []).forEach((mt) =>
      out.push({ date: null, round: "GROUP", group: g.letter,
        home: mt.home, away: mt.away, hg: mt.hg, ag: mt.ag })));
  return out.reverse();
}

function renderRecentResults() {
  const sec = $("#recent");
  const matches = recentMatchList();
  if (!matches.length) { sec.hidden = true; return; }
  sec.hidden = false;
  const sc = state.sim.meta.scoring || {};
  const FALLBACK = { R32: 5, R16: 7, QF: 10, SF: 15 };
  const roundPts = (r) => r === "GROUP" ? (sc.group_win ?? 3)
    : r === "FINAL" ? (sc.champion ?? 20) : (sc[r] ?? FALLBACK[r] ?? 0);
  const drawPts = sc.group_draw ?? 1;
  const chip = (e) => `<span class="rr-ent${isMe(e.name) ? " mine" : ""}" title="${esc(e.name)}${e.win_prob != null ? " · " + pct(e.win_prob) + " to win" : " · live only"}">${isMe(e.name) ? "★ " : ""}${esc(e.name)}</span>`;
  const side = (arr) => {
    if (!arr.length) return `<span class="rr-none">none</span>`;
    return arr.slice(0, 6).map(chip).join("") +
      (arr.length > 6 ? `<span class="rr-more">+${arr.length - 6}</span>` : "");
  };
  const LIMIT = 14;
  const rows = matches.slice(0, LIMIT).map((m) => {
    const draw = m.hg === m.ag;
    const homeWin = m.hg > m.ag;
    let helped, hurt, tag;
    if (draw) {
      helped = [...ownersOf(m.home), ...ownersOf(m.away)]
        .sort((a, b) => (b.win_prob ?? -1) - (a.win_prob ?? -1));
      hurt = [];
      tag = `<span class="rr-tag draw">draw · +${drawPts} each</span>`;
    } else {
      const win = homeWin ? m.home : m.away, lose = homeWin ? m.away : m.home;
      helped = ownersOf(win).sort((a, b) => (b.win_prob ?? -1) - (a.win_prob ?? -1));
      hurt = ownersOf(lose).sort((a, b) => (b.win_prob ?? -1) - (a.win_prob ?? -1));
      tag = `<span class="rr-tag win">${getFlag(win)} ${esc(win)} +${roundPts(m.round)}</span>`;
    }
    const meta = [ROUND_LABEL[m.round] || m.round, m.group ? "Grp " + m.group : null,
      m.date ? fmtClock(new Date(Date.parse(m.date))) : null].filter(Boolean).join(" · ");
    return `<div class="rr-row">
      <div class="rr-match">
        <div class="rr-line">
          <span class="h${homeWin ? " w" : ""}">${esc(m.home)}${getFlag(m.home)}</span>
          <span class="sc">${m.hg}–${m.ag}</span>
          <span class="a${!draw && !homeWin ? " w" : ""}">${getFlag(m.away)}${esc(m.away)}</span>
        </div>
        <div class="rr-meta">${esc(meta)} ${tag}</div>
      </div>
      <div class="rr-impact">
        <div class="rr-side help"><span class="lbl">▲ helped</span><span class="ents">${side(helped)}</span></div>
        <div class="rr-side hurt"><span class="lbl">▼ hurt</span><span class="ents">${side(hurt)}</span></div>
      </div>
    </div>`;
  }).join("");
  const note = `<p class="disclaimer">` +
    (matches.length > LIMIT ? `Latest ${LIMIT} of ${matches.length} completed matches. ` : "") +
    `“Helped/hurt” = pool entries who picked the winning / losing team ` +
    `(group win +${sc.group_win ?? 3}, draw +${drawPts}), ordered by win %.</p>`;
  sec.innerHTML =
    `<div class="card-head"><h2>Recent Results <span class="muted">who it helped &amp; hurt</span></h2>` +
    `<span class="tag">live</span></div><div class="rr-list">${rows}</div>${note}`;
}

// upcoming fixtures — dated from the builder when present, else remaining group
// round-robin pairings derived from the standings (undated)
function upcomingList() {
  if (Array.isArray(state.sim.schedule_upcoming) && state.sim.schedule_upcoming.length)
    return state.sim.schedule_upcoming.slice().sort((a, b) =>
      (a.date ? Date.parse(a.date) : Infinity) - (b.date ? Date.parse(b.date) : Infinity));
  const out = [];
  (state.sim.groups || []).forEach((g) => {
    const names = (g.teams || []).map((t) => t.name);
    const played = new Set((g.matches || []).map((mt) =>
      [normName(mt.home), normName(mt.away)].sort().join("|")));
    for (let a = 0; a < names.length; a++)
      for (let b = a + 1; b < names.length; b++) {
        const key = [normName(names[a]), normName(names[b])].sort().join("|");
        if (!played.has(key))
          out.push({ date: null, round: "GROUP", group: g.letter, home: names[a], away: names[b] });
      }
  });
  return out.sort((a, b) => (a.group || "").localeCompare(b.group || ""));
}

function renderUpcoming() {
  const sec = $("#schedule");
  const all = upcomingList();
  if (!all.length) { sec.hidden = true; return; }
  sec.hidden = false;
  const dated = all.some((m) => m.date);
  const LIMIT = 16;
  const list = all.slice(0, LIMIT);
  const elim = state.elim || new Set();
  const matchHTML = (m) => {
    const eh = elim.has(m.home) ? " elim" : "", ea = elim.has(m.away) ? " elim" : "";
    const when = m.date ? fmtTime(new Date(Date.parse(m.date))) : "Grp " + esc(m.group || "?");
    const rd = m.date
      ? (ROUND_LABEL[m.round] || m.round) + (m.group ? " · " + esc(m.group) : "")
      : (m.round === "GROUP" ? "" : (ROUND_LABEL[m.round] || m.round)); // group letter already in "when"
    const odds = oddsWidget(m.home, m.away);
    return `<div class="sch-match">
      <span class="when">${when}</span>
      <span class="teams"><span class="h${eh}">${esc(m.home)}${getFlag(m.home)}</span>
        <span class="vs">v</span>
        <span class="a${ea}">${getFlag(m.away)}${esc(m.away)}</span></span>
      <span class="rd">${rd}</span>
      ${odds ? `<span class="sch-odds">${odds}</span>` : ""}
    </div>`;
  };
  let body;
  if (dated) {
    const days = new Map();
    list.forEach((m) => {
      const k = m.date ? fmtDay(new Date(Date.parse(m.date))) : "TBD";
      if (!days.has(k)) days.set(k, []);
      days.get(k).push(m);
    });
    body = [...days.entries()].map(([day, ms]) =>
      `<div class="sch-day"><div class="sch-dh">${esc(day)}</div>${ms.map(matchHTML).join("")}</div>`).join("");
  } else {
    body = `<div class="sch-day">${list.map(matchHTML).join("")}</div>`;
  }
  const note = (all.length > LIMIT
    ? `Next ${LIMIT} of ${all.length} upcoming matches. `
    : "") + (dated ? "" : "Remaining group fixtures — kickoff times appear once the schedule feed updates.");
  sec.innerHTML =
    `<div class="card-head"><h2>Upcoming <span class="muted">schedule</span></h2>` +
    `<span class="tag">${dated ? "fixtures" : "remaining"}</span></div>` +
    `<div class="sch-grid">${body}</div>` +
    (note ? `<p class="disclaimer">${note}</p>` : "");
}

function fmtPicksFlags(e) {
  if (!e.picks || !e.picks.length)
    return `<span class="muted" style="font-size:12px">live only</span>`;
  return `<div class="picks">${e.picks
    .map((p, i) => {
      const miss = e.unmapped_tiers && e.unmapped_tiers.includes(i + 1);
      const out = state.elim && state.elim.has(p);
      const cls = (miss ? " miss" : "") + (out ? " out" : "");
      const tip = (p || "—") + (out ? " — eliminated" : "");
      return `<span class="pk${cls}" title="${esc(tip)}">${getFlag(p)}</span>`;
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
  // when the visitor has starred an entry, the 4th card becomes "their" standing
  const me = myMergedEntry();
  let myCard;
  if (me) {
    const winRank = state.merged.filter((e) => e.win_prob != null)
      .sort((a, b) => b.win_prob - a.win_prob)
      .findIndex((e) => normName(e.name) === normName(me.name)) + 1;
    const back = leader ? leader.live_total - (me.live_total || 0) : 0;
    const sub = me.win_prob != null
      ? `${winRank ? "win rank #" + winRank : ""}${back > 0 ? " · " + back + " pts back" : (back === 0 && leader ? " · live leader" : "")}`
      : "live only";
    myCard = { k: "My entry ★", v: me.win_prob != null ? pct(me.win_prob) : "—",
      s: sub, cls: "mine" };
  } else {
    myCard = { k: "Win % favorite", v: fav ? fav.name.split(" ")[0] : "—",
      s: fav ? pct(fav.win_prob) + " to win pool" : "", cls: "" };
  }
  const cards = [
    { k: "Entries", v: f.n_entries, s: `fair share ${f.fair_share_pct}%` },
    { k: "Live leader", v: leader ? leader.name.split(" ")[0] : "—",
      s: leader ? `${leader.live_total} pts` : "" },
    { k: "Avg score", v: avg, s: "live points", cls: "cyan" },
    myCard,
    { k: "Boot leader", v: topBoot ? topBoot.player.split(" ").slice(-1)[0] : "—",
      s: topBoot ? pct(topBoot.win / 100) + " · " + topBoot.exp_goals + " xG" : "", cls: "amber" },
  ];
  $("#statstrip").innerHTML = cards
    .map((c) => `<div class="stat ${c.cls || ""}"><div class="k">${esc(c.k)}</div>
      <div class="v">${esc(c.v)}</div><div class="s">${esc(c.s)}</div></div>`)
    .join("");
}

/* ---------- "What to root for" (personalized for the starred entry) ---------- */
// the visitor's starred entry, looked up in the merged leaderboard (or null)
function myMergedEntry() {
  if (!state.myEntry) return null;
  const key = normName(state.myEntry);
  return state.merged.find((m) => normName(m.name) === key) || null;
}
// is this entry the visitor's starred one?
function isMe(name) {
  return state.myEntry && normName(name) === normName(state.myEntry);
}
// set of normalized team names the starred entry picked (for cross-panel highlights)
function myPicksSet() {
  const me = myMergedEntry();
  return new Set((me && me.picks ? me.picks : []).filter(Boolean).map(normName));
}

function renderRooting() {
  const sec = $("#rooting");
  if (!sec || !state.sim) return;
  const root = state.sim.rooting;
  // no tracked games at all -> nothing to root for (e.g. tournament finished)
  if (!root || !Array.isArray(root.games) || !root.games.length) {
    sec.hidden = true; return;
  }
  sec.hidden = false;
  const head = (body) =>
    `<div class="card-head"><h2>Root For <span class="muted">your best outcomes today</span></h2>` +
    `<span class="tag">my entry</span></div>${body}`;

  // not starred yet: prompt the visitor to pick their entry
  if (!state.myEntry) {
    sec.innerHTML = head(
      `<div class="root-cta"><span class="root-cta-star">★</span>` +
      `<div><b>Star your entry</b> in the leaderboard below (tap the ☆ next to your name) ` +
      `and we'll show exactly what to root for in each of today's games to lift your win&nbsp;%.</div></div>`);
    return;
  }

  const me = myMergedEntry();
  const rows = me ? root.by_entry[normName(me.name)] : null;
  if (!me || !rows) {
    sec.innerHTML = head(
      `<div class="root-note">No projection for <b>${esc(state.myEntry)}</b> yet — ` +
      `${me ? "this entry was added after the last model run." : "we couldn't find that entry."} ` +
      `<button class="root-clear" data-rootclear="1">choose a different entry</button></div>`);
    return;
  }

  const base = me.win_prob;            // baseline win %
  const games = root.games;
  const OUT_KEYS = ["home", "draw", "away"]; // index order of each by_entry row
  // assemble per-game outcome cells (skip nulls — e.g. draw for a knockout game)
  const built = games.map((gm, gi) => {
    const vals = rows[gi] || [];
    const cells = [];
    const push = (idx, label, sub) => {
      const v = vals[idx];
      if (v == null) return;
      cells.push({ idx, key: OUT_KEYS[idx], label, sub, val: v });
    };
    push(0, `${getFlag(gm.home)} ${esc(gm.home)} win`, gm.p_home);
    push(1, `Draw`, gm.p_draw);
    push(2, `${getFlag(gm.away)} ${esc(gm.away)} win`, gm.p_away);
    const best = cells.reduce((a, c) => (c.val > a.val ? c : a), cells[0]);
    const worst = cells.reduce((a, c) => (c.val < a.val ? c : a), cells[0]);
    const impact = best && worst ? best.val - worst.val : 0;
    return { gm, cells, best, worst, impact };
  }).filter((b) => b.cells.length);

  if (!built.length) { sec.hidden = true; return; }
  // focus the rooting guide on the next two distinct match-days
  const dayOrder = [];
  built.forEach((b) => { const k = dayLabel(b.gm.date); if (!dayOrder.includes(k)) dayOrder.push(k); });
  const keepDays = new Set(dayOrder.slice(0, 2));
  const focus = built.filter((b) => keepDays.has(dayLabel(b.gm.date)));
  const maxImpact = Math.max(...focus.map((b) => b.impact));

  const chip = (b, c) => {
    const d = c.val - base;            // change vs baseline, in probability
    const cls = c === b.best ? "good" : (c === b.worst && b.impact > 0 ? "bad" : "");
    const dtxt = Math.abs(d) < 0.00005 ? "" :
      `<span class="rc-d ${d > 0 ? "up" : "down"}">${d > 0 ? "+" : "−"}${Math.abs(d * 100).toFixed(1)}</span>`;
    return `<span class="root-chip ${cls}"><span class="rc-l">${c.label}</span>` +
      `<span class="rc-v">${pct(c.val)}${dtxt}</span></span>`;
  };

  const gameRow = (b) => {
    const gm = b.gm;
    const when = gm.date ? fmtTime(new Date(Date.parse(gm.date)))
      : (gm.group ? "Grp " + esc(gm.group) : (ROUND_LABEL[gm.round] || gm.round));
    const rd = [ROUND_LABEL[gm.round] || gm.round, gm.group ? "Grp " + esc(gm.group) : null]
      .filter(Boolean).join(" · ");
    const big = b.impact >= maxImpact && b.impact > 0
      ? `<span class="root-big" title="The most win-%-swinging game for you today">biggest swing</span>` : "";
    const rec = b.best
      ? `<span class="root-rec">Root for <b>${b.best.label}</b></span>` +
        `<span class="root-imp" title="Win-% swing between your best and worst outcome">±${(b.impact * 100).toFixed(1)}pp</span>`
      : "";
    return `<div class="root-game">
      <div class="root-g-head">
        <span class="root-when">${when}</span>
        <span class="root-match"><span class="h">${esc(gm.home)}${getFlag(gm.home)}</span>` +
        `<span class="vs">v</span><span class="a">${getFlag(gm.away)}${esc(gm.away)}</span></span>
        <span class="root-rd">${rd}${big}</span>
      </div>
      <div class="root-chips">${b.cells.map((c) => chip(b, c)).join("")}</div>
      ${oddsWidget(gm.home, gm.away) ? `<div class="root-odds">${oddsWidget(gm.home, gm.away)}</div>` : ""}
      <div class="root-foot">${rec}</div>
    </div>`;
  };

  // group chronologically by local day (Today / Tomorrow / weekday)
  const days = new Map();
  focus.forEach((b) => {
    const k = dayLabel(b.gm.date);
    if (!days.has(k)) days.set(k, []);
    days.get(k).push(b);
  });
  const body = [...days.entries()].map(([day, list]) =>
    `<div class="root-day"><div class="root-dh">${esc(day)}</div>` +
    `<div class="root-grid">${list.map(gameRow).join("")}</div></div>`).join("");

  const hdr = `<div class="root-me">
      <div class="root-me-l"><span class="lbl">Rooting guide for</span>
        <span class="root-name">★ ${esc(me.name)}</span>
        <button class="root-clear" data-rootclear="1" title="Unstar / pick a different entry">change</button></div>
      <div class="root-me-r"><span class="lbl">your win&nbsp;% now</span>
        <span class="root-base">${base != null ? pct(base) : "—"}</span></div>
    </div>`;
  const note = `<p class="disclaimer">Each percentage is your modeled chance to win the pool ` +
    `<i>if that result happens</i>, vs your current ${base != null ? pct(base) : "win %"} — ` +
    `estimated by conditioning the simulations on each outcome (rarer outcomes are noisier).</p>`;
  // alive/clinch status + the full stage-by-stage "what you need" ladder
  const cond = state.sim.meta.results && state.sim.meta.results.conditional;
  const status = cond && me.max_final != null
    ? `<div class="alive-status${me.blocked ? " out" : ""}">${aliveStatus(me).html}</div>` : "";
  const stageBlock = cond
    ? `<div class="sn-block root-needs"><div class="ch">What you need — by stage</div>${stageNeedsHTML(me)}${threatsHTML(me)}</div>`
    : "";
  sec.innerHTML = head(hdr + status + stageBlock + body + note);
}

/* ---------- head-to-head: compare any two entries ---------- */
// entries that have a projection (so they're in the h2h matrix), by win% desc
function h2hEntries() {
  return state.merged
    .filter((e) => e.win_prob != null && e.hasSim !== false)
    .sort((a, b) => b.win_prob - a.win_prob);
}
// normalized team name -> team row (EV, title, out) for the tier compare
function teamInfoMap() {
  const m = new Map();
  (state.sim.teams || []).forEach((t) => m.set(normName(t.name), t));
  return m;
}

function renderH2H() {
  const sec = $("#h2h");
  if (!sec || !state.sim) return;
  const h2h = state.sim.h2h;
  const pool = h2hEntries();
  // need the pairwise matrix and at least two projected entries
  if (!h2h || !Array.isArray(h2h.ahead) || pool.length < 2) { sec.hidden = true; return; }
  sec.hidden = false;
  const idx = new Map((h2h.order || []).map((n, i) => [n, i])); // folded name -> matrix row
  const byName = (nm) => pool.find((e) => normName(e.name) === normName(nm));

  // resolve the two sides (respect explicit picks; sensible defaults otherwise)
  let A = byName(state.h2hA) || byName(state.myEntry) || pool[0];
  let B = byName(state.h2hB);
  if (!B || normName(B.name) === normName(A.name)) {
    const rivalName = A.path && A.path.chief_rival && A.path.chief_rival.name;
    B = byName(rivalName) || pool.find((e) => normName(e.name) !== normName(A.name));
  }
  state.h2hA = A.name; state.h2hB = B.name;

  const opts = (sel) => pool.map((e) =>
    `<option value="${esc(e.name)}"${normName(e.name) === normName(sel.name) ? " selected" : ""}>` +
    `${esc(e.name)} · ${pct(e.win_prob)}</option>`).join("");

  // P(A finishes ahead of B) from the shipped matrix
  const ia = idx.get(normName(A.name)), ib = idx.get(normName(B.name));
  const aAhead = (ia != null && ib != null) ? h2h.ahead[ia][ib] : null;
  const bAhead = (ia != null && ib != null) ? h2h.ahead[ib][ia] : null;
  const aPct = aAhead != null ? Math.round(aAhead * 100) : 50;
  const bPct = bAhead != null ? Math.round(bAhead * 100) : 50;

  const tinfo = teamInfoMap();
  const aPicks = A.picks || [], bPicks = B.picks || [];
  const aSet = new Set(aPicks.filter(Boolean).map(normName));
  const bSet = new Set(bPicks.filter(Boolean).map(normName));
  const shared = aPicks.filter((p) => p && bSet.has(normName(p)));

  // tier-by-tier: same pick, or who holds the higher-EV team
  const tierRows = [];
  for (let t = 0; t < 6; t++) {
    const pa = aPicks[t], pb = bPicks[t];
    const same = pa && pb && normName(pa) === normName(pb);
    const ta = pa ? tinfo.get(normName(pa)) : null;
    const tb = pb ? tinfo.get(normName(pb)) : null;
    const eva = ta ? ta.ev : -1, evb = tb ? tb.ev : -1;
    const edge = same ? "" : eva > evb ? "a" : evb > eva ? "b" : "";
    const cell = (nm, info, side) => {
      const out = info && info.out ? " out" : "";
      const win = !same && edge === side ? " evwin" : "";
      return `<span class="h2h-team h2h-${side}${out}${win}">${getFlag(nm)}` +
        `<span class="h2h-tn">${esc(nm || "—")}</span>` +
        `${info ? `<span class="h2h-ev">EV ${info.ev}</span>` : ""}</span>`;
    };
    tierRows.push(`<div class="h2h-trow${same ? " same" : ""}">
      <span class="h2h-t">T${t + 1}</span>
      ${cell(pa, ta, "a")}
      <span class="h2h-mid">${same ? "same" : "vs"}</span>
      ${cell(pb, tb, "b")}</div>`);
  }

  const side = (e, ahead, p, cls) => `
    <div class="h2h-side ${cls}">
      <div class="h2h-name">${isMe(e.name) ? "★ " : ""}${esc(e.name)}</div>
      <div class="h2h-ahead">${ahead != null ? pct(ahead) : "—"}</div>
      <div class="h2h-sub">finishes ahead</div>
      <div class="h2h-meta">win ${e.win_prob != null ? pct(e.win_prob) : "—"} · live ${e.live_total ?? "—"} · proj ${e.proj_total ?? "—"}</div>
    </div>`;

  sec.innerHTML =
    `<div class="card-head"><h2>Head&#8209;to&#8209;Head <span class="muted">you vs anyone</span></h2>` +
    `<span class="tag">model</span></div>` +
    `<div class="h2h-pick">
       <select class="h2h-sel" data-side="a" aria-label="Entry A">${opts(A)}</select>
       <span class="h2h-vs">vs</span>
       <select class="h2h-sel" data-side="b" aria-label="Entry B">${opts(B)}</select>
     </div>` +
    `<div class="h2h-top">${side(A, aAhead, aPct, "a")}
       <div class="h2h-bar"><i class="a" style="width:${aPct}%"></i><i class="b" style="width:${bPct}%"></i></div>
       ${side(B, bAhead, bPct, "b")}</div>` +
    `<div class="h2h-shared">${shared.length
        ? `Shared picks: ${shared.map((s) => `${getFlag(s)} ${esc(s)}`).join(" · ")} — you rise & fall together there.`
        : "No shared picks — a clean head-to-head."}</div>` +
    `<div class="h2h-tiers">${tierRows.join("")}</div>` +
    `<p class="disclaimer">“Finishes ahead” = share of simulations where that entry ends with the better pool ` +
    `result (points, Golden&nbsp;Boot tiebreak included). Tier <b class="evw">highlight</b> marks the higher-EV pick.</p>`;
}

/* ---------- biggest games for the whole pool ---------- */
function renderBigGames() {
  const sec = $("#biggames");
  if (!sec || !state.sim) return;
  const root = state.sim.rooting;
  const games = (root && Array.isArray(root.games)) ? root.games.filter((g) => g.impact > 0) : [];
  if (!games.length) { sec.hidden = true; return; }
  sec.hidden = false;
  const top = [...games].sort((a, b) => (b.impact || 0) - (a.impact || 0)).slice(0, 8);
  const maxImp = top[0].impact || 1;
  const movers = (arr, dir) => {
    if (!arr || !arr.length) return `<span class="bg-none">—</span>`;
    return arr.slice(0, 3).map((m) =>
      `<span class="bg-mv" title="${esc(m.name)} +${(m.d * 100).toFixed(1)}pp to win"><b>${esc(m.name.split(" ")[0])}</b> +${(m.d * 100).toFixed(1)}</span>`).join("");
  };
  const isKO = (g) => g.round !== "GROUP";
  const card = (g) => {
    const when = g.date ? `${dayLabel(g.date)} ${fmtTime(new Date(Date.parse(g.date)))}`
      : (g.group ? "Grp " + esc(g.group) : (ROUND_LABEL[g.round] || g.round));
    const lines = [
      `<div class="bg-out"><span class="bg-k">${getFlag(g.home)} ${esc(g.home)} win</span><span class="bg-v">${movers(g.movers && g.movers.home)}</span></div>`,
      !isKO(g) ? `<div class="bg-out"><span class="bg-k">Draw</span><span class="bg-v">${movers(g.movers && g.movers.draw)}</span></div>` : "",
      `<div class="bg-out"><span class="bg-k">${getFlag(g.away)} ${esc(g.away)} win</span><span class="bg-v">${movers(g.movers && g.movers.away)}</span></div>`,
    ].join("");
    return `<div class="bg-card">
      <div class="bg-head">
        <span class="bg-match">${esc(g.home)}${getFlag(g.home)} <span class="vs">v</span> ${getFlag(g.away)}${esc(g.away)}</span>
        <span class="bg-when">${when}</span>
      </div>
      <div class="bg-imp"><span class="bg-imp-bar"><i style="width:${(g.impact / maxImp) * 100}%"></i></span>
        <span class="bg-imp-v" title="Total win-% across the pool that swings on this result">${(g.impact * 100).toFixed(0)}pp in play</span></div>
      ${oddsWidget(g.home, g.away) ? `<div class="bg-odds">${oddsWidget(g.home, g.away)}</div>` : ""}
      <div class="bg-outs">${lines}</div>
    </div>`;
  };
  sec.innerHTML =
    `<div class="card-head"><h2>Biggest Games <span class="muted">what most shakes up the pool</span></h2>` +
    `<span class="tag">model</span></div>` +
    `<div class="bigg-grid">${top.map(card).join("")}</div>` +
    `<p class="disclaimer">Ranked by how much pool win&nbsp;% swings on the result. ` +
    `Names show who each outcome helps most (▲ percentage points to win). Conditioned on the simulations.</p>`;
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
  const val = SORT_VAL[state.sortKey] || SORT_VAL.live;
  const dir = state.sortDir === "asc" ? 1 : -1;
  const rows = [...state.merged].sort((a, b) => {
    const va = val(a), vb = val(b);
    let c = typeof va === "string" ? va.localeCompare(vb) : va - vb;
    c *= dir;
    if (c) return c;
    return (b.win_prob ?? -1) - (a.win_prob ?? -1); // stable tiebreak
  });
  updateSortHeaders();
  const maxWin = Math.max(0.01, ...rows.map((r) => r.win_prob || 0));

  const html = rows.map((e, i) => {
    const rank = i + 1;
    // medals only make sense for the standings view
    const g = (state.sortKey === "live" && rank <= 3) ? ` g${rank}` : "";
    const dw = e.delta_win;
    const dchip = (dw != null && Math.abs(dw) >= 0.0005)
      ? `<span class="dch ${dw > 0 ? "up" : "down"}" title="Win-odds change since the last update">${dw > 0 ? "▲" : "▼"}${Math.abs(dw * 100).toFixed(1)}</span>`
      : "";
    const win = e.win_prob != null
      ? `<div class="winbar"><span class="track"><i style="width:${(e.win_prob / maxWin) * 100}%"></i></span>
         <span class="wv">${pct(e.win_prob)}${dchip}</span></div>`
      : `<div class="winbar"><span class="wv muted">—</span></div>`;
    const warn = e.unmapped_tiers && e.unmapped_tiers.length
      ? `<span class="warn" title="Tier(s) ${e.unmapped_tiers.join(",")} not matched to a team">!${e.unmapped_tiers.length}</span>` : "";
    const blk = e.blocked
      ? `<span class="badge-out" title="Can no longer catch the leader, even in the best case">out</span>` : "";
    const boot = e.boot_pick
      ? `<span class="fl">${getFlag((state.sim.golden_boot.race.find((p) => p.player === e.boot_pick) || {}).team)}</span>${esc(e.boot_pick)}`
      : "—";
    const mine = state.myEntry && normName(e.name) === normName(state.myEntry);
    const star = `<span class="starbtn${mine ? " on" : ""}" data-star="${esc(e.name)}" role="button" tabindex="0" ` +
      `title="${mine ? "This is my entry — click to unstar" : "Mark this as my entry"}" ` +
      `aria-label="${mine ? "Unstar my entry" : "Mark as my entry"}">${mine ? "★" : "☆"}</span>`;
    const main = `<tr class="row${state.open.has(e.name) ? " open" : ""}${e.blocked ? " blocked" : ""}${mine ? " mine" : ""}" data-name="${esc(e.name)}">
      <td class="c-rank"><span class="rankbadge${g}">${rank}</span></td>
      <td class="c-name"><div class="ename">${star}<span class="caret">▶</span>${esc(e.name)} ${warn}${blk}</div></td>
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
  const LBL = { name: "entry name", live: "live points", proj: "projected final",
                win: "win %", boot: "Golden Boot pick" };
  $("#lb-disclaimer").innerHTML =
    `Sorted by <b>${LBL[state.sortKey]}</b> (${state.sortDir === "asc" ? "ascending" : "descending"}). ` +
    `Click any column header to re-sort, or a row for its path to victory. ` +
    `Win % is each entry's modeled chance of winning the pool.`;
}

function updateSortHeaders() {
  document.querySelectorAll("#lb thead th.sortable").forEach((th) => {
    const active = th.dataset.sort === state.sortKey;
    th.classList.toggle("sorted", active);
    const sind = th.querySelector(".sind");
    if (sind) sind.textContent = active ? (state.sortDir === "asc" ? "▲" : "▼") : "";
  });
}

const STAT_TIP = {
  "Win pool": "Modeled chance you finish 1st in the pool",
  "Top 3": "Chance you finish in the top 3",
  "Avg finish": "Your average finishing position across all simulations",
  "Best case": "The highest final score your lineup can still reach",
  "Guaranteed": "Points already locked in — your minimum final score",
  "Champ when you win": "Who lifts the World Cup in most of your winning simulations",
  "Typical winning score": "Median final score in the sims where you win",
  "Chief rival": "The entry most often ahead of you when you finish 2nd",
};

function rivalWhy(p) {
  const r = p.chief_rival;
  if (!r) return "";
  const fl = (ns) => ns.map((n) => `${getFlag(n)} ${esc(n)}`).join(", ");
  let why = `<b>${esc(r.name)}</b> is ahead of you ${r.pct}% of the time you finish 2nd. `;
  why += (r.shared && r.shared.length)
    ? `You both have ${fl(r.shared)} (you rise and fall together there). `
    : `You share no picks, so it's a clean head-to-head. `;
  if (r.rival_edge && r.rival_edge.length)
    why += `Their edge teams: <span class="red">${fl(r.rival_edge)}</span>. `;
  if (r.decisive)
    why += `The biggest swing is <b>Tier ${r.decisive.tier}</b> — their ${esc(r.decisive.rival_team)} ` +
      `averages <b class="red">+${r.decisive.gap}</b> over your ${esc(r.decisive.your_team)} ` +
      `in the sims where they beat you.`;
  return `<div class="rival-why"><span class="ch">Why ${esc(r.name)} is your rival</span>` +
    `<p>${why}</p></div>`;
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
  const cond = state.sim.meta.results && state.sim.meta.results.conditional;
  const stats = [
    e.win_prob != null ? { k: "Win pool", v: pct(e.win_prob), acc: 1 } : null,
    e.p_top3 != null ? { k: "Top 3", v: pct(e.p_top3) } : null,
    e.exp_finish != null ? { k: "Avg finish", v: String(e.exp_finish) } : null,
    cond && e.max_final != null ? { k: "Best case", v: String(e.max_final) } : null,
    cond && e.min_final != null ? { k: "Guaranteed", v: String(e.min_final) } : null,
    p.champion_when_win ? { k: "Champ when you win", v: `${getFlag(p.champion_when_win.team)} ${esc(p.champion_when_win.team)} ${p.champion_when_win.pct}%` } : null,
    p.typical_winning_score ? { k: "Typical winning score", v: String(p.typical_winning_score) } : null,
    p.chief_rival ? { k: "Chief rival", v: `${esc(p.chief_rival.name)} (${p.chief_rival.pct}%)` } : null,
  ].filter(Boolean);
  // alive / elimination status + per-stage needs (knockout era only)
  const status = cond && e.max_final != null
    ? `<div class="alive-status${e.blocked ? " out" : ""}">${aliveStatus(e).html}</div>` : "";
  const needs = cond
    ? `<div class="sn-block"><div class="ch">What you need — by stage</div>${stageNeedsHTML(e)}${threatsHTML(e)}</div>` : "";
  return `<tr class="detail"><td colspan="7"><div class="detail-inner">
    ${status}
    <div>
      <div class="path-summary"><span class="lead">Path to victory</span>${esc(p.summary)}</div>
      <div class="path-stats">${stats.map((s) =>
        `<div class="pstat" title="${esc(STAT_TIP[s.k] || "")}"><div class="k">${esc(s.k)}</div>` +
        `<div class="v${s.acc ? " acc" : ""}">${s.v}</div></div>`).join("")}</div>
      ${rivalWhy(p)}
    </div>
    <div class="carries"><div class="ch">Points carried (when you win)</div>${carries}</div>
    ${needs}
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
    return `<div class="tcard${t.out ? " out" : ""}">
      <div class="top"><span class="fl">${getFlag(t.name)}</span><span class="nm">${esc(t.name)}</span>
        ${t.out ? `<span class="elim">out</span>`
                : `<span class="tr">T${t.tier} · ${esc(t.group)}</span>`}</div>
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
        <div class="own-tag" title="Goals scored so far · expected goals remaining · chance of 6+ total">${r.current} now · +${r.remaining} exp · ${r.p_6plus}% 6+</div></div>
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

  const cond = state.sim.meta.results && state.sim.meta.results.conditional;
  $("#how-note").innerHTML = cond
    ? `Projections are a Monte&nbsp;Carlo simulation <strong>conditioned on results so far</strong>: ` +
      `completed matches are fixed and only the remaining games are simulated (calibrated to ` +
      `DraftKings odds). <strong>Win&nbsp;%</strong> is each entry's modeled chance of finishing ` +
      `first, Golden&nbsp;Boot tiebreak included. <strong>Blocked from winning</strong> means an ` +
      `entry's best-case final score can no longer reach the current leader's locked-in total. ` +
      `Live points come from the pool's Google&nbsp;Sheet.`
    : `Projections come from a Monte&nbsp;Carlo simulation of the full 2026 bracket, calibrated to ` +
      `DraftKings title &amp; match odds. <strong>Win&nbsp;%</strong> is each entry's modeled chance ` +
      `of finishing first (Golden&nbsp;Boot tiebreak included). Before kickoff the model replays the ` +
      `whole tournament from today's odds; once games are played it conditions on real results. ` +
      `Live points come from the pool's Google&nbsp;Sheet.`;
}

/* ---------- interactions ---------- */
// star/unstar an entry as "mine"; persist across visits (best-effort)
function setMyEntry(name) {
  state.myEntry = name;
  try {
    if (name) localStorage.setItem(MY_ENTRY_KEY, name);
    else localStorage.removeItem(MY_ENTRY_KEY);
  } catch (e) { /* storage disabled (private mode) — keep in-memory only */ }
  if (name) { state.h2hA = name; state.h2hB = null; } // your star becomes side A
  render(); // refresh every panel that highlights "you"
}
function toggleMyEntry(name) {
  const isMine = state.myEntry && normName(state.myEntry) === normName(name);
  setMyEntry(isMine ? null : name);
}

function wire() {
  $("#lb-body").addEventListener("click", (ev) => {
    const sb = ev.target.closest(".starbtn");
    if (sb) { ev.stopPropagation(); toggleMyEntry(sb.dataset.star); return; }
    const tr = ev.target.closest("tr.row");
    if (!tr) return;
    const name = tr.dataset.name;
    if (state.open.has(name)) state.open.delete(name); else state.open.add(name);
    renderLeaderboard();
  });
  // keyboard: Enter/Space on a focused star toggles it
  $("#lb-body").addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const sb = ev.target.closest(".starbtn");
    if (!sb) return;
    ev.preventDefault(); ev.stopPropagation();
    toggleMyEntry(sb.dataset.star);
  });
  // "change" / "choose a different entry" inside the Root For panel -> unstar
  const rootSec = $("#rooting");
  if (rootSec) rootSec.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-rootclear]")) {
      setMyEntry(null);
      $("#leaderboard").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
  // head-to-head selectors
  const h2hSec = $("#h2h");
  if (h2hSec) h2hSec.addEventListener("change", (ev) => {
    const sel = ev.target.closest(".h2h-sel"); if (!sel) return;
    if (sel.dataset.side === "a") state.h2hA = sel.value; else state.h2hB = sel.value;
    renderH2H();
  });
  const lbHead = document.querySelector("#lb thead");
  if (lbHead) lbHead.addEventListener("click", (ev) => {
    const th = ev.target.closest("th.sortable");
    if (!th) return;
    const key = th.dataset.sort;
    if (state.sortKey === key)
      state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    else { state.sortKey = key; state.sortDir = SORT_DEFAULT_DIR[key] || "desc"; }
    renderLeaderboard();
  });
  // knockout bracket: tap a team to trace its path; "clear" resets
  const bk = $("#bracket");
  if (bk) bk.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-bkclear]")) { state.bracketTeam = null; renderBracket(); return; }
    const t = ev.target.closest(".bk-team[data-team]");
    if (!t) return;
    const name = t.dataset.team;
    const same = state.bracketTeam && normName(state.bracketTeam) === normName(name);
    state.bracketTeam = same ? null : name;
    renderBracket();
  });
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
  if (state.countdown % 30 === 0 && state.sim) renderUpdateStatus(); // keep "ago/next" fresh
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
  // restore the visitor's starred entry (localStorage), or a #me=<name> deep link
  try {
    const saved = localStorage.getItem(MY_ENTRY_KEY);
    if (saved) state.myEntry = saved;
  } catch (e) { /* storage disabled */ }
  if (location.hash.startsWith("#me=")) {
    const nm = decodeURIComponent(location.hash.slice(4));
    const hit = state.merged.find((m) => normName(m.name) === normName(nm));
    if (hit) {
      state.myEntry = hit.name;
      try { localStorage.setItem(MY_ENTRY_KEY, hit.name); } catch (e) { /* noop */ }
    }
  }
  // optional shareable sort: ?sort=win|live|proj|name|boot[&dir=asc|desc]
  const sp = new URLSearchParams(location.search);
  if (sp.get("sort") && SORT_VAL[sp.get("sort")]) {
    state.sortKey = sp.get("sort");
    state.sortDir = sp.get("dir") === "asc" ? "asc"
      : sp.get("dir") === "desc" ? "desc" : (SORT_DEFAULT_DIR[state.sortKey] || "desc");
  }
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
