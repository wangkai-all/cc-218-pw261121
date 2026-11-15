/* 仓内作业分析 — 前端（原生 ES 模块，无构建步骤） */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const SVG_NS = "http://www.w3.org/2000/svg";

const state = {
  meta: null,
  scenarioId: "day1",
  data: null,
  params: {},
  defaults: {},
  window: { start: 0, end: 5400 },
  scrub: 0,
  filters: { zone: "all", picker: "all", showObs: true, showPoss: true, showUnk: true },
  conflictCause: "all",
  replay: null,
  replayTime: 0,
  playTimer: null,
  replayTimer: null,
  imported: null,
  view: "floor",
};

const PARAM_CONFIG = [
  { key: "walk_speed_mps", min: 0.6, max: 1.8, step: 0.05, unit: "m/s",
    hint: "决定两次扫码之间最短步行时间，进而影响可能区间宽度。" },
  { key: "dwell_min_s", min: 10, max: 60, step: 1, unit: "秒",
    hint: "每次扫码至少视为驻留的时长，是驻留区间的下限。" },
  { key: "repeat_scan_window_s", min: 2, max: 30, step: 1, unit: "秒",
    hint: "同人同货位在此窗口内的重复上报会被合并。" },
  { key: "same_loc_dwell_s", min: 30, max: 300, step: 10, unit: "秒",
    hint: "同货位连续扫码时，最多可确证多久的驻留。" },
  { key: "long_gap_s", min: 180, max: 1800, step: 30, unit: "秒",
    hint: "超过此次数无扫描就判为位置未知，而不是继续算在最后货位。" },
  { key: "walk_slack", min: 1.0, max: 2.0, step: 0.05, unit: "×",
    hint: "放宽行走时间的估计；越大，可能区间越窄、判定越保守。" },
  { key: "gap_alert_s", min: 30, max: 600, step: 10, unit: "秒",
    hint: "比预计步行多出多久就标记为扫描缺口。" },
];

const ZONE_COLOR = { fast: "--series-1", cold: "--series-2", staging: "--series-3", dock: "--series-neutral", main: "--series-neutral", unknown: "--series-neutral" };
const ZONE_NAME = { fast: "快拣区", cold: "冷链区", staging: "复核区", dock: "月台", main: "主通道", unknown: "未知" };
const CAUSE_TEXT = { task_cross: "任务分配交叉", data_gap: "扫描数据缺失", possible: "推测区间重叠", topology: "拓扑改版" };

/* ------------------------------------------------------------------ */
/* 基础工具                                                           */
/* ------------------------------------------------------------------ */

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else if (k === "html") el.innerHTML = v; // 仅用于固定文案，绝不用于数据
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function fmtTime(epoch) {
  if (epoch === null || epoch === undefined) return "—";
  const d = new Date(epoch * 1000);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  const ss = String(d.getUTCSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}
function fmtHM(epoch) {
  return fmtTime(epoch).slice(0, 5);
}
function fmtDur(sec) {
  if (sec === null || sec === undefined) return "—";
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return `${m} 分 ${String(s).padStart(2, "0")} 秒`;
  const hh = Math.floor(m / 60);
  return `${hh} 小时 ${String(m % 60).padStart(2, "0")} 分`;
}
function fmtDurSigned(sec) {
  if (sec === null || sec === undefined) return "—";
  const sign = sec < 0 ? "−" : sec > 0 ? "+" : "";
  return `${sign}${fmtDur(Math.abs(sec))}`;
}
function fmtNum(n, digits = 0) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function pct(n) { return `${Math.round((n || 0) * 100)}%`; }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function hashOffset(id) {
  let hsh = 0;
  for (let i = 0; i < id.length; i++) hsh = (hsh * 31 + id.charCodeAt(i)) >>> 0;
  const angle = (hsh % 360) * Math.PI / 180;
  const radius = 1.1 + ((hsh >>> 9) % 100) / 100 * 1.2;
  return { dx: Math.cos(angle) * radius, dy: Math.sin(angle) * radius };
}

function toast(msg, ms = 2600) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `请求失败 (${res.status})`);
  return body;
}

function paramQuery() {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(state.params)) q.set(k, String(v));
  return q;
}

/** 顶栏高度随宽度换行变化，筛选条吸顶偏移必须跟着走，否则会盖住顶栏。 */
function syncStickyOffset() {
  const bar = $(".topbar");
  if (!bar) return;
  const h = bar.getBoundingClientRect().height;
  document.documentElement.style.setProperty("--topbar-h", `${h}px`);
}

/* ------------------------------------------------------------------ */
/* 初始化                                                              */
/* ------------------------------------------------------------------ */

async function boot() {
  setTheme(localStorage.getItem("wh-theme") || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  syncStickyOffset();
  window.addEventListener("resize", syncStickyOffset);
  window.addEventListener("orientationchange", syncStickyOffset);
  window.addEventListener("load", syncStickyOffset);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(syncStickyOffset);
  bindChrome();
  try {
    const meta = await api("/api/meta");
    state.meta = meta;
    state.defaults = { ...meta.defaults };
    state.params = { ...meta.defaults };
    fillScenarioSelect(meta);
    buildParamFields(meta);
    await loadScenario(state.scenarioId, true);
  } catch (err) {
    toast(`初始化失败：${err.message}`, 6000);
    console.error(err);
  }
}

function bindChrome() {
  $$(".mode-tab").forEach((tab) => {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
  });
  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    setTheme(next);
    localStorage.setItem("wh-theme", next);
    if (state.data) { renderFloorMap(); }
    if (state.replay) { renderReplayMaps(); }
  });
  $("#params-btn").addEventListener("click", () => toggleParams(true));
  $("#params-close").addEventListener("click", () => toggleParams(false));
  $("#params-reset").addEventListener("click", () => {
    state.params = { ...state.defaults };
    syncParamInputs();
  });
  $("#params-apply").addEventListener("click", async () => {
    toggleParams(false);
    await loadScenario(state.scenarioId, false);
    if (state.replay) await loadReplay();
    toast("已按新参数重算");
  });
  $("#apply-speed").addEventListener("click", () => {
    const speed = Number($("#apply-speed").dataset.speed);
    if (!speed) return;
    state.params.walk_speed_mps = speed;
    syncParamInputs();
    $("#apply-speed").hidden = true;
    toast(`速度已设为 ${speed.toFixed(2)} m/s，点“重新计算”生效`);
  });
  $("#scenario-select").addEventListener("change", (e) => {
    loadScenario(e.target.value, true);
  });
  $("#window-start").addEventListener("input", onWindowChange);
  $("#window-end").addEventListener("input", onWindowChange);
  $("#time-scrub").addEventListener("input", (e) => {
    const base = state.data ? state.data.scenario.start : 0;
    state.scrub = base + Number(e.target.value);
    updateScrub(false);
  });
  $("#play-toggle").addEventListener("click", togglePlay);
  $("#zone-filter").addEventListener("change", (e) => {
    state.filters.zone = e.target.value;
    renderFloor();
  });
  $("#picker-filter").addEventListener("change", (e) => {
    state.filters.picker = e.target.value;
    renderFloor();
  });
  for (const [id, key] of [["show-observer", "showObs"], ["show-possible", "showPoss"], ["show-unknown", "showUnk"]]) {
    $(`#${id}`).addEventListener("change", (e) => {
      state.filters[key] = e.target.checked;
      renderFloor();
    });
  }
  $("#reset-filters").addEventListener("click", () => {
    state.filters = { zone: "all", picker: "all", showObs: true, showPoss: true, showUnk: true };
    $("#zone-filter").value = "all";
    $("#picker-filter").value = "all";
    $("#show-observer").checked = true;
    $("#show-possible").checked = true;
    $("#show-unknown").checked = true;
    if (!state.data) return;
    const base = state.data.scenario.start;
    const span = state.data.scenario.end - state.data.scenario.start;
    state.window = { start: base, end: base + span };
    state.scrub = base;
    $("#window-start").value = "0";
    $("#window-end").value = String(span);
    onWindowChange();
  });
  $("#replay-play").addEventListener("click", toggleReplayPlay);
  $("#replay-scrub").addEventListener("input", (e) => {
    state.replayTime = (state.replay ? state.replay.runs.a.start : 0) + Number(e.target.value);
    updateReplayMaps(false);
  });
  $$(".segmented [data-cause]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.conflictCause = btn.dataset.cause;
      $$(".segmented [data-cause]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
      renderConflictList();
    });
  });
  $$(".link-btn[data-table]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const host = $(`#table-${btn.dataset.table}`);
      const open = host.hidden;
      host.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      if (open) renderTable(btn.dataset.table, host);
    });
  });
  $("#import-form").addEventListener("submit", onImport);
  $("#import-sample").addEventListener("click", async () => {
    try {
      const [topology, scans, tasks] = await Promise.all([
        fetch("/data/csv/topology.json").then((r) => r.text()),
        fetch("/data/csv/scans.csv").then((r) => r.text()),
        fetch("/data/csv/tasks.csv").then((r) => r.text()),
      ]);
      const result = await api("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topology, scans, tasks, label: "内置样例 day1", params: state.params }),
      });
      state.imported = result;
      renderImportResult(result);
      toast("已用内置样例完成导入解析");
    } catch (e) {
      const err = $("#import-error");
      err.textContent = `样例载入失败：${e.message}`;
      err.hidden = false;
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") toggleParams(false);
    if (e.target.matches("input, select, textarea")) return;
    if (e.key === " " && state.view === "floor") { e.preventDefault(); togglePlay(); }
  });
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#theme-toggle").textContent = theme === "dark" ? "亮色" : "暗色";
}

