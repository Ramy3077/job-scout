// The Scout: front end. Plain JS, no build step.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

const api = async (path, params, body) => {
  const opts = { method: "POST" };
  if (body != null) { opts.body = body; opts.headers = { "Content-Type": "text/plain;charset=utf-8" }; }
  try {
    const r = await fetch("/api/" + path + (params ? "?" + new URLSearchParams(params) : ""), opts);
    return await r.json();
  } catch (e) { return { ok: false, error: "the server did not answer" }; }
};
const get = async (path) => (await fetch("/api/" + path)).json();

const asDate = (s) => new Date(s.length <= 10 ? s + "T12:00" : s);
const short = (s) => asDate(s).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
const dm = (s) => asDate(s).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
const daysTo = (s) => Math.ceil((asDate(s) - Date.now()) / 864e5);

let S = null;            // /api/state
let JD = {};             // CV builds by job_key: {state, started_at, error, ...}
let showCold = false;    // awaiting results: the 6-weeks-silent tail is folded by default
// Per-fixture drawer: which panel is open ("jd" or "report"), the draft, and a pending warning.
const DRAW = {};
const drawer = (k) => (DRAW[k] ||= { open: null, draft: "", warned: "" });

// ── toast + confetti ───────────────────────────────────────────
let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("on"), 3600);
}

const cv = $("confetti");
const cx = cv.getContext("2d");
let bits = [];
function confetti(x, y, n = 70) {
  if (REDUCED) return;
  cv.width = innerWidth * devicePixelRatio;
  cv.height = innerHeight * devicePixelRatio;
  const cols = ["#d9481f", "#efb32d", "#2f6b3c", "#a6dcc6", "#1d1a16"];
  for (let i = 0; i < n; i++) {
    bits.push({
      x: x * devicePixelRatio, y: y * devicePixelRatio,
      vx: (Math.random() - .5) * 18, vy: -Math.random() * 16 - 4,
      r: Math.random() * Math.PI, vr: (Math.random() - .5) * .4,
      w: 6 + Math.random() * 8, h: 4 + Math.random() * 6,
      c: cols[i % cols.length], life: 0,
    });
  }
  if (bits.length === n) requestAnimationFrame(frame);
}
function frame() {
  cx.clearRect(0, 0, cv.width, cv.height);
  bits = bits.filter((b) => b.life < 140 && b.y < cv.height + 40);
  for (const b of bits) {
    b.life++; b.vy += .55; b.vx *= .985; b.x += b.vx; b.y += b.vy; b.r += b.vr;
    cx.save(); cx.translate(b.x, b.y); cx.rotate(b.r);
    cx.fillStyle = b.c; cx.globalAlpha = Math.max(0, 1 - b.life / 140);
    cx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h); cx.restore();
  }
  if (bits.length) requestAnimationFrame(frame);
  else cx.clearRect(0, 0, cv.width, cv.height);
}

// ── masthead + ticker ──────────────────────────────────────────
function season(today) {
  // a season runs August to July, like a recruiting cycle
  const d = asDate(today), y = d.getFullYear() % 100, start = d.getMonth() >= 7 ? y : y - 1;
  return `Season ${String(start).padStart(2, "0")}/${String((start + 1) % 100).padStart(2, "0")}`;
}
function renderMast() {
  $("m-season").textContent = season(S.today);
  $("m-week").textContent = "Matchweek " + S.matchweek;
  $("m-date").textContent = short(S.today);
  $("scan-note").textContent = S.demo ? "Demo: a made-up candidate" : S.last_scan ? `Last scouted ${short(S.last_scan)}` : "";
  const label = { sent: "Sent", interviewing: "Interview", offer: "Offer", rejected: "FT" };
  const tail = { sent: "", interviewing: " · through to interview", offer: " · offer in", rejected: " · not progressed" };
  const items = S.ticker.map((t) =>
    `<span class="k-${t.kind}"><b>${label[t.kind]}</b>${esc(t.company)}${t.title ? " · " + esc(t.title.slice(0, 48)) : ""}${tail[t.kind]} <i style="opacity:.5;font-style:normal">${short(t.at)}</i></span>`).join("");
  $("ticker").innerHTML = items ? items + items : `<span>Nothing sent yet. The first application starts the season.</span>`;   // twice, so the loop is seamless
  $("foot").innerHTML = S.demo
    ? `Demo data for a made-up candidate, Alex Example. Everything resets every few hours. CVs are built offline from the profile and every line is checked against it. <a href="https://github.com/Ramy3077/job-scout">Source on GitHub</a>.`
    : `${S.season.tracked} roles tracked. Everything runs on this machine. Roles arrive from the sources you set up (see docs/sources.md), or add one by hand under Pick your own.`;
}

// ── matchday ───────────────────────────────────────────────────
function renderHero() {
  const n = S.sent_week, t = S.target;
  $("sent-n").textContent = n;
  $("target-n").textContent = t;
  $("balls").innerHTML = Array.from({ length: t }, (_, i) =>
    `<span class="ball ${i < n ? "full" : ""}" style="animation-delay:${i * .07}s"></span>`).join("");
  const left = t - n;
  $("hero-line").innerHTML = n === 0 ? `Kick-off. <b>${t} to send</b> before Sunday.`
    : left > 0 ? `<b>${n} in.</b> ${left} to go before Sunday.`
    : `<b>Target hit.</b> Anything more is a bonus.`;
  $("form-sub").textContent = `last ${S.form.length} week${S.form.length === 1 ? "" : "s"} · W = ${t} sent`;
  $("form").innerHTML = S.form.map((w) => {
    const letter = w.result === "live" ? "·" : w.result;
    return `<div class="fm ${w.result}" title="w/c ${dm(w.week)}: ${w.sent} sent"><i>${letter}</i><small>${w.sent}</small></div>`;
  }).join("");
  const rec = S.weeks.filter((w) => w.result !== "live");
  const W = rec.filter((w) => w.result === "W").length, D = rec.filter((w) => w.result === "D").length;
  $("streak-line").innerHTML = (S.streak > 1 ? `<b>${S.streak}-week winning run.</b> ` : S.streak === 1 ? "<b>Won last week.</b> " : "")
    + (rec.length ? `Season record W${W} D${D} L${rec.length - W - D}.` : "The form guide fills in as the weeks go by.");
}

// ── CV builds ──────────────────────────────────────────────────
const JD_MIN = 120;
function since(iso) {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + (s % 60) + "s";
}
function jdMsg(st) {
  if (st.state === "queued") return "Queued behind another build…";
  if (st.state === "running")
    return (S && S.demo ? "Choosing the best-matching lines from the profile… " : "Reading the JD and tailoring a CV… ") + since(st.started_at || st.at);
  if (st.state === "failed") return st.error || "The build failed.";
  return "";
}

// Cheap sanity check, not a gate: does the pasted text mention this employer and any word of
// the title? Pasting the wrong posting builds a CV for the wrong job without a sound.
const GENERIC = new Set(["ltd", "inc", "llc", "plc", "the", "and", "intern", "internship",
  "placement", "graduate", "summer", "scheme", "junior"]);