function switchView(view) {
  state.view = view;
  $$(".mode-tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.view === view)));
  for (const name of ["floor", "conflicts", "replay", "import"]) {
    $(`#view-${name}`).hidden = name !== view;
  }
  stopPlay();
  stopReplayPlay();
  if (view === "replay" && !state.replay) loadReplay();
  if (view === "floor") renderFloorMap();
  if (view === "conflicts") { renderCapacityCharts(); renderCauseBars(); renderConflictList(); }
  if (view === "replay" && state.replay) { renderReplayMaps(); }
}

function toggleParams(open) {
  $("#params-panel").hidden = !open;
  $("#params-btn").setAttribute("aria-expanded", String(open));
  if (open && state.data) {
    renderSpeedAdvice(state.data.speed_estimate);
    renderCalibration(state.data.calibration);
  }
}

/* ------------------------------------------------------------------ */
/* 场景加载                                                            */
/* ------------------------------------------------------------------ */

function fillScenarioSelect(meta) {
  const sel = $("#scenario-select");
  sel.textContent = "";
  for (const sc of meta.scenarios) {
    sel.append(h("option", { value: sc.id }, `${sc.label} · ${sc.scans} 扫描 / ${sc.tasks} 单`));
  }
  sel.value = state.scenarioId;
}

async function loadScenario(id, resetWindow) {
  state.scenarioId = id;
  $("#view-floor").classList.add("loading");
  try {
    const q = paramQuery();
    q.set("id", id);
    const data = await api(`/api/scenario?${q.toString()}`);
    state.data = data;
    if (resetWindow) {
      state.window = { start: data.scenario.start, end: data.scenario.end };
      state.scrub = data.scenario.start;
      $("#window-start").min = "0";
      $("#window-start").max = String(data.scenario.end - data.scenario.start);
      $("#window-start").value = "0";
      $("#window-end").min = "0";
      $("#window-end").max = String(data.scenario.end - data.scenario.start);
      $("#window-end").value = String(data.scenario.end - data.scenario.start);
      $("#time-scrub").max = String(data.scenario.end - data.scenario.start);
      $("#time-scrub").value = "0";
    }
    fillZonePicker(data);
    syncWindowFields();
    renderAll();
    renderSpeedAdvice(data.speed_estimate);
    renderCalibration(data.calibration);
  } catch (err) {
    toast(`加载失败：${err.message}`, 5000);
    console.error(err);
  } finally {
    $("#view-floor").classList.remove("loading");
  }
}

function fillZonePicker(data) {
  const zones = new Set();
  for (const ent of data.topology.entities) zones.add(ent.zone);
  const sel = $("#zone-filter");
  const prev = sel.value || "all";
  sel.textContent = "";
  sel.append(h("option", { value: "all" }, "全部区域"));
  for (const z of zones) {
    if (!z || z === "unknown") continue;
    sel.append(h("option", { value: z }, ZONE_NAME[z] || z));
  }
  sel.value = [...zones].includes(prev) || prev === "all" ? prev : "all";
  state.filters.zone = sel.value;

  const pickerSel = $("#picker-filter");
  const prevP = pickerSel.value || "all";
  pickerSel.textContent = "";
  pickerSel.append(h("option", { value: "all" }, "全部人员"));
  for (const p of data.scenario.pickers) pickerSel.append(h("option", { value: p }, p));
  pickerSel.value = data.scenario.pickers.includes(prevP) ? prevP : "all";
  state.filters.picker = pickerSel.value;
}

function onWindowChange() {
  if (!state.data) return;
  const max = state.data.scenario.end - state.data.scenario.start;
  let s = Number($("#window-start").value);
  let e = Number($("#window-end").value);
  if (s > e - 60) {
    if (document.activeElement === $("#window-start")) { s = Math.max(0, e - 60); $("#window-start").value = s; }
    else { e = Math.min(max, s + 60); $("#window-end").value = e; }
  }
  state.window = { start: state.data.scenario.start + s, end: state.data.scenario.start + e };
  state.scrub = clamp(state.scrub, state.window.start, state.window.end);
  syncWindowFields();
  renderFloor();
  renderConflicts();
}

function syncWindowFields() {
  const base = state.data ? state.data.scenario.start : 0;
  const max = state.data ? state.data.scenario.end - state.data.scenario.start : 5400;
  const s = state.window.start - base;
  const e = state.window.end - base;
  $("#window-start").value = String(Math.round(s));
  $("#window-end").value = String(Math.round(e));
  $("#window-label").textContent = `${fmtTime(state.window.start)} – ${fmtTime(state.window.end)}`;
  $("#time-scrub").value = String(Math.round(state.scrub - base));
  $("#scrub-label").textContent = fmtTime(state.scrub);
  $("#replay-scrub").max = String(max);
}

function renderAll() {
  renderFloor();
  renderConflicts();
  if (state.view === "conflicts") renderCapacityCharts();
  if (state.view === "floor") renderFloorMap();
}

/* ------------------------------------------------------------------ */
/* 区域还原：KPI                                                       */
/* ------------------------------------------------------------------ */

function windowSegments() {
  const { start, end } = state.window;
  return (state.data?.segments || []).filter((s) => s.end > start && s.start < end);
}

function renderFloor() {
  if (!state.data) return;
  renderFloorKpis();
  renderZoneBars();
  renderPickerTimelines();
  renderQuality();
  renderTopologyInfo();
  renderFloorMap();
}

function renderFloorKpis() {
  const host = $("#floor-kpis");
  host.textContent = "";
  const d = state.data;
  const sum = d.summary;
  const segs = windowSegments();
  const obsSeconds = segs.filter((s) => s.kind === "dwell").reduce((a, s) => a + s.duration, 0);
  const possSeconds = segs.filter((s) => s.kind === "possible").reduce((a, s) => a + s.duration, 0);
  const unknownSeconds = segs.filter((s) => s.entity === "UNKNOWN").reduce((a, s) => a + s.duration, 0);
  const people = new Set(segs.filter((s) => s.kind !== "unknown" && s.entity !== "UNKNOWN").map((s) => s.picker)).size;
  const conflicted = sum.conflict_count;
  const unknownShare = obsSeconds + possSeconds + unknownSeconds > 0
    ? unknownSeconds / (obsSeconds + possSeconds + unknownSeconds) : 0;

  const tiles = [
    { label: "窗口内扫描", value: fmtNum(sum.scans), unit: "次", sub: `合并重复 ${sum.merged_duplicates} 条` },
    { label: "在场人员", value: fmtNum(people), unit: "人", sub: `窗口 ${fmtHM(state.window.start)}–${fmtHM(state.window.end)}` },
    { label: "确切驻留", value: fmtDur(obsSeconds), sub: `占观测/推测合计 ${pct(obsSeconds / Math.max(1, obsSeconds + possSeconds))}` },
    { label: "可能区间", value: fmtDur(possSeconds), sub: "扫码间隔内无法确定具体货位" },
    { label: "位置未知", value: fmtDur(unknownSeconds), sub: `占本窗口 ${pct(unknownShare)}，不计入最后货位` },
    { label: "超容量区间", value: fmtNum(conflicted), unit: "段", sub: `确证 ${sum.confirmed_conflicts} · 推测 ${sum.possible_conflicts}` },
  ];
  for (const t of tiles) {
    host.append(h("div", { class: "kpi" },
      h("span", { class: "kpi-label" }, t.label),
      h("span", { class: "kpi-value" }, t.value, t.unit ? h("small", {}, t.unit) : null),
      h("span", { class: "kpi-sub" }, t.sub),
    ));
  }
}

/* ------------------------------------------------------------------ */
/* 区域还原：平面图                                                    */
/* ------------------------------------------------------------------ */

const MAP_BOX = { x: 0.5, y: 4.5, w: 93, h: 54 };
const UNKNOWN_STRIP = { y: 60.5, h: 5.0 };

function entityAt(seg, topology) {
  if (!seg || !seg.entity || seg.entity === "UNKNOWN") return null;
  return topology.entities.find((e) => e.id === seg.entity) || null;
}

function locationPoint(topology, seg) {
  let loc = topology.locations.find((l) => l.id === seg.location);
  if (!loc && seg.location && topology.aliases && topology.aliases[seg.location]) {
    const target = topology.aliases[seg.location];
    loc = topology.locations.find((l) => l.id === target);
  }
  if (loc) return { x: loc.x, y: loc.y, located: true };
  const ent = entityAt(seg, topology);
  if (ent) return { x: ent.rect.x + ent.rect.w / 2, y: ent.rect.y + ent.rect.h / 2, located: false };
  return null;
}

function activeSegmentsAt(time) {
  const byPicker = new Map();
  for (const seg of state.data.segments) {
    if (seg.start <= time && time < seg.end) {
      if (!byPicker.has(seg.picker)) byPicker.set(seg.picker, []);
      byPicker.get(seg.picker).push(seg);
    }
  }
  return byPicker;
}

function renderFloorMap() {
  if (!state.data) return;
  const host = $("#floor-map");
  host.textContent = "";
  const topology = state.data.topology;
  const time = state.scrub;
  const zoneFilter = state.filters.zone;
  const pickerFilter = state.filters.picker;
  const active = activeSegmentsAt(time);

  const defs = svg("defs");
  const hatch = svg("pattern", { id: "hatch-possible", width: 3, height: 3, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
    svg("rect", { width: 3, height: 3, fill: "transparent" }),
    svg("line", { x1: 0, y1: 0, x2: 0, y2: 3, stroke: "var(--text-muted)", "stroke-width": 0.55, opacity: 0.55 }),
  );
  defs.append(hatch);
  host.append(defs);

  // 区域底色
  const zones = [...new Set(topology.entities.map((e) => e.zone))];
  for (const zone of zones) {
    if (!ZONE_NAME[zone]) continue;
    const ents = topology.entities.filter((e) => e.zone === zone);
    const x0 = Math.min(...ents.map((e) => e.rect.x)) - 2;
    const x1 = Math.max(...ents.map((e) => e.rect.x + e.rect.w)) + 2;
    const y0 = Math.min(...ents.map((e) => e.rect.y)) - 3.4;
    const y1 = Math.max(...ents.map((e) => e.rect.y + e.rect.h)) + 2.4;
    host.append(svg("rect", {
      x: x0, y: y0, width: x1 - x0, height: y1 - y0, rx: 2,
      fill: `var(${ZONE_COLOR[zone] || "--series-neutral"})`, opacity: zone === "staging" ? 0.05 : 0.035,
      stroke: "var(--grid)", "stroke-width": 0.3,
    }));
    if (zone === "fast" || zone === "cold") {
      host.append(svg("text", { x: x0 + 0.8, y: y0 - 1.1, class: "zone-wash-label" }, ZONE_NAME[zone]));
    }
  }

  // 步道（弱化）
  for (const e of topology.edges) {
    const a = topology.nodes.find((n) => n.id === e.a);
    const b = topology.nodes.find((n) => n.id === e.b);
    if (!a || !b) continue;
    host.append(svg("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: "edge-line" }));
  }

  // 实体
  const counts = currentEntityCounts(active);
  const entityLabels = [];
  for (const ent of topology.entities) {
    const dim = zoneFilter !== "all" && ent.zone !== zoneFilter;
    const cls = `entity-rect zone-${ent.zone}` + (counts.get(ent.id)?.observed > ent.capacity ? " over-cap" : "");
    const rect = svg("rect", {
      x: ent.rect.x, y: ent.rect.y, width: ent.rect.w, height: ent.rect.h, rx: 1.2, class: cls,
      opacity: dim ? 0.35 : 1,
      tabindex: 0, role: "img",
      "aria-label": `${ent.name}，容量 ${ent.capacity}`,
    });
    const info = counts.get(ent.id) || { observed: 0, possible: 0 };
    rect.addEventListener("pointerenter", (ev) => showMapTooltip(ev, ent, info));
    rect.addEventListener("pointermove", (ev) => moveMapTooltip(ev));
    rect.addEventListener("pointerleave", hideMapTooltip);
    host.append(rect);
    entityLabels.push(svg("text", {
      x: ent.rect.x + ent.rect.w / 2, y: ent.rect.y + 2.9, class: "entity-label", "text-anchor": "middle",
      opacity: dim ? 0.5 : 1,
    }, ent.name.replace("（脚手架限行）", "").replace("（冷链扩容）", "")));
    entityLabels.push(svg("text", {
      x: ent.rect.x + ent.rect.w / 2, y: ent.rect.y + ent.rect.h - 0.9, class: "entity-sub", "text-anchor": "middle",
      opacity: dim ? 0.5 : 1,
    }, `容量 ${ent.capacity}${info.observed || info.possible ? ` · 当前 ${info.observed}${info.possible ? `+${info.possible}?` : ""}` : ""}`));
  }

  // 人员
  const unknownList = [];
  for (const [picker, segs] of active.entries()) {
    if (pickerFilter !== "all" && picker !== pickerFilter) continue;
    const visible = segs.filter((seg) => {
      if (seg.kind === "dwell" && !state.filters.showObs) return false;
      if ((seg.kind === "possible" || seg.kind === "gap") && !state.filters.showPoss) return false;
      if (seg.kind === "unknown" && !state.filters.showUnk) return false;
      return true;
    });
    if (!visible.length) continue;
    const hasUnknown = visible.some((s) => s.kind === "unknown" || s.entity === "UNKNOWN");
    if (hasUnknown && visible.every((s) => s.kind === "unknown" || s.entity === "UNKNOWN")) {
      unknownList.push(picker);
      continue;
    }
    for (const seg of visible) {
      if (seg.entity === "UNKNOWN") continue;
      if (zoneFilter !== "all") {
        const ent = entityAt(seg, topology);
        if (ent && ent.zone !== zoneFilter) continue;
      }
      const pt = locationPoint(topology, seg);
      if (!pt) continue;
      const off = hashOffset(picker + seg.kind);
      const isObs = seg.kind === "dwell";
      const g = svg("g", {
        class: `person-dot ${isObs ? "observed" : seg.kind === "unknown" ? "unknown" : "possible"}`,
        transform: `translate(${(pt.x + off.dx).toFixed(2)} ${(pt.y + off.dy).toFixed(2)})`,
        tabindex: 0, role: "button",
        "aria-label": `${picker} 在 ${seg.entity}，${seg.kind_label}，任务 ${seg.task || "无"}`,
      });
      g.append(svg("circle", { r: 2.0, fill: "transparent" }));
      g.append(svg("circle", {
        r: 1.05, class: "core",
        fill: isObs ? `var(${ZONE_COLOR[seg.zone] || "--series-1"})` : "var(--surface-0)",
        stroke: seg.kind === "unknown" ? "var(--text-muted)" : `var(${ZONE_COLOR[seg.zone] || "--series-1"})`,
      }));
      g.addEventListener("pointerenter", (ev) => showSegTooltip(ev, seg));
      g.addEventListener("pointermove", moveMapTooltip);
      g.addEventListener("pointerleave", hideMapTooltip);
      g.addEventListener("click", () => {
        $("#picker-filter").value = picker;
        state.filters.picker = picker;
        renderFloor();
      });
      g.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { $("#picker-filter").value = picker; state.filters.picker = picker; renderFloor(); }
      });
      host.append(g);
    }
  }

  // 标签置顶，避免被人员点遮挡
  for (const label of entityLabels) host.append(label);

  // 位置未知带
  host.append(svg("rect", {
    x: MAP_BOX.x + 1, y: UNKNOWN_STRIP.y, width: MAP_BOX.w - 2, height: UNKNOWN_STRIP.h,
    rx: 1.5, fill: "var(--wash-neutral)", stroke: "var(--grid)", "stroke-width": 0.3,
  }));
  host.append(svg("text", {
    x: MAP_BOX.x + 3, y: UNKNOWN_STRIP.y + 2.3, class: "entity-sub", "text-anchor": "start",
  }, "位置未知（无扫描 / 休息 / 设备离线）"));
  if (!unknownList.length) {
    host.append(svg("text", {
      x: MAP_BOX.x + 3, y: UNKNOWN_STRIP.y + 4.8, class: "entity-sub", fill: "var(--text-muted)",
    }, "此刻没有人员处于未知状态"));
  }
  unknownList.forEach((picker, i) => {
    // 未知带最多可能同时有 6~8 人；贴左侧紧凑排布，避免末端越过图框，
    // 圆心也压在下沿之上，防止文字被 viewBox 裁掉一半。
    const gx = MAP_BOX.x + 4 + i * 8.5;
    const g = svg("g", { class: "person-dot unknown", transform: `translate(${gx} ${UNKNOWN_STRIP.y + 3.9})`, tabindex: 0, role: "button", "aria-label": `${picker} 位置未知` });
    g.append(svg("circle", { r: 1.9, fill: "transparent" }));
    g.append(svg("circle", { r: 0.95, class: "core", fill: "var(--surface-0)", stroke: "var(--text-muted)" }));
    g.append(svg("text", { x: 1.4, y: 0, "text-anchor": "start" }, picker));
    const seg = active.get(picker)?.find((s) => s.kind === "unknown");
    g.addEventListener("pointerenter", (ev) => showSegTooltip(ev, seg || { kind_label: "位置未知", note: "", picker }));
    g.addEventListener("pointermove", moveMapTooltip);
    g.addEventListener("pointerleave", hideMapTooltip);
    host.append(g);
  });

  $("#map-sub").textContent = `${fmtTime(time)} · ${topology.label} · 方框括号内为通道容量，虚线为推测位置`;
  renderMapLegend();
}

function currentEntityCounts(active) {
  const map = new Map();
  for (const segs of active.values()) {
    const obs = new Set(), poss = new Set();
    for (const s of segs) {
      if (!s.entity || s.entity === "UNKNOWN") continue;
      if (s.kind === "dwell") obs.add(s.picker);
      else if (s.kind === "possible" || s.kind === "gap") poss.add(s.picker);
    }
    for (const ent of new Set([...obs, ...poss])) {
      const rec = map.get(ent) || { observed: 0, possible: 0 };
      if (obs.has(ent)) rec.observed += 1;
      else rec.possible += 1;
      map.set(ent, rec);
    }
  }
  return map;
}

function showMapTooltip(ev, ent, info) {
  const el = $("#map-tooltip");
  el.textContent = "";
  el.append(
    h("strong", {}, ent.name),
    h("div", { class: "mono" }, `容量 ${ent.capacity} · 观测 ${info.observed} · 推测 ${info.possible}`),
    h("div", { style: { color: "var(--text-muted)", fontSize: "11px" } },
      info.observed > ent.capacity ? "当前观测人数已超容量" : "当前未超容量"),
  );
  el.hidden = false;
  moveMapTooltip(ev);
}

function showSegTooltip(ev, seg) {
  const el = $("#map-tooltip");
  el.textContent = "";
  el.append(
    h("strong", {}, `${seg.picker} · ${seg.kind_label || "推测"}`),
    h("div", { style: { fontSize: "11.5px", color: "var(--text-secondary)" } },
      `${ZONE_NAME[seg.zone] || seg.zone || "未知"} · ${seg.location || seg.entity || "—"}`),
    h("div", { class: "mono", style: { fontSize: "11px" } }, `${fmtTime(seg.start)} – ${fmtTime(seg.end)} · ${fmtDur(seg.duration)}`),
    seg.task ? h("div", { class: "mono", style: { fontSize: "11px" } }, `任务 ${seg.task}`) : null,
    seg.note ? h("div", { style: { fontSize: "11px", color: "var(--text-muted)", maxWidth: "230px" } }, seg.note) : null,
  );
  el.hidden = false;
  moveMapTooltip(ev);
}

function moveMapTooltip(ev) {
  const el = $("#map-tooltip");
  if (el.hidden) return;
  const wrap = $(".map-wrap").getBoundingClientRect();
  const x = ev.clientX - wrap.left;
  const y = ev.clientY - wrap.top;
  el.style.left = `${clamp(x + 14, 4, wrap.width - 250)}px`;
  el.style.top = `${clamp(y + 12, 4, wrap.height - 100)}px`;
}

function hideMapTooltip() { $("#map-tooltip").hidden = true; }

function renderMapLegend() {
  const host = $("#map-legend");
  host.textContent = "";
  const items = [
    ["确切驻留", "observed"],
    ["可能位置（空心）", "possible"],
    ["位置未知", "unknown"],
  ];
  for (const [label, cls] of items) {
    host.append(h("span", { class: "item" },
      h("span", { class: `swatch round ${cls === "observed" ? "" : "dashed"}`,
        style: { background: cls === "observed" ? "var(--series-1)" : "transparent",
          borderColor: cls === "observed" ? "var(--series-1)" : "var(--text-muted)" } }),
      label));
  }
  host.append(h("span", { class: "item" },
    h("span", { class: "swatch", style: { borderColor: "var(--status-critical)", background: "transparent" } }),
    "超容量"));
}

/* ------------------------------------------------------------------ */
/* 区域还原：区域占用条                                                */
/* ------------------------------------------------------------------ */

function renderZoneBars() {
  const host = $("#zone-bars");
  host.textContent = "";
  const topology = state.data.topology;
  const buckets = state.data.zone_occupancy.buckets;
  if (!buckets.length) { host.append(h("p", { class: "card-sub" }, "窗口内没有分桶数据")); return; }
  const entities = topology.entities.filter((e) => state.filters.zone === "all" || e.zone === state.filters.zone);
  const maxCount = Math.max(2, ...buckets.flatMap((b) => Object.values(b.entities).map((v) => v.observed + v.possible)));
  const base = state.data.scenario.start;

  for (const ent of entities) {
    const row = h("div", { class: "zone-row" });
    row.append(h("span", { class: "zone-name", title: ent.name }, ent.name));
    const track = h("div", { class: "zone-track" });
    const n = buckets.length;
    buckets.forEach((b, i) => {
      const v = b.entities[ent.id] || { observed: 0, possible: 0 };
      if (!v.observed && !v.possible) return;
      const left = (i / n) * 100;
      const width = Math.max(100 / n - 0.35, 0.6);
      if (v.observed) {
        const intensity = 0.28 + 0.62 * (v.observed / maxCount);
        const seg = h("div", {
          class: "zone-seg observed",
          style: { left: `${left}%`, width: `${width}%`, opacity: String(intensity) },
          title: `${fmtHM(b.t)} ${ent.name}：观测 ${v.observed} 人${v.possible ? `，另有 ${v.possible} 人可能` : ""}${v.observed > ent.capacity ? "（超容量）" : ""}`,
        });
        if (v.observed > ent.capacity) {
          seg.style.outline = "1px solid var(--status-critical)";
          seg.style.outlineOffset = "-1px";
        }
        track.append(seg);
      } else if (v.possible) {
        track.append(h("div", {
          class: "zone-seg possible",
          style: { left: `${left}%`, width: `${width}%` },
          title: `${fmtHM(b.t)} ${ent.name}：约 ${v.possible} 人可能在此（无确证扫码）`,
        }));
      }
    });
    const peak = Math.max(...buckets.map((b) => {
      const v = b.entities[ent.id] || { observed: 0, possible: 0 };
      return v.observed + v.possible;
    }));
    row.append(track, h("span", { class: "zone-val" }, `峰 ${peak} / 容 ${ent.capacity}`));
    host.append(row);
  }
  if (!entities.length) host.append(h("p", { class: "card-sub" }, "当前区域筛选下没有通道实体"));
  host.append(h("div", { class: "tl-axis" },
    h("span", {}, fmtHM(state.window.start)),
    h("span", {}, fmtHM((state.window.start + state.window.end) / 2)),
    h("span", {}, fmtHM(state.window.end))));
}

/* ------------------------------------------------------------------ */
/* 区域还原：人员时间线                                                */
/* ------------------------------------------------------------------ */

function renderPickerTimelines() {
  const host = $("#picker-timelines");
  host.textContent = "";
  const { start, end } = state.window;
  const span = Math.max(1, end - start);
  const segs = windowSegments().filter((s) => state.filters.picker === "all" || s.picker === state.filters.picker);
  const byPicker = new Map();
  for (const s of segs) {
    if (!byPicker.has(s.picker)) byPicker.set(s.picker, []);
    byPicker.get(s.picker).push(s);
  }
  const pickers = [...byPicker.keys()].sort();
  if (!pickers.length) {
    host.append(h("p", { class: "card-sub" }, "窗口内没有人员记录"));
    return;
  }
  for (const picker of pickers) {
    const row = h("div", { class: "tl-row" });
    row.append(h("span", { class: "tl-name" }, picker));
    const track = h("div", { class: "tl-track" });
    for (const s of byPicker.get(picker)) {
      if (s.entity === "UNKNOWN" && !state.filters.showUnk) continue;
      if (s.kind === "dwell" && !state.filters.showObs) continue;
      if ((s.kind === "possible" || s.kind === "gap") && !state.filters.showPoss) continue;
      const left = ((Math.max(s.start, start) - start) / span) * 100;
      const width = Math.max(((Math.min(s.end, end) - Math.max(s.start, start)) / span) * 100, 0.25);
      const cls = s.kind === "dwell" ? "dwell" : s.kind === "unknown" ? "unknown" : s.kind === "gap" ? "gap" : "possible";
      track.append(h("div", {
        class: `tl-seg ${cls}`,
        style: { left: `${left}%`, width: `${width}%` },
        title: `${picker} · ${s.kind_label}\n${ZONE_NAME[s.zone] || s.zone} ${s.location || s.entity}\n${fmtTime(s.start)} – ${fmtTime(s.end)}${s.task ? `\n任务 ${s.task}` : ""}${s.note ? `\n${s.note}` : ""}`,
      }));
    }
    if (state.scrub >= start && state.scrub <= end) {
      track.append(h("div", { class: "tl-now", style: { left: `${((state.scrub - start) / span) * 100}%` } }));
    }
    row.append(track);
    host.append(row);
  }
  host.append(h("div", { class: "tl-axis" },
    h("span", {}, fmtHM(start)), h("span", {}, fmtHM((start + end) / 2)), h("span", {}, fmtHM(end))));
  const legend = $("#timeline-legend");
  legend.textContent = "";
  for (const [label, cls, style] of [
    ["确切驻留", "dwell", { background: "var(--series-1)" }],
    ["可能区间", "possible", { background: "repeating-linear-gradient(135deg, var(--wash-1) 0 3px, transparent 3px 6px)", border: "1px solid var(--wash-1)" }],
    ["扫描缺口", "gap", { background: "repeating-linear-gradient(135deg, var(--wash-2) 0 3px, transparent 3px 6px)" }],
    ["位置未知", "unknown", { background: "var(--wash-neutral)" }],
  ]) {
    legend.append(h("span", { class: "item" }, h("span", { class: "swatch", style }), label));
  }
}

/* ------------------------------------------------------------------ */
/* 区域还原：数据质量                                                  */
/* ------------------------------------------------------------------ */

function renderTopologyInfo() {
  const host = $("#topology-info");
  if (!host || !state.data) return;
  host.textContent = "";
  const topo = state.data.topology;
  host.append(h("p", { class: "card-sub" },
    `${topo.version} · ${topo.label} · 生效 ${fmtHM(topo.effective_from)}${topo.effective_to < 9e12 ? ` 至 ${fmtHM(topo.effective_to)}` : " 起"}`));
  if (topo.changes && topo.changes.length) {
    const ul = h("ul", { class: "alt-list" });
    for (const c of topo.changes) ul.append(h("li", {}, c));
    host.append(ul);
  }
  const diffs = state.data.topology_diff || [];
  if (diffs.length) {
    const other = topo.version === "v1" ? "v2" : "v1";
    host.append(h("p", { class: "card-sub", style: { margin: "8px 0 4px" } },
      `与 ${other} 版本的差异（仅用于对照，当前分析按 ${topo.version} 解释）`));
    const table = h("table", { class: "mini-table" });
    table.append(h("thead", {}, h("tr", {},
      h("th", {}, "对象"), h("th", {}, "变化"), h("th", {}, "类型"))));
    const tbody = h("tbody");
    for (const d of diffs) {
      tbody.append(h("tr", {},
        h("td", {}, d.name || d.entity),
        h("td", { style: { whiteSpace: "normal" } }, d.summary),
        h("td", {}, ({ capacity: "容量", alias: "条码别名", zone: "区域归属", added: "新增", removed: "移除" })[d.kind] || d.kind)));
    }
    table.append(tbody);
    host.append(table);
  } else {
    host.append(h("p", { class: "card-sub" }, "与上一版本相比没有实体、容量或条码变化。"));
  }
  const aliases = Object.entries(topo.aliases || {});
  if (aliases.length) {
    const ul = h("ul", { class: "alt-list" });
    for (const [from, to] of aliases) ul.append(h("li", {}, `旧条码 ${from} → ${to}（历史扫码按当时区域解释）`));
    host.append(h("div", { class: "card-sub", style: { marginTop: "6px" } }, "别名"));
    host.append(ul);
  }
}

function renderQuality() {
  const host = $("#quality-body");
  host.textContent = "";
  const q = state.data.summary.data_quality;
  const dup = state.data.merged_duplicates.filter((m) => m.ts >= state.window.start && m.ts <= state.window.end);
  const block = (title, rows) => {
    const b = h("div", { class: "q-block" }, h("h3", {}, title));
    const ul = h("ul", { class: "q-list" });
    for (const [k, v] of rows) ul.append(h("li", {}, h("span", {}, k), h("span", {}, v)));
    b.append(ul);
    return b;
  };
  host.append(block("重复扫码", [
    ["合并条数（本窗口）", fmtNum(dup.length)],
    ["合并条数（整场）", fmtNum(q.duplicate_count)],
    ["扫描设备数", fmtNum(q.devices.length)],
  ]));
  host.append(block("长间隔", [
    ["超过阈值次数", fmtNum(q.long_gap_count)],
    ["未知时长合计", fmtDur(q.unknown_seconds)],
  ]));
  if (q.long_gaps.length) {
    const details = h("div", { class: "q-block" }, h("h3", {}, "最长的无扫描间隔"));
    const ul = h("ul", { class: "q-list" });
    for (const g of q.long_gaps.slice(0, 5)) {
      ul.append(h("li", {},
        h("span", { class: "mono" }, `${g.picker} ${fmtTime(g.start)}–${fmtTime(g.end)}`),
        h("span", {}, fmtDur(g.gap))));
    }
    details.append(ul);
    host.append(details);
  }
  if (dup.length) {
    const details = h("div", { class: "q-block" }, h("h3", {}, "重复扫码明细"));
    const ul = h("ul", { class: "q-list" });
    for (const m of dup.slice(0, 6)) {
      ul.append(h("li", {},
        h("span", { class: "mono" }, `${m.picker} @ ${m.location} ${fmtTime(m.ts)}`),
        h("span", {}, `+${m.gap_s}s → ${m.kept}`)));
    }
    details.append(ul);
    host.append(details);
  }
}

/* ------------------------------------------------------------------ */
/* 播放与拖动                                                          */
/* ------------------------------------------------------------------ */

function updateScrub(advance = false) {
  if (!state.data) return;
  const base = state.data.scenario.start;
  $("#time-scrub").value = String(Math.round(state.scrub - base));
  $("#scrub-label").textContent = fmtTime(state.scrub);
  renderFloorMap();
  if (advance) {
    $("#time-scrub").setAttribute("aria-valuetext", fmtTime(state.scrub));
  }
}

function togglePlay() {
  if (state.playTimer) { stopPlay(); return; }
  if (!state.data) return;
  const speed = Number($("#play-speed").value);
  $("#play-toggle").textContent = "⏸ 暂停";
  state.playTimer = setInterval(() => {
    const { start, end } = state.window;
    let next = state.scrub + speed;
    if (next > end) next = start;
    state.scrub = next;
    updateScrub();
  }, 250);
}
function stopPlay() {
  if (state.playTimer) clearInterval(state.playTimer);
  state.playTimer = null;
  const btn = $("#play-toggle");
  if (btn) btn.textContent = "▶ 播放";
}

/* ------------------------------------------------------------------ */
/* 冲突视图                                                            */
/* ------------------------------------------------------------------ */

function windowConflicts() {
  const { start, end } = state.window;
  return (state.data?.conflicts || []).filter((c) => c.end > start && c.start < end);
}

function renderConflicts() {
  if (!state.data) return;
  renderConflictKpis();
  renderCauseBars();
  renderCapacityCharts();
  renderConflictList();
}

function renderConflictKpis() {
  const host = $("#conflict-kpis");
  host.textContent = "";
  const conflicts = windowConflicts();
  const confirmed = conflicts.filter((c) => c.overlap_kind === "confirmed");
  const possible = conflicts.filter((c) => c.overlap_kind === "possible");
  const seconds = conflicts.reduce((a, c) => a + c.duration, 0);
  const byCause = {};
  for (const c of conflicts) {
    for (const key of c.causes || [c.cause]) byCause[key] = (byCause[key] || 0) + 1;
  }
  const tiles = [
    { label: "超容量区间", value: fmtNum(conflicts.length), unit: "段", sub: `合计 ${fmtDur(seconds)}` },
    { label: "确证重叠", value: fmtNum(confirmed.length), sub: "观测占用人数已超过容量" },
    { label: "推测重叠", value: fmtNum(possible.length), sub: "叠加推测区间后才超容量" },
    { label: "任务交叉", value: fmtNum(byCause.task_cross || 0), sub: "不同区域/波次的任务交汇" },
    { label: "数据缺失", value: fmtNum(byCause.data_gap || 0), sub: "区间被扫描缺口拉长" },
    { label: "涉及作业单", value: fmtNum(new Set(conflicts.flatMap((c) => c.tasks)).size), unit: "单", sub: `涉及人员 ${new Set(conflicts.flatMap((c) => c.people)).size} 人` },
  ];
  for (const t of tiles) {
    host.append(h("div", { class: "kpi" },
      h("span", { class: "kpi-label" }, t.label),
      h("span", { class: "kpi-value" }, t.value, t.unit ? h("small", {}, t.unit) : null),
      h("span", { class: "kpi-sub" }, t.sub)));
  }
}

function renderCauseBars() {
  const host = $("#cause-bars");
  host.textContent = "";
  const conflicts = windowConflicts();
  const total = Math.max(1, conflicts.length);
  const counts = { task_cross: 0, data_gap: 0, possible: 0 };
  const seconds = { task_cross: 0, data_gap: 0, possible: 0 };
  for (const c of conflicts) {
    for (const key of (c.causes || [c.cause])) {
      counts[key] = (counts[key] || 0) + 1;
      seconds[key] = (seconds[key] || 0) + ((c.cause_seconds || {})[key] || 0);
    }
  }
  for (const key of ["task_cross", "data_gap", "possible"]) {
    const row = h("div", { class: "cause-row" });
    row.append(h("span", { class: "cause-label" }, CAUSE_TEXT[key]));
    const track = h("div", { class: "cause-track" });
    const fill = h("div", { class: `cause-fill ${key}`, style: { width: `${(counts[key] / total) * 100}%` } });
    fill.title = `${CAUSE_TEXT[key]}：${counts[key]} 段（合计 ${fmtDur(seconds[key])}）`;
    track.append(fill);
    row.append(track, h("span", { class: "cause-val" }, String(counts[key])));
    host.append(row);
  }
  host.append(h("p", { class: "card-sub" },
    `共 ${conflicts.length} 段超容量区间。一段区间可能同时满足多个成因，故各项之和可大于区间数；成因是候选解释，不是判决。`));
}

function entityConflictSeries(entityId) {
  const { start, end } = state.window;
  const step = state.data.entity_series.step;
  const points = [];
  for (let t = start; t < end; t += step) {
    points.push({ t, observed: 0, possible: 0, capacity: 1 });
  }
  for (const row of state.data.entity_series.series) {
    if (row.entity !== entityId) continue;
    const idx = Math.floor((row.t - start) / step);
    if (idx < 0 || idx >= points.length) continue;
    points[idx].observed = row.observed;
    points[idx].possible = row.possible;
    points[idx].capacity = row.capacity;
  }
  return points;
}

function renderCapacityCharts() {
  const host = $("#capacity-charts");
  host.textContent = "";
  const conflicts = windowConflicts();
  const totals = new Map();
  for (const c of conflicts) totals.set(c.entity, (totals.get(c.entity) || 0) + c.duration);
  const entities = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([id]) => id);
  if (!entities.length) {
    host.append(h("p", { class: "card-sub" }, "本窗口没有超容量区间。可放大时间窗或调整容量/参数后再看。"));
    return;
  }
  const topology = state.data.topology;
  for (const entityId of entities) {
    const ent = topology.entities.find((e) => e.id === entityId);
    if (!ent) continue;
    const points = entityConflictSeries(entityId);
    const entConflicts = conflicts.filter((c) => c.entity === entityId);
    host.append(capacityChart(ent, points, entConflicts));
  }
  const legend = $("#capacity-legend");
  legend.textContent = "";
  for (const [label, style] of [
    ["观测人数", { background: "var(--series-1)" }],
    ["推测上界（虚线）", { background: "transparent", border: "1px dashed var(--series-1)" }],
    ["容量", { background: "var(--status-critical)" }],
    ["超容量区间", { background: "var(--status-critical)", opacity: ".18" }],
  ]) {
    legend.append(h("span", { class: "item" }, h("span", { class: "swatch", style }), label));
  }
}

function capacityChart(ent, points, conflicts) {
  const W = 620, H = 104, padL = 26, padR = 8, padT = 8, padB = 20;
  const { start, end } = state.window;
  const span = Math.max(1, end - start);
  const maxY = Math.max(ent.capacity + 1, ...points.map((p) => p.observed + p.possible), 2);
  const x = (t) => padL + ((t - start) / span) * (W - padL - padR);
  const y = (v) => padT + (1 - v / maxY) * (H - padT - padB);
  const chart = h("div", { class: "cap-chart" });
  chart.append(h("div", { class: "cap-head" },
    h("strong", {}, ent.name),
    h("span", { class: "mono" }, `容量 ${ent.capacity} · 冲突 ${conflicts.length} 段 / ${fmtDur(conflicts.reduce((a, c) => a + c.duration, 0))}`)));

  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "cap-svg", role: "img", "aria-label": `${ent.name} 同时占用时间线` });
  // 超容量背景带
  for (const c of conflicts) {
    s.append(svg("rect", { x: x(Math.max(c.start, start)), y: padT, width: Math.max(1, x(Math.min(c.end, end)) - x(Math.max(c.start, start))), height: H - padT - padB, class: "cap-conflict-band" }));
  }
  // 网格与 y 轴
  for (let v = 0; v <= maxY; v++) {
    s.append(svg("line", { x1: padL, y1: y(v), x2: W - padR, y2: y(v), class: "cap-grid", opacity: v === 0 ? 0.9 : 0.55 }));
    s.append(svg("text", { x: padL - 5, y: y(v) + 2.5, class: "cap-axis-label", "text-anchor": "end" }, String(v)));
  }
  // 容量线
  s.append(svg("line", { x1: padL, y1: y(ent.capacity), x2: W - padR, y2: y(ent.capacity), class: "cap-cap" }));
  s.append(svg("text", { x: W - padR, y: y(ent.capacity) - 3, class: "cap-axis-label", "text-anchor": "end", fill: "var(--status-critical)" }, `容量 ${ent.capacity}`));
  // 观测阶梯线 / 推测上界
  const stepPath = (key) => {
    let d = "";
    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      const px = x(p.t), py = y(p[key]);
      if (i === 0) d += `M${px.toFixed(1)},${py.toFixed(1)}`;
      else d += `H${px.toFixed(1)}V${py.toFixed(1)}`;
    }
    return d;
  };
  s.append(svg("path", { d: stepPath("observed"), class: "cap-line-obs" }));
  const upper = points.map((p) => ({ t: p.t, v: p.observed + p.possible }));
  let dU = "";
  for (let i = 0; i < upper.length; i++) {
    const px = x(upper[i].t), py = y(upper[i].v);
    if (i === 0) dU += `M${px.toFixed(1)},${py.toFixed(1)}`;
    else dU += `H${px.toFixed(1)}V${py.toFixed(1)}`;
  }
  s.append(svg("path", { d: dU, class: "cap-line-poss" }));
  // x 轴刻度
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const t = start + (span * i) / ticks;
    s.append(svg("text", { x: x(t), y: H - 5, class: "cap-axis-label", "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle" }, fmtHM(t)));
  }
  // hover 层
  const cross = svg("line", { y1: padT, y2: H - padB, class: "cap-hover-line" });
  s.append(cross);
  const hit = svg("rect", { x: padL, y: 0, width: W - padL - padR, height: H, fill: "transparent" });
  const tip = h("div", { class: "map-tooltip", hidden: true });
  chart.append(s);
  chart.append(tip);
  chart.style.position = "relative";
  hit.addEventListener("pointermove", (ev) => {
    const box = s.getBoundingClientRect();
    const ratio = clamp((ev.clientX - box.left) / box.width, 0, 1);
    const t = start + ratio * span;
    const idx = clamp(Math.floor((t - start) / (span / Math.max(1, points.length))), 0, points.length - 1);
    const p = points[idx] || { t, observed: 0, possible: 0 };
    cross.setAttribute("x1", x(p.t)); cross.setAttribute("x2", x(p.t)); cross.style.opacity = "1";
    tip.textContent = "";
    tip.append(
      h("strong", { class: "mono" }, fmtTime(p.t)),
      h("div", {}, `观测 ${p.observed} 人 · 可能另有 ${p.possible} 人`),
      h("div", { style: { color: "var(--text-muted)", fontSize: "11px" } }, p.observed > ent.capacity ? "已超容量" : p.observed + p.possible > ent.capacity ? "叠加推测后超容量" : "未超容量"),
    );
    tip.hidden = false;
    const localX = (x(p.t) / W) * box.width;
    tip.style.left = `${clamp(localX + 12, 4, box.width - 190)}px`;
    tip.style.top = "6px";
  });
  hit.addEventListener("pointerleave", () => { tip.hidden = true; cross.style.opacity = "0"; });
  s.append(hit);
  return chart;
}