function jdMismatch(r, text) {
  const t = text.toLowerCase();
  const words = (s) => (s || "").toLowerCase().split(/[^a-z0-9+#]+/).filter((w) => w.length > 2 && !GENERIC.has(w));
  const co = words(r.company), role = words(r.title);
  const coHit = !co.length || co.some((w) => t.includes(w));
  const roleHit = !role.length || role.some((w) => t.includes(w));
  if (coHit && roleHit) return "";
  return "This text does not mention " + (coHit ? "the role title (" + r.title + ")"
    : roleHit ? r.company : r.company + " or " + r.title) + ". It may be a different job. Check it, or build anyway.";
}

// What the builder did with the profile for this JD. Every kept line is a verbatim profile
// line, and dropped lines are real profile lines that did not make the cut, so this is the
// "cannot invent" mechanism made visible. Score = share of the JD's weighted terms the line hits.
function reportHTML(rep) {
  const li = (x, cls) => `<li class="${cls}"><span class="sc">${x.score}%</span><span class="tx"><span class="sec">${esc(x.section)}: </span>${esc(x.text)}</span></li>`;
  const kept = rep.kept || [], dropped = rep.dropped || [];
  const matched = kept.filter((x) => !x.filler), filler = kept.filter((x) => x.filler);
  return `<div class="report">
    <h4>Kept, matched the JD <small>${matched.length} lines, best first within each role</small></h4>
    <ul>${matched.length ? matched.map((x) => li(x, "keep")).join("") : `<li class="fill"><span></span><span class="tx">No line in the profile matched this JD.</span></li>`}</ul>
    ${filler.length ? `<h4>Kept to fill the page <small>${filler.length} lines, no JD match</small></h4><ul>${filler.map((x) => li(x, "fill")).join("")}</ul>` : ""}
    <h4>Dropped <small>${dropped.length} profile lines left off</small></h4>
    <ul>${dropped.map((x) => li(x, "drop")).join("")}</ul>
    ${(rep.skills_up || []).length ? `<h4>Skills moved up</h4><div class="skills">${rep.skills_up.map((x) => `<span class="tag ok">${esc(x.text)} ${x.score}%</span>`).join("")}</div>` : ""}
    <p class="fineprint">Match is the share of the job description's weighted terms a line mentions. Nothing is reworded: every line is copied from the profile and checked against it before the PDF is written.</p>
  </div>`;
}

function jdDrawerHTML(r) {
  const st = JD[r.job_key];
  if (st && (st.state === "queued" || st.state === "running"))
    return `<div class="jdstate" id="jdst-${esc(r.job_key)}"><span class="dot"></span><span class="txt">${esc(jdMsg(st))}</span></div>`;
  const D = drawer(r.job_key);
  const failed = st && st.state === "failed"
    ? `<div class="jdstate bad"><span class="dot"></span><span>${esc(jdMsg(st))}</span></div>` : "";
  const n = D.draft.trim().length;
  const warned = D.warned && D.warned === D.draft.trim();
  return `${failed}<textarea data-jd placeholder="Paste the whole job description: responsibilities, requirements, tech stack. It is read as job text only, never as instructions." aria-label="Job description for ${esc(r.company)}">${esc(D.draft)}</textarea>
    ${warned ? `<div class="jdwarn">${esc(jdMismatch(r, D.draft.trim()))}</div>` : ""}
    <div class="jdrow"><span class="cnt" data-cnt>${n < JD_MIN ? n + " / " + JD_MIN + " characters" : n.toLocaleString() + " characters"}</span>
      ${r.sample_jd ? `<button class="btn btn-ghost sm" data-do="sample">Paste sample JD</button>` : ""}
      <button class="btn btn-claret sm" data-do="build">${warned ? "Build anyway" : "Build CV"}</button></div>
    <p class="fineprint">${S.demo ? "The demo builds offline: it picks lines from the profile that match this text, and can only use lines the profile already has." : "Builds one CV from your profile for this posting. It never adds a skill or job title your profile does not have."}</p>`;
}

function fixture(r, i) {
  let ko = `<span class="tag">rolling</span>`;
  if (r.deadline) {
    const d = daysTo(r.deadline);
    ko = `<span class="tag ${d <= 7 ? "hot" : d <= 21 ? "warm" : ""}">closes ${short(r.deadline)} · ${d}d</span>`;
  }
  const st = JD[r.job_key];
  const busy = st && (st.state === "queued" || st.state === "running");
  const SRC = { jd: ["ok", "CV from the posting"], search: ["", "CV from research"], "title-only": ["warm", "general CV"] };
  const src = !r.cv ? `<span class="tag warm">no CV yet</span>`
    : SRC[r.tailoring] ? `<span class="tag ${SRC[r.tailoring][0]}">${SRC[r.tailoring][1]}</span>` : "";
  const failed = st && st.state === "failed" ? `<span class="tag hot">last build failed</span>` : "";
  const D = drawer(r.job_key);
  if (busy) D.open = "jd";
  const cvBtns = r.cv
    ? `<a class="btn sm" href="/cv/${encodeURIComponent(r.cv)}" target="_blank" rel="noopener">CV</a><a class="btn btn-ghost sm" href="/cv/${encodeURIComponent(r.cv)}?dl=1" download title="Download the PDF">PDF</a>` : "";
  const tl = busy ? `<button class="btn sm busy" disabled>${st.state === "queued" ? "Queued…" : "Building…"}</button>`
    : `<button class="btn ${r.cv ? "btn-ghost" : "btn-claret"} sm" data-do="jd" aria-expanded="${D.open === "jd"}">${D.open === "jd" ? "Close" : r.cv ? "Re-tailor" : "Build CV"}</button>`;
  const rep = r.report ? `<button class="btn btn-ghost sm" data-do="report" aria-expanded="${D.open === "report"}">What went in</button>` : "";
  const body = D.open === "jd" ? jdDrawerHTML(r) : D.open === "report" && r.report ? reportHTML(r.report) : "";
  return `<article class="fx ${r.why === "pinned" ? "pinned" : ""}" style="animation-delay:${i * .07}s" data-key="${esc(r.job_key)}">
    <div class="fx-rate"><b>${r.fit ?? "–"}</b><small>${r.why === "pinned" ? "your pick" : "fit"}</small></div>
    <div class="fx-main">
      <div class="fx-vs"><span class="you">You</span><span class="v">v</span>${esc(r.company)}</div>
      <div class="fx-role">${esc(r.title)}</div>
      ${r.note ? `<div class="fx-note">${esc(r.note)}</div>` : ""}
      <div class="fx-tags">${ko}${src}${failed}${r.location ? `<span class="tag">${esc(r.location)}</span>` : ""}${r.why === "pinned" ? `<span class="tag mine">picked by you</span>` : ""}</div>
    </div>
    <div class="fx-acts">
      <div class="row">${r.url ? `<a class="btn sm" href="${esc(r.url)}" target="_blank" rel="noopener">Posting</a>` : ""}${cvBtns}</div>
      <div class="row">${tl}${rep}</div>
      <button class="btn btn-pitch" data-do="sent">Sent it</button>
      <div class="row">
        <button class="btn btn-ghost sm" data-do="swap" title="Not this week: bring on the next one">Swap</button>
        <button class="btn btn-ghost sm" data-do="drop" title="Not interested">Drop</button>
      </div>
    </div>${body ? `<div class="drawer">${body}</div>` : ""}</article>`;
}

function renderFixtures() {
  const left = S.target - S.sent_week;
  $("fixtures").innerHTML = S.five.map(fixture).join("") || (left > 0
    ? `<div class="empty-fx"><b>No fixtures</b>Nothing open scores ${S.demo ? "high enough" : "high enough yet"}. Pick your own, or add a role by hand.</div>`
    : `<div class="empty-fx"><b>Full time</b>${S.target} sent this week. Rest, or get ahead on next week.</div>`);
  // the cards rise in once; later re-renders (a drawer opening, a build landing) stay put
  requestAnimationFrame(() => setTimeout(() => $("fixtures").classList.add("settled"), 900));
  $("fx-sub").textContent = S.five.length ? `${S.five.length} on the card this week` : "this week's applications";
  $("bench").textContent = S.reserve > 0 ? `${S.reserve} more on the bench. Swap brings the next one on.` : "";
}

function renderWaiting() {
  const row = (w) => {
    const d = w.days ?? 0, cold = d > 42;
    return `<li class="${cold ? "cold" : ""}" title="${cold ? "Six weeks or more with no word usually means no" : ""}">
      <div class="club"><b>${esc(w.company)}</b><span>${esc(w.title)}${w.status === "interviewing" ? " · interviewing" : ""}</span></div>
      <div class="bar"><i style="width:${Math.min(100, d / 70 * 100)}%"></i></div>
      <div class="days">${d}d</div></li>`;
  };
  // Six weeks of silence usually means no. Those rows fold into one at the bottom, so the
  // table shows the applications that can still move.
  const warm = S.waiting.filter((w) => (w.days ?? 0) <= 42);
  const cold = S.waiting.filter((w) => (w.days ?? 0) > 42);
  const fold = cold.length ? `<li class="fold" data-fold role="button" tabindex="0">
      <div class="club"><b>${cold.length} silent for six weeks or more</b><span>Probably gone. ${showCold ? "Click to fold them away." : "Click to list them."}</span></div>
      <div class="bar"></div><div class="days">${Math.min(...cold.map((w) => w.days))}d+</div></li>` : "";
  $("waiting").innerHTML = S.waiting.length
    ? `<li class="hdr"><div>Club</div><div>Waiting</div><div class="days">Days</div></li>` + warm.map(row).join("") + fold + (showCold ? cold.map(row).join("") : "")
    : `<li class="empty"><div class="club"><span>Nothing out yet. Press Sent it on a fixture once you apply.</span></div></li>`;
  $("wait-sub").textContent = `${warm.length} still live${cold.length ? ` · ${cold.length} silent for 6+ weeks` : ""}`;
}
$("waiting").addEventListener("click", (e) => {
  if (!e.target.closest("[data-fold]")) return;
  showCold = !showCold;
  renderWaiting();
});
$("waiting").addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.closest("[data-fold]")) { e.preventDefault(); showCold = !showCold; renderWaiting(); }
});