/* ------------------------------------------------------------------ */
/* 冲突明细                                                            */
/* ------------------------------------------------------------------ */

function conflictTaskRows(conflict) {
  const tasks = state.data.tasks.filter((t) => conflict.tasks.includes(t.id));
  return tasks;
}

function renderConflictList() {
  const host = $("#conflict-list");
  host.textContent = "";
  let conflicts = windowConflicts();
  if (state.conflictCause !== "all") {
    conflicts = conflicts.filter((c) => (c.causes || [c.cause]).includes(state.conflictCause));
  }
  conflicts.sort((a, b) => b.duration - a.duration);
  if (!conflicts.length) {
    host.append(h("p", { class: "card-sub" }, "该筛选下没有冲突区间。"));
    return;
  }
  for (const c of conflicts.slice(0, 120)) {
    const body = h("div", { class: "conflict-body", hidden: true });
    const tasks = conflictTaskRows(c);
    body.append(h("h4", {}, "涉及人员"));
    const chips = h("div", { class: "people-chips" });
    for (const p of c.people) chips.append(h("span", { class: "person-chip" }, p));
    body.append(chips);
    body.append(h("h4", {}, "作业单"));
    if (tasks.length) {
      const table = h("table", { class: "mini-table" });
      table.append(h("thead", {}, h("tr", {},
        h("th", {}, "作业单"), h("th", {}, "波次"), h("th", {}, "区域"), h("th", {}, "发单"),
        h("th", {}, "状态"), h("th", {}, "关联订单"), h("th", {}, "货位"))));
      const tbody = h("tbody");
      for (const t of tasks) {
        const cancelled = t.cancelled !== null && t.cancelled !== undefined;
        tbody.append(h("tr", {},
          h("td", { class: "mono" }, t.id),
          h("td", {}, t.wave || "—"),
          h("td", {}, ZONE_NAME[t.zone] || t.zone),
          h("td", { class: "mono" }, fmtHM(t.released)),
          h("td", {}, cancelled ? `已取消（${fmtHM(t.cancelled)}${t.cancel_reason ? " · " + t.cancel_reason : ""}）` : "执行中/完成"),
          h("td", { class: "mono", style: { whiteSpace: "normal" } }, t.orders.join("、") || "—"),
          h("td", { class: "mono", style: { whiteSpace: "normal" } }, t.locations.join("、") || "—"),
        ));
      }
      table.append(tbody);
      body.append(table);
    } else {
      body.append(h("p", { class: "card-sub" }, "冲突区间内的任务不属于当前作业单集合（例如取消或手工扫码）。"));
    }
    body.append(h("h4", {}, "判定依据"));
    const reasons = h("ul", { class: "alt-list" });
    for (const r of (c.reasons || [c.detail])) reasons.append(h("li", {}, r));
    body.append(reasons);
    body.append(h("h4", {}, "替代解释（不是结论）"));
    const alts = h("ul", { class: "alt-list" });
    for (const a of c.alternatives) alts.append(h("li", {}, a));
    body.append(alts);
    body.append(h("div", { style: { marginTop: "8px" } },
      h("button", {
        class: "ghost-btn small",
        onclick: () => {
          state.scrub = c.start;
          $("#picker-filter").value = "all";
          state.filters.picker = "all";
          switchView("floor");
          syncWindowFields();
          renderFloorMap();
        },
      }, "在平面图中定位此时刻")));

    const btn = h("button", {
      class: "conflict-head",
      "aria-expanded": "false",
      onclick: () => {
        const open = body.hidden;
        body.hidden = !open;
        btn.setAttribute("aria-expanded", String(open));
      },
    },
      h("span", { class: "conflict-time" }, `${fmtHM(c.start)}–${fmtHM(c.end)}`),
      h("span", { class: "conflict-main" },
        h("strong", {}, `${entityName(c.entity)} · 峰值 ${c.peak} 人 / 容量 ${c.capacity}`),
        h("small", {}, `${c.detail}`)),
      h("span", { class: `badge ${c.overlap_kind === "confirmed" ? "confirmed" : "possible"}` },
        c.overlap_kind === "confirmed" ? "确证" : "推测"),
      h("span", { class: "badge-group" },
        (c.causes || [c.cause]).map((key) => h("span", { class: "badge" }, CAUSE_TEXT[key] || key))),
      h("span", { class: "conflict-caret", "aria-hidden": "true" }, "▸"),
    );
    host.append(h("article", { class: "conflict-item" }, btn, body));
  }
}