// ── season ─────────────────────────────────────────────────────
function renderSeason() {
  const s = S.season;
  $("stats").innerHTML = [
    [s.sent, "sent", "c"], [s.live, "still live", ""], [s.interviews, "interviews", "g"],
    [s.offers, "offers", "g"], [s.rejected, "not progressed", "r"], [s.tracked, "roles tracked", ""],
  ].map(([n, l, c]) => `<div class="stat ${c} rise"><b>${n}</b><span>${l}</span></div>`).join("");
  $("chart").innerHTML = chartHTML(S.weeks.map((w) => ({ week: w.week, n: w.sent })), S.target, `target ${S.target}`);
  const steps = [["Sent", s.sent], ["Interview", s.interviews], ["Offer", s.offers]];
  $("funnel").innerHTML = steps.map(([l, n], i) =>
    `<div class="fn"><span>${l}</span><div class="track"><i style="width:${s.sent ? Math.max(n ? 1.5 : 0, n / s.sent * 100) : 0}%;animation-delay:${i * .1}s"></i></div><b>${n}</b></div>`).join("");
}

// bars sit in a fixed 182px box above a 26px label row, so the target line is exact
function chartHTML(weeks, target, label) {
  const max = Math.max(target + 2, ...weeks.map((w) => w.n));
  return `<div class="goal" data-label="${esc(label)}" style="bottom:${26 + (target / max) * 160}px"></div>` +
    weeks.map((w, i) => `<div class="col ${w.n >= target ? "won" : ""}"><div class="bars"><b>${w.n || ""}</b>
        <i style="height:${Math.max(2, (w.n / max) * 160)}px;animation-delay:${i * .04}s"></i></div><small>${dm(w.week)}</small></div>`).join("");
}

function renderAll() {
  renderMast(); renderHero(); renderFixtures(); renderWaiting(); renderSeason();
}

async function load() {
  S = await get("state");
  // a half-typed job description must not be wiped by a refresh
  const typing = document.activeElement?.matches?.("textarea[data-jd]");
  if (typing) { renderMast(); renderHero(); renderWaiting(); renderSeason(); }
  else renderAll();
}

// builds: poll while any is in flight, announce each one as it lands
let jdT;
const jdSig = (o) => Object.keys(o).sort().map((k) => k + ":" + o[k].state).join("|");
async function pollJD() {
  const before = jdSig(JD);
  try { JD = await get("jdstatus"); } catch (e) { return; }
  const done = Object.entries(JD).filter(([, s]) => s.state === "done");
  if (done.length) {
    S = await get("state");
    for (const [k, s] of done) {
      drawer(k).open = "report";
      drawer(k).draft = "";
      await api("jddismiss", { job_key: k });
      delete JD[k];
      toast(`CV ready: ${s.company}. Here is what went in.`);
    }
    renderAll();
    const card = document.querySelector(`.fx[data-key="${CSS.escape(done[0][0])}"]`);
    if (card) {
      const r = card.getBoundingClientRect();
      confetti(r.right - 80, r.top + 30, 40);
      card.querySelector(".drawer")?.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "nearest" });
    }
  } else if (jdSig(JD) !== before) {
    renderFixtures();
  } else {
    // same builds, same states: just tick the elapsed time, so nobody loses their caret
    for (const [k, s] of Object.entries(JD)) {
      const t = document.querySelector(`#jdst-${CSS.escape(k)} .txt`);
      if (t) t.textContent = jdMsg(s);
    }
  }
  clearTimeout(jdT);
  if (Object.values(JD).some((s) => s.state === "queued" || s.state === "running")) jdT = setTimeout(pollJD, 1500);
}