function entityName(id) {
  const ent = state.data?.topology.entities.find((e) => e.id === id);
  return ent ? ent.name : id;
}

/* ------------------------------------------------------------------ */
/* 表格视图                                                            */
/* ------------------------------------------------------------------ */

function renderTable(kind, host) {
  host.textContent = "";
  const table = document.createElement("table");
  const thead = h("thead");
  const tbody = h("tbody");
  const addHead = (labels) => {
    const tr = h("tr");
    for (const l of labels) tr.append(h("th", {}, l));
    thead.append(tr);
  };
  if (kind === "zone") {
    addHead(["通道", "区域", "容量", "窗口内峰值(观测)", "峰值(含推测)"]);
    const buckets = state.data.zone_occupancy.buckets;
    for (const ent of state.data.topology.entities) {
      if (state.filters.zone !== "all" && ent.zone !== state.filters.zone) continue;
      let peakObs = 0, peakAll = 0;
      for (const b of buckets) {
        const v = b.entities[ent.id] || { observed: 0, possible: 0 };
        peakObs = Math.max(peakObs, v.observed);
        peakAll = Math.max(peakAll, v.observed + v.possible);
      }
      tbody.append(h("tr", {},
        h("td", {}, ent.name), h("td", {}, ZONE_NAME[ent.zone] || ent.zone),
        h("td", { class: "num" }, String(ent.capacity)),
        h("td", { class: "num" }, String(peakObs)), h("td", { class: "num" }, String(peakAll))));
    }
  } else if (kind === "segments") {
    addHead(["人员", "类型", "区域", "实体", "货位", "开始", "结束", "时长", "任务", "说明"]);
    for (const s of windowSegments()) {
      if (state.filters.picker !== "all" && s.picker !== state.filters.picker) continue;
      tbody.append(h("tr", {},
        h("td", {}, s.picker), h("td", {}, s.kind_label), h("td", {}, ZONE_NAME[s.zone] || s.zone),
        h("td", {}, s.entity), h("td", {}, s.location || "—"),
        h("td", { class: "num" }, fmtTime(s.start)), h("td", { class: "num" }, fmtTime(s.end)),
        h("td", { class: "num" }, fmtDur(s.duration)), h("td", {}, s.task || "—"),
        h("td", { style: { whiteSpace: "normal" } }, s.note || "—")));
    }
  } else if (kind === "capacity") {
    addHead(["时刻", "实体", "观测人数", "可能人数", "容量"]);
    for (const row of state.data.entity_series.series) {
      if (state.filters.zone !== "all") {
        const ent = state.data.topology.entities.find((e) => e.id === row.entity);
        if (!ent || ent.zone !== state.filters.zone) continue;
      }
      tbody.append(h("tr", {},
        h("td", { class: "num" }, fmtTime(row.t)), h("td", {}, entityName(row.entity)),
        h("td", { class: "num" }, String(row.observed)), h("td", { class: "num" }, String(row.possible)),
        h("td", { class: "num" }, String(row.capacity))));
    }
  } else if (kind === "conflicts") {
    addHead(["实体", "开始", "结束", "时长", "峰值", "容量", "重叠性质", "成因", "人员", "作业单"]);
    for (const c of windowConflicts()) {
      tbody.append(h("tr", {},
        h("td", {}, entityName(c.entity)), h("td", { class: "num" }, fmtTime(c.start)),
        h("td", { class: "num" }, fmtTime(c.end)), h("td", { class: "num" }, fmtDur(c.duration)),
        h("td", { class: "num" }, String(c.peak)), h("td", { class: "num" }, String(c.capacity)),
        h("td", {}, c.overlap_kind === "confirmed" ? "确证" : "推测"),
        h("td", {}, (c.causes || [c.cause]).map((key) => CAUSE_TEXT[key] || key).join("、")),
        h("td", {}, c.people.join("、")), h("td", { style: { whiteSpace: "normal" } }, c.tasks.join("、"))));
    }
  }
  table.append(thead, tbody);
  host.append(table);
}