async function submitJD(r, card, btn) {
  const D = drawer(r.job_key), text = D.draft.trim();
  if (text.length < JD_MIN) {
    toast(`That is only ${text.length} characters. Paste the whole description.`);
    card.querySelector("textarea[data-jd]")?.focus();
    return;
  }
  if (jdMismatch(r, text) && D.warned !== text) {
    D.warned = text;
    renderFixtures();
    return;
  }
  btn.disabled = true; btn.textContent = "Queueing…";
  const res = await api("jd", { job_key: r.job_key }, text);
  if (!res.ok) { btn.disabled = false; btn.textContent = "Build CV"; return toast(res.error || "Could not start the build."); }
  D.warned = "";
  toast(`Building a CV for ${r.company}.`);
  await pollJD();
}

// ── fixture actions ────────────────────────────────────────────
document.addEventListener("input", (e) => {
  const ta = e.target.closest("textarea[data-jd]");
  if (!ta) return;
  const k = ta.closest(".fx").dataset.key, D = drawer(k);
  D.draft = ta.value;
  const n = ta.value.trim().length, cnt = ta.closest(".drawer").querySelector("[data-cnt]");
  if (cnt) cnt.textContent = n < JD_MIN ? n + " / " + JD_MIN + " characters" : n.toLocaleString() + " characters";
  if (D.warned) { D.warned = ""; ta.closest(".drawer").querySelector(".jdwarn")?.remove(); const b = ta.closest(".drawer").querySelector('[data-do="build"]'); if (b) b.textContent = "Build CV"; }
});
document.addEventListener("keydown", (e) => {
  const ta = e.target.closest?.("textarea[data-jd]");
  if (ta && (e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    ta.closest(".drawer").querySelector('[data-do="build"]')?.click();
  }
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-do]");
  if (!b) return;
  const card = b.closest(".fx");
  const key = card?.dataset.key || b.dataset.key;
  const r = S?.five.find((x) => x.job_key === key);
  const act = b.dataset.do;

  if (act === "jd" || act === "report") {
    const D = drawer(key);
    D.open = D.open === act ? null : act;
    renderFixtures();
    if (D.open === "jd") document.querySelector(`.fx[data-key="${CSS.escape(key)}"] textarea[data-jd]`)?.focus();
    return;
  }
  if (act === "close") { drawer(key).open = null; return renderFixtures(); }
  if (act === "sample") {
    drawer(key).draft = r.sample_jd; drawer(key).warned = "";
    renderFixtures();
    return;
  }
  if (act === "build") return submitJD(r, card, b);

  b.disabled = true;
  if (act === "sent") {
    card.appendChild($("stamp").content.cloneNode(true));
    const rc = b.getBoundingClientRect();
    confetti(rc.left + rc.width / 2, rc.top);
    await api("mark", { job_key: key, status: "applied" });
    const hit = S.sent_week + 1 === S.target;
    setTimeout(() => card.classList.add("gone"), 650);
    setTimeout(async () => {
      await load();
      if (hit) { confetti(innerWidth / 2, innerHeight / 3, 180); toast(`${S.target} sent. Target hit for the week.`); }
      else toast("Sent. " + Math.max(0, S.target - S.sent_week) + " to go this week.");
    }, 1150);
    return;
  }
  if (act === "swap" || act === "drop") {
    card.classList.add("gone");
    await (act === "swap" ? api("pick", { job_key: key, pick: "skipped" })
                          : api("mark", { job_key: key, status: "not-interested" }));
    toast(act === "swap" ? "Subbed off. The next one is coming on." : "Dropped. It is under Scouting, Shelved, if you change your mind.");
    setTimeout(load, 500);
    return;
  }
  if (act === "pin") {
    const res = await api("pick", { job_key: key, pick: "pinned" });
    toast(res.ok ? "Added to this week's fixtures." : res.error || "Could not pick that.");
    searchFixtures(); loadMarket(); await load();
  }
});