/* ------------------------------------------------------------------ */
/* 回放                                                                */
/* ------------------------------------------------------------------ */

async function loadReplay() {
  $("#view-replay").classList.add("loading");
  try {
    const data = await api(`/api/replay?${paramQuery().toString()}`);
    state.replay = data;
    state.replayTime = data.runs.a.start + 300;
    $("#replay-scrub").max = String(data.runs.a.end - data.runs.a.start);
    $("#replay-scrub").value = "300";
    renderReplay();
  } catch (err) {
    toast(`回放加载失败：${err.message}`, 5000);
    console.error(err);
  } finally {
    $("#view-replay").classList.remove("loading");
  }
}

function renderReplay() {
  if (!state.replay) return;
  renderReplayKpis();
  renderReplayCompare();
  renderReplayMaps();
  renderReplayDiff();
  const caveats = $("#replay-caveats");
  caveats.textContent = "";
  caveats.append(h("strong", {}, "读数前请先看："), h("br"));
  caveats.append(document.createTextNode(state.replay.caveats.join(" ")));
}

function renderReplayKpis() {
  const host = $("#replay-kpis");
  host.textContent = "";
  const { a, b } = state.replay.totals;
  const tiles = [
    { label: "共享作业单", value: fmtNum(state.replay.shared_tasks), unit: "单", sub: "两套方案作业内容完全相同" },
    { label: "冲突区间数", value: `${a.conflicts} → ${b.conflicts}`, sub: `确证 ${b.confirmed} · 推测 ${b.possible}（方案 B）` },
    { label: "冲突时长", value: `${fmtDur(a.conflict_seconds)} → ${fmtDur(b.conflict_seconds)}`, sub: "超容量区间的总时长" },
    { label: "总行走距离", value: `${fmtNum(a.walk_m)} → ${fmtNum(b.walk_m)} m`, sub: "方案 B 减少了集中度，但未必减少行走" },
    { label: "位置未知时长", value: `${fmtDur(a.unknown_seconds)} → ${fmtDur(b.unknown_seconds)}`, sub: "未知越多，冲突结论越不可靠" },
  ];
  for (const t of tiles) {
    host.append(h("div", { class: "kpi" },
      h("span", { class: "kpi-label" }, t.label),
      h("span", { class: "kpi-value kpi-pair" }, t.value),
      h("span", { class: "kpi-sub" }, t.sub)));
  }
}

function renderReplayCompare() {
  const host = $("#replay-compare");
  host.textContent = "";
  const { a, b } = state.replay.totals;
  host.append(h("div", { class: "rc-legend legend" },
    h("span", { class: "item" }, h("span", { class: "swatch", style: { background: "var(--series-1)" } }), "方案 A · 波次集中"),
    h("span", { class: "item" }, h("span", { class: "swatch", style: { background: "var(--series-2)" } }), "方案 B · 跨区混流")));
  host.append(h("div", { class: "rc-row head" },
    h("span", {}, "指标"), h("span", {}, "方案 A · 波次集中"), h("span", {}, "方案 B · 跨区混流"), h("span", { style: { textAlign: "right" } }, "差异 B−A")));
  const rows = [
    { label: "超容量区间", a: a.conflicts, b: b.conflicts, fmt: (v) => fmtNum(v), better: "lower" },
    { label: "确证重叠", a: a.confirmed, b: b.confirmed, fmt: (v) => fmtNum(v), better: "lower" },
    { label: "推测重叠", a: a.possible, b: b.possible, fmt: (v) => fmtNum(v), better: "lower" },
    { label: "冲突时长", a: a.conflict_seconds, b: b.conflict_seconds, fmt: (v) => fmtDur(v), better: "lower" },
    { label: "总行走距离", a: a.walk_m, b: b.walk_m, fmt: (v) => `${fmtNum(v)} m`, better: "lower" },
    { label: "位置未知时长", a: a.unknown_seconds, b: b.unknown_seconds, fmt: (v) => fmtDur(v), better: "lower" },
  ];
  for (const r of rows) {
    const isDur = r.label === "冲突时长" || r.label === "位置未知时长";
    const max = Math.max(r.a, r.b, 1);
    const delta = r.b - r.a;
    const cls = delta === 0 ? "neutral" : (r.better === "lower" ? (delta < 0 ? "better" : "worse") : (delta > 0 ? "better" : "worse"));
    host.append(h("div", { class: "rc-row" },
      h("span", { class: "rc-label" }, r.label),
      h("span", { class: "rc-pair", "aria-label": `方案 A：${r.fmt(r.a)}` },
        h("span", { class: "rc-bar a", title: `方案 A：${r.fmt(r.a)}` }, h("span", { style: { width: `${(r.a / max) * 100}%` } })),
        h("span", { class: "rc-val" }, r.fmt(r.a))),
      h("span", { class: "rc-pair", "aria-label": `方案 B：${r.fmt(r.b)}` },
        h("span", { class: "rc-bar b", title: `方案 B：${r.fmt(r.b)}` }, h("span", { style: { width: `${(r.b / max) * 100}%` } })),
        h("span", { class: "rc-val" }, r.fmt(r.b))),
      h("span", { class: `rc-delta ${cls}`, title: `差异 B−A：${isDur ? fmtDurSigned(delta) : r.fmt(delta)}` },
        isDur ? fmtDurSigned(delta) : `${delta > 0 ? "+" : ""}${r.fmt(delta)}`)));
  }
  host.append(h("p", { class: "card-sub" }, "条长按该行两方案的最大值归一；数值随条直接标出，展开“表格视图”可看完整口径。"));
  const toggle = h("div", { class: "table-toggle" },
    h("button", { class: "link-btn", onclick: () => {
      const box = $("#replay-table");
      box.hidden = !box.hidden;
      renderReplayTable(box);
    } }, "表格视图"));
  host.append(toggle);
  host.append(h("div", { class: "table-host", id: "replay-table", hidden: true }));
}