// ── pick your own ──────────────────────────────────────────────
$("sign-toggle").onclick = () => {
  $("sign").hidden = !$("sign").hidden;
  $("sign-toggle").textContent = $("sign").hidden ? "+ Pick your own" : "Close";
  if (!$("sign").hidden) $("sq").focus();
};
let sqT;
async function searchFixtures() {
  const q = $("sq").value.trim();
  if (q.length < 2) { $("sq-hits").innerHTML = ""; return; }
  const { rows } = await get("roles?view=all&q=" + encodeURIComponent(q));
  $("sq-hits").innerHTML = rows.filter((r) => !["applied", "interviewing", "offer"].includes(r.status)).slice(0, 10).map((r) =>
    `<div class="hit"><div class="m"><b>${esc(r.company)}</b> <span>${esc(r.title)}</span>
      <small>${r.score != null ? "fit " + r.score : "no score"}${r.deadline ? " · closes " + short(r.deadline) : ""}${r.closed ? " · closed" : ""}${r.status !== "surfaced" ? " · " + esc(r.status) : ""}</small></div>
      ${r.pick === "pinned" && r.status === "surfaced" ? `<span class="tag mine">picked</span>` : `<button class="btn sm" data-do="pin" data-key="${esc(r.job_key)}">Pick</button>`}</div>`).join("")
    || `<div class="hit"><div class="m"><small>Nothing tracked matches. Add it below.</small></div></div>`;
}
$("sq").oninput = () => { clearTimeout(sqT); sqT = setTimeout(searchFixtures, 180); };
$("n-add").onclick = async () => {
  const r = await api("add", { company: $("n-co").value, title: $("n-ti").value, url: $("n-url").value, deadline: $("n-dl").value });
  $("sign-msg").textContent = r.ok ? "Done: " + r.note + "." : r.error;
  if (r.ok) { ["n-co", "n-ti", "n-url", "n-dl"].forEach((i) => ($(i).value = "")); load(); }
};

// ── scouting tab ───────────────────────────────────────────────
let view = "open", rqT;
const STATUS_OPTS = [["surfaced", "Open"], ["applied", "Sent"], ["interviewing", "Interviewing"], ["offer", "Offer"],
  ["rejected", "Not progressed"], ["parked", "Parked"], ["not-interested", "Not interested"]];

async function loadMarket() {
  const q = $("rq").value.trim();
  const { rows } = await get(`roles?view=${view}&q=${encodeURIComponent(q)}`);
  $("scout-count").textContent = `${rows.length} role${rows.length === 1 ? "" : "s"}`;
  $("market").innerHTML = rows.slice(0, 240).map((r) => {
    const picked = r.pick === "pinned" && r.status === "surfaced";
    const st = r.status === "surfaced" ? (r.closed ? `<span class="st st-closed">closed</span>` : "")
      : `<span class="st st-${esc(r.status)}">${esc(STATUS_OPTS.find((o) => o[0] === r.status)?.[1] || r.status)}</span>`;
    const dl = r.deadline ? `<span class="tag ${!r.closed && daysTo(r.deadline) <= 7 ? "hot" : ""}">closes ${short(r.deadline)}</span>` : "";
    return `<div class="mk ${picked ? "picked" : ""}">
      <div class="mk-top"><div class="mk-co">${esc(r.company)}</div><div class="mk-fit ${(r.score ?? 0) < 75 ? "lo" : ""}">${r.score ?? "–"}</div></div>
      <div class="mk-role">${esc(r.title)}</div>
      ${r.reason ? `<div class="mk-why">${esc(r.reason)}</div>` : ""}
      <div class="mk-foot">${st}${dl}
        ${r.url ? `<a class="btn btn-ghost sm" href="${esc(r.url)}" target="_blank" rel="noopener">Posting</a>` : ""}
        ${r.cv ? `<a class="btn btn-ghost sm" href="/cv/${encodeURIComponent(r.cv)}" target="_blank" rel="noopener">CV</a>` : ""}
        ${r.status === "surfaced" && !r.closed ? (picked ? `<span class="tag mine">in fixtures</span>` : `<button class="btn sm" data-do="pin" data-key="${esc(r.job_key)}">Pick</button>`) : ""}
        <select data-key="${esc(r.job_key)}" aria-label="Status for ${esc(r.company)}">${STATUS_OPTS.map(([v, l]) => `<option value="${v}" ${v === r.status ? "selected" : ""}>${l}</option>`).join("")}</select>
      </div></div>`;
  }).join("") || `<div class="calm">Nothing here.</div>`;
}
$("seg").onclick = (e) => {
  const b = e.target.closest("button"); if (!b) return;
  view = b.dataset.view;
  document.querySelectorAll("#seg button").forEach((x) => x.classList.toggle("on", x === b));
  loadMarket();
};
$("rq").oninput = () => { clearTimeout(rqT); rqT = setTimeout(loadMarket, 180); };
$("market").addEventListener("change", async (e) => {
  const s = e.target.closest("select"); if (!s) return;
  await api("mark", { job_key: s.dataset.key, status: s.value });
  if (s.value === "applied") { const r = s.getBoundingClientRect(); confetti(r.left + r.width / 2, r.top, 40); }
  toast("Updated.");
  load(); loadMarket();
});

// ── tabs ───────────────────────────────────────────────────────
document.querySelectorAll(".tab[data-tab]").forEach((t) => t.onclick = () => {
  document.querySelectorAll(".tab[data-tab]").forEach((x) => { x.classList.toggle("on", x === t); x.setAttribute("aria-selected", x === t); });
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("on", p.id === "p-" + t.dataset.tab));
  if (t.dataset.tab === "scouting") loadMarket();
  if (t.dataset.tab === "season" && S) renderSeason();
  history.replaceState(null, "", "#" + t.dataset.tab);
});

load().then(() => {
  const start = location.hash.slice(1);
  if (start) document.querySelector(`.tab[data-tab="${start}"]`)?.click();
  return pollJD();
});
setInterval(() => { if (!document.hidden) load(); }, 120000);