function renderReplayTable(host) {
  host.textContent = "";
  const { a, b } = state.replay.runs;
  const table = document.createElement("table");
  const rows = [
    ["方案", a.name + " / " + b.name],
    ["冲突区间", `${a.summary.conflict_count} / ${b.summary.conflict_count}`],
    ["确证/推测", `${a.summary.confirmed_conflicts}/${a.summary.possible_conflicts} 与 ${b.summary.confirmed_conflicts}/${b.summary.possible_conflicts}`],
    ["冲突时长", `${fmtDur(a.summary.conflict_seconds)} / ${fmtDur(b.summary.conflict_seconds)}`],
    ["扫描次数", `${a.summary.scans} / ${b.summary.scans}`],
    ["合并重复", `${a.summary.merged_duplicates} / ${b.summary.merged_duplicates}`],
  ];
  table.append(h("thead", {}, h("tr", {}, h("th", {}, "指标"), h("th", {}, "A / B"))));
  const tbody = h("tbody");
  for (const [k, v] of rows) tbody.append(h("tr", {}, h("td", {}, k), h("td", { class: "num", style: { whiteSpace: "normal" } }, v)));
  table.append(tbody);
  host.append(table);
}

function replayActiveAt(run, time) {
  const map = new Map();
  for (const s of run.segments) {
    if (s.start <= time && time < s.end) {
      if (!map.has(s.picker)) map.set(s.picker, []);
      map.get(s.picker).push(s);
    }
  }
  return map;
}

function renderReplayMaps() {
  if (!state.replay || !state.data) return;
  const time = state.replayTime;
  for (const key of ["a", "b"]) {
    const run = state.replay.runs[key];
    const host = $(`#replay-map-${key}`);
    host.textContent = "";
    const topology = state.data.topology;
    const active = replayActiveAt(run, time);
    const occ = new Map();
    for (const segs of active.values()) {
      for (const s of segs) {
        if (!s.entity || s.entity === "UNKNOWN") continue;
        const rec = occ.get(s.entity) || { obs: new Set(), poss: new Set() };
        (s.kind === "dwell" ? rec.obs : rec.poss).add(s.picker);
        occ.set(s.entity, rec);
      }
    }
    const labels = [];
    for (const ent of topology.entities) {
      const rec = occ.get(ent.id);
      const over = rec && rec.obs.size > ent.capacity;
      host.append(svg("rect", {
        x: ent.rect.x, y: ent.rect.y, width: ent.rect.w, height: ent.rect.h, rx: 1.2,
        class: `entity-rect zone-${ent.zone}` + (over ? " over-cap" : ""),
      }));
      const name = ent.name.replace(/（.*）/, "");
      labels.push(svg("text", { x: ent.rect.x + ent.rect.w / 2, y: ent.rect.y + 2.9, class: "entity-label", "text-anchor": "middle" }, name));
      if (rec) {
        labels.push(svg("text", {
          x: ent.rect.x + ent.rect.w / 2, y: ent.rect.y + ent.rect.h - 0.9,
          class: "entity-sub", "text-anchor": "middle",
        }, `${rec.obs.size}${rec.poss.size ? `+${rec.poss.size}?` : ""} / ${ent.capacity}`));
      }
    }
    const unknown = [];
    for (const [picker, segs] of active.entries()) {
      const located = segs.filter((s) => s.entity !== "UNKNOWN" && s.kind !== "unknown");
      if (!located.length) { unknown.push(picker); continue; }
      for (const seg of located) {
        const pt = locationPoint(topology, seg);
        if (!pt) continue;
        const off = hashOffset(picker + key);
        const isObs = seg.kind === "dwell";
        const g = svg("g", {
          class: `person-dot ${isObs ? "observed" : "possible"}`,
          transform: `translate(${(pt.x + off.dx).toFixed(2)} ${(pt.y + off.dy).toFixed(2)})`,
          "aria-label": `${picker} ${seg.kind_label}`,
        });
        g.append(svg("circle", { r: 2.0, fill: "transparent" }));
        g.append(svg("circle", {
          r: 1.05, class: "core",
          fill: isObs ? `var(${ZONE_COLOR[seg.zone] || "--series-1"})` : "var(--surface-0)",
          stroke: `var(${ZONE_COLOR[seg.zone] || "--series-1"})`,
        }));
        host.append(g);
      }
    }
    for (const label of labels) host.append(label);
    host.append(svg("rect", {
      x: MAP_BOX.x + 1, y: UNKNOWN_STRIP.y, width: MAP_BOX.w - 2, height: UNKNOWN_STRIP.h,
      rx: 1.5, fill: "var(--wash-neutral)", stroke: "var(--grid)", "stroke-width": 0.3,
    }));
    host.append(svg("text", { x: MAP_BOX.x + 3, y: UNKNOWN_STRIP.y + 3.2, class: "entity-sub", "text-anchor": "start" },
      unknown.length ? `位置未知 ${unknown.length} 人：${unknown.join(" ")}` : "位置未知：无"));
    const stats = $(`#replay-stats-${key}`);
    stats.textContent = "";
    const over = [...occ.entries()].filter(([id, v]) => {
      const ent = topology.entities.find((e) => e.id === id);
      return ent && v.obs.size > ent.capacity;
    });
    stats.append(
      h("span", {}, `活跃 `, h("b", {}, String(new Set(active.keys()).size))),
      h("span", {}, `未知 `, h("b", {}, String(unknown.length))),
      h("span", {}, `超容量通道 `, h("b", {}, String(over.length))),
    );
    if (key === "a") $("#replay-label").textContent = fmtTime(time);
  }
  updateReplayTimelineStats();
}

function updateReplayTimelineStats() {
  // 每 30 秒统计一次超容量，用于差异卡片
  const time = state.replayTime;
  for (const key of ["a", "b"]) {
    const run = state.replay.runs[key];
    const active = replayActiveAt(run, time);
    const occ = new Map();
    for (const segs of active.values()) {
      for (const s of segs) {
        if (!s.entity || s.entity === "UNKNOWN") continue;
        const rec = occ.get(s.entity) || { obs: new Set(), poss: new Set() };
        (s.kind === "dwell" ? rec.obs : rec.poss).add(s.picker);
        occ.set(s.entity, rec);
      }
    }
    const tiles = $(`#replay-stats-${key}`);
    if (!tiles) continue;
  }
}

function renderReplayDiff() {
  const host = $("#replay-diff");
  host.textContent = "";
  const { a, b } = state.replay.totals;
  const cards = [
    {
      title: "冲突区间",
      value: `${a.conflicts} → ${b.conflicts}（${b.conflicts - a.conflicts >= 0 ? "+" : ""}${b.conflicts - a.conflicts}）`,
      text: b.conflicts < a.conflicts
        ? "方案 B 的超容量区间更少。可能因为同一时刻同区进场的人被摊开；也可能只是该窗口内任务释放更分散。"
        : b.conflicts > a.conflicts
          ? "方案 B 的超容量区间更多。可能与其混流让快拣/冷链在同通道交汇有关；同样可能受随机行走抖动影响。"
          : "两套方案的超容量区间数相同，差异主要体现在时长与成因构成上。",
    },
    {
      title: "冲突时长",
      value: `${fmtDur(a.conflict_seconds)} → ${fmtDur(b.conflict_seconds)}`,
      text: "时长比区间数更能反映通道被挤占的时间。但若一边的扫描缺口更多，推测区间会被拉长，时长会随之虚高。",
    },
    {
      title: "行走距离",
      value: `${fmtNum(a.walk_m)} m → ${fmtNum(b.walk_m)} m`,
      text: b.walk_m <= a.walk_m
        ? "方案 B 的行走距离没有增加，说明分散发单未以更多行走为代价（仅限本仿真）。"
        : "方案 B 行走距离增加。冲突变少可能是用更多行走换来的，不能直接说方案更优。",
    },
    {
      title: "数据质量差异",
      value: `未知 ${fmtDur(a.unknown_seconds)} → ${fmtDur(b.unknown_seconds)}`,
      text: "两边的扫描缺失程度不同，冲突总数不可直接横比。比较前应先看未知时长的比例。",
    },
  ];
  for (const c of cards) {
    host.append(h("div", { class: "diff-card" },
      h("h3", {}, c.title),
      h("div", { class: "diff-value" }, c.value),
      h("p", {}, c.text)));
  }
}

function toggleReplayPlay() {
  if (state.replayTimer) { stopReplayPlay(); return; }
  if (!state.replay) return;
  $("#replay-play").textContent = "⏸ 暂停";
  state.replayTimer = setInterval(() => {
    const start = state.replay.runs.a.start, end = state.replay.runs.a.end;
    let next = state.replayTime + 60;
    if (next > end) next = start;
    state.replayTime = next;
    $("#replay-scrub").value = String(Math.round(next - start));
    updateReplayMaps(false);
  }, 220);
}
function stopReplayPlay() {
  if (state.replayTimer) clearInterval(state.replayTimer);
  state.replayTimer = null;
  const btn = $("#replay-play");
  if (btn) btn.textContent = "▶ 播放";
}

/* ------------------------------------------------------------------ */
/* 参数面板                                                            */
/* ------------------------------------------------------------------ */

function buildParamFields(meta) {
  const host = $("#params-fields");
  host.textContent = "";
  for (const cfg of PARAM_CONFIG) {
    const out = h("output", { for: `param-${cfg.key}` }, paramText(cfg.key, state.params[cfg.key], cfg));
    const input = h("input", {
      type: "range", id: `param-${cfg.key}`, min: String(cfg.min), max: String(cfg.max),
      step: String(cfg.step), value: String(state.params[cfg.key]),
      "aria-describedby": `hint-${cfg.key}`,
      oninput: (e) => {
        state.params[cfg.key] = Number(e.target.value);
        out.textContent = paramText(cfg.key, e.target.value, cfg);
      },
    });
    host.append(h("div", { class: "param-field" },
      h("label", { for: `param-${cfg.key}` }, (meta.param_labels[cfg.key] || cfg.key), out),
      input,
      h("p", { class: "hint", id: `hint-${cfg.key}` }, cfg.hint)));
  }
}

function paramText(key, value, cfg) {
  const v = Number(value);
  if (key === "walk_slack") return `${v.toFixed(2)}×`;
  if (key === "walk_speed_mps") return `${v.toFixed(2)} ${cfg.unit}`;
  return `${Math.round(v)} ${cfg.unit}`;
}

function syncParamInputs() {
  for (const cfg of PARAM_CONFIG) {
    const input = $(`#param-${cfg.key}`);
    if (!input) continue;
    input.value = String(state.params[cfg.key]);
    const out = input.parentElement.querySelector("output");
    if (out) out.textContent = paramText(cfg.key, state.params[cfg.key], cfg);
  }
}

function renderSpeedAdvice(est) {
  const host = $("#speed-advice");
  host.textContent = "";
  if (!est || !est.samples) {
    host.append(h("span", {}, "样本不足，无法给出建议。"));
    $("#apply-speed").hidden = true;
    return;
  }
  host.append(
    h("div", { class: "cal-row" }, h("span", {}, "样本对数"), h("span", {}, String(est.samples))),
    h("div", { class: "cal-row" }, h("span", {}, "回归斜率"), h("span", {}, `${est.slope_s_per_m} 秒/米`)),
    h("div", { class: "cal-row" }, h("span", {}, "估计驻留"), h("span", {}, est.dwell_estimate !== null ? fmtDur(est.dwell_estimate) : "—")),
    h("div", { class: "cal-row" }, h("span", {}, "建议速度"), h("span", {}, `${est.suggested_speed.toFixed(2)} m/s`)),
    h("div", { class: "cal-row" }, h("span", {}, "当前使用"), h("span", {}, `${est.current_speed.toFixed(2)} m/s`)),
    h("p", { class: "hint" }, est.note || ""),
  );
  const btn = $("#apply-speed");
  btn.dataset.speed = String(est.suggested_speed);
  btn.hidden = Math.abs(est.suggested_speed - est.current_speed) < 0.03;
}

function renderCalibration(cal) {
  const host = $("#calibration");
  host.textContent = "";
  if (!cal) {
    host.append(h("span", {}, "导入数据没有真值，无法校准。推断仍可运行，但精度未知。"));
    return;
  }
  const rows = [
    ["驻留归属准确率", cal.dwell_attribution_accuracy === null ? "—" : pct(cal.dwell_attribution_accuracy)],
    ["未知状态召回率", cal.unknown_recall === null ? "—" : pct(cal.unknown_recall)],
    ["陈旧位置占比", pct(cal.stale_location_rate)],
    ["观测/未知时长", `${cal.observed_hours} / ${cal.unknown_hours} 小时`],
    ["真值超容量时长", `${fmtDur(cal.truth_conflict_seconds)}（含通道穿行 ${fmtDur(cal.truth_conflict_seconds_any)}）`],
    ["推断超容量时长", `${fmtDur(cal.inferred_conflict_seconds)}（含推测 ${fmtDur(cal.inferred_conflict_seconds_upper)}）`],
  ];
  for (const [k, v] of rows) host.append(h("div", { class: "cal-row" }, h("span", {}, k), h("span", {}, v)));
  for (const note of cal.notes) host.append(h("p", { class: "hint" }, note));
}

/* ------------------------------------------------------------------ */
/* 导入                                                                */
/* ------------------------------------------------------------------ */

async function onImport(ev) {
  ev.preventDefault();
  const form = ev.currentTarget;
  const err = $("#import-error");
  err.hidden = true;
  const files = {
    topology: form.elements.topology.files[0],
    scans: form.elements.scans.files[0],
    tasks: form.elements.tasks.files[0],
  };
  if (!files.topology || !files.scans || !files.tasks) {
    err.textContent = "请选择三个文件：拓扑、扫码、作业单。";
    err.hidden = false;
    return;
  }
  try {
    const [topology, scans, tasks] = await Promise.all([
      files.topology.text(), files.scans.text(), files.tasks.text(),
    ]);
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    btn.textContent = "解析中…";
    const result = await api("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topology, scans, tasks, label: files.topology.name.replace(/\.json$/i, ""), params: state.params }),
    });
    state.imported = result;
    renderImportResult(result);
    toast("导入完成");
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = false;
    btn.textContent = "解析并分析";
  }
}

function renderImportResult(result) {
  const host = $("#import-result");
  host.hidden = false;
  host.textContent = "";
  const info = result.imported;
  host.append(h("h3", {}, `已导入：${info.label}`));
  const rows = [
    ["拓扑版本", info.topology],
    ["扫描记录", `${info.scans} 条（合并重复 ${result.summary.merged_duplicates}）`],
    ["作业单", `${info.tasks} 单`],
    ["人员", info.pickers.join("、")],
    ["站点实体", `${result.topology.entities.length} 个`],
    ["超容量区间", `${result.summary.conflict_count} 段（确证 ${result.summary.confirmed_conflicts} / 推测 ${result.summary.possible_conflicts}）`],
    ["位置未知时长", fmtDur(result.summary.time_by_kind.unknown || 0)],
  ];
  const ul = h("ul", { class: "q-list" });
  for (const [k, v] of rows) ul.append(h("li", {}, h("span", {}, k), h("span", {}, v)));
  host.append(ul);
  if (info.warnings && info.warnings.length) {
    for (const w of info.warnings) host.append(h("p", { class: "form-error" }, w));
  }
  if (result.conflicts.length) {
    host.append(h("h3", { style: { marginTop: "12px" } }, "超容量区间（前 10）"));
    const table = h("table", { class: "mini-table" });
    table.append(h("thead", {}, h("tr", {},
      h("th", {}, "实体"), h("th", {}, "开始"), h("th", {}, "时长"), h("th", {}, "峰值"), h("th", {}, "成因"))));
    const tbody = h("tbody");
    for (const c of result.conflicts.slice(0, 10)) {
      tbody.append(h("tr", {},
        h("td", {}, c.entity), h("td", { class: "num" }, fmtTime(c.start)),
        h("td", { class: "num" }, fmtDur(c.duration)),
        h("td", { class: "num" }, `${c.peak}/${c.capacity}`),
        h("td", {}, (c.cause_labels || [c.cause_label]).join("、"))));
    }
    table.append(tbody);
    host.append(table);
  }
}

/* ------------------------------------------------------------------ */

boot();
