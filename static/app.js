// Claude History Dashboard — frontend (vanilla JS + Chart.js)
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const charts = {};
let SESSIONS = [];
let ACTIVE_TIMER = null;

const fmt = (n) => (n ?? 0).toLocaleString("es-CO");
const fmtK = (n) => {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
};
const usd = (n) => "$" + (n ?? 0).toLocaleString("es-CO", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtDate = (s) => (s ? new Date(s).toLocaleString("es-CO", { dateStyle: "medium", timeStyle: "short" }) : "—");
const fmtDay = (s) => (s ? new Date(s).toLocaleDateString("es-CO", { dateStyle: "medium" }) : "—");

const PALETTE = ["#2563eb", "#16a34a", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4",
  "#ec4899", "#84cc16", "#f97316", "#14b8a6", "#a855f7", "#64748b", "#0ea5e9", "#22c55e"];

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}

// --------------------------------------------------------------------- Tabs
$$("#tabs .tab").forEach((b) =>
  b.addEventListener("click", () => switchTab(b.dataset.tab)));

function switchTab(tab) {
  $$("#tabs .tab").forEach((b) => b.classList.toggle("tab-active", b.dataset.tab === tab));
  $$(".view").forEach((v) => v.classList.add("hidden"));
  $("#view-" + tab).classList.remove("hidden");
  // Polling de activas sólo en la pestaña Sesiones.
  if (tab === "sessions") startActivePolling();
  else stopActivePolling();
  if (tab === "mcp") loadMcp();
}

// ----------------------------------------------------------------- Overview
function kpiCard(label, value, sub) {
  return `<div class="card p-4">
    <div class="text-xs text-slate-400">${label}</div>
    <div class="text-2xl font-semibold kpi-num mt-1">${value}</div>
    ${sub ? `<div class="text-xs text-slate-500 mt-1">${sub}</div>` : ""}
  </div>`;
}

async function loadOverview() {
  const o = await api("/api/overview");
  $("#root-path").textContent = o.root_path + "  ·  fuente: ~/.claude (jsonl + history.jsonl + sessions)";
  $("#loaded-at").textContent = "sync " + fmtDate(o.last_sync);

  $("#honesty-note").innerHTML = `
    <div class="font-medium text-amber-300 mb-1">ℹ️ Cómo leer estos números (honestidad)</div>
    <ul class="list-disc pl-5 space-y-1 text-slate-300">
      <li><b>${fmt(o.total_sessions)} sesiones</b> en total: ${fmt(o.detail_sessions)} con transcripción completa y
        <b>${fmt(o.history_only_sessions)} recuperadas de <code>history.jsonl</code></b> porque Claude Code
        ya borró su transcripción (limpieza automática <code>cleanupPeriodDays</code>, default 30 días).
        Por eso el historial real arranca el ${fmtDay(o.first_activity)}.</li>
      <li>El <b>costo es una estimación equivalente-API</b> (precios pay-per-token). Si usas Claude Code con
        <b>suscripción (Pro/Max/Team/Enterprise)</b> NO pagas por token: pagas por asiento y el uso cuenta
        contra límites de tasa, no contra dólares. Tómalo como volumen, no como factura.</li>
      <li>Los <b>tokens están dominados por <code>cache_read</code></b> (${fmtK(o.cache_read_tokens)}), normal en
        Claude Code: relee el contexto cacheado en cada turno (se cobra a 0.1× del input).</li>
    </ul>`;

  $("#kpis").innerHTML = [
    kpiCard("Sesiones", fmt(o.total_sessions), `${o.detail_sessions} con detalle · ${o.history_only_sessions} purgadas`),
    kpiCard("Prompts del usuario", fmt(o.total_user_prompts), `${fmt(o.total_assistant_msgs)} respuestas`),
    kpiCard("Tool calls", fmt(o.total_tool_calls)),
    kpiCard("Tokens totales", fmtK(o.total_tokens), `${fmtK(o.output_tokens)} salida · ${fmtK(o.cache_read_tokens)} cache read`),
    kpiCard("Costo estimado (API)", usd(o.total_cost), "no es tu factura con suscripción"),
    kpiCard("Días activos", fmt(o.days_active), `${fmtDay(o.first_activity)} → ${fmtDay(o.last_activity)}`),
  ].join("");

  drawDoughnut("chart-tokens",
    ["Entrada", "Salida", "Cache write 5m", "Cache write 1h", "Cache read"],
    [o.input_tokens, o.output_tokens, o.cache_write_5m, o.cache_write_1h, o.cache_read_tokens]);
  drawDoughnut("chart-models",
    o.models.map((m) => m[0].replace("claude-", "")), o.models.map((m) => m[1]));

  // Tabla de precios
  $("#pricing-table").innerHTML = `
    <table class="w-full">
      <thead class="text-slate-400"><tr>
        <th class="text-left py-1">Familia</th><th class="text-right">Input</th><th class="text-right">Output</th>
        <th class="text-right">Cache write 5m</th><th class="text-right">Cache write 1h</th><th class="text-right">Cache read</th>
      </tr></thead><tbody>
      ${o.pricing_table.map((p) => `<tr class="border-t border-[#1f2735] kpi-num">
        <td class="py-1">${p.family}</td><td class="text-right">$${p.input}</td><td class="text-right">$${p.output}</td>
        <td class="text-right">$${p.cache_write_5m}</td><td class="text-right">$${p.cache_write_1h}</td>
        <td class="text-right">$${p.cache_read}</td></tr>`).join("")}
      </tbody></table>`;

  await Promise.all([loadProjectCharts(), loadDailyChart()]);
}

async function loadProjectCharts() {
  const p = await api("/api/projects");
  drawBar("chart-proj", p.map((x) => x.project), p.map((x) => x.total_tokens), "Tokens", true);
  drawBar("chart-cost", p.map((x) => x.project), p.map((x) => x.cost), "USD", true);
}

async function loadDailyChart() {
  const d = await api("/api/timeseries/day");
  const ctx = $("#chart-daily");
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    data: {
      labels: d.map((x) => x.period),
      datasets: [
        { type: "bar", label: "Tokens", data: d.map((x) => x.total_tokens),
          backgroundColor: "#2563eb", yAxisID: "y", borderRadius: 3 },
        { type: "line", label: "Prompts", data: d.map((x) => x.user_prompts),
          borderColor: "#f59e0b", backgroundColor: "#f59e0b", yAxisID: "y1", tension: .3, pointRadius: 2 },
      ],
    },
    options: baseOpts({
      scales: {
        x: gridX(),
        y: { position: "left", ...gridY(), ticks: { ...tickColor(), callback: fmtK } },
        y1: { position: "right", grid: { drawOnChartArea: false }, ticks: tickColor() },
      },
    }),
  });
}

// --------------------------------------------------------------- Proyectos
async function loadProjects() {
  const p = await api("/api/projects");
  $("#projects-body").innerHTML = p.map((x) => `
    <tr class="border-t border-[#1f2735] hover:bg-[#0f1420]">
      <td class="px-4 py-3 font-medium">${x.project}</td>
      <td class="px-4 py-3 text-right kpi-num">${fmt(x.sessions)}</td>
      <td class="px-4 py-3 text-right kpi-num text-amber-400/80">${fmt(x.pruned)}</td>
      <td class="px-4 py-3 text-right kpi-num">${fmt(x.user_prompts)}</td>
      <td class="px-4 py-3 text-right kpi-num">${fmt(x.tool_calls)}</td>
      <td class="px-4 py-3 text-right kpi-num">${fmtK(x.total_tokens)}</td>
      <td class="px-4 py-3 text-right kpi-num">${usd(x.cost)}</td>
      <td class="px-4 py-3 text-xs text-slate-400">${fmtDay(x.first)} → ${fmtDay(x.last)}</td>
    </tr>`).join("");
}

// ------------------------------------------------------------- Línea tiempo
let gran = "day";
$$("#gran-toggle .gran").forEach((b) =>
  b.addEventListener("click", () => {
    gran = b.dataset.g;
    $$("#gran-toggle .gran").forEach((x) => x.classList.toggle("tab-active", x === b));
    loadTimeline();
  }));

async function loadTimeline() {
  const d = await api("/api/timeseries/" + gran);
  $("#timeline-body").innerHTML = d.slice().reverse().map((x) => `
    <tr class="border-t border-[#1f2735] hover:bg-[#0f1420]">
      <td class="px-4 py-3 font-medium">${x.period}</td>
      <td class="px-4 py-3 text-right kpi-num">${fmt(x.sessions)}</td>
      <td class="px-4 py-3 text-right kpi-num">${fmt(x.user_prompts)}</td>
      <td class="px-4 py-3 text-right kpi-num">${fmt(x.tool_calls)}</td>
      <td class="px-4 py-3 text-right kpi-num">${fmtK(x.total_tokens)}</td>
      <td class="px-4 py-3 text-right kpi-num">${usd(x.cost)}</td>
      <td class="px-4 py-3 text-xs text-slate-400">${x.projects.join(", ")}</td>
    </tr>`).join("");

  const ctx = $("#chart-timeline");
  if (charts.timeline) charts.timeline.destroy();
  charts.timeline = new Chart(ctx, {
    data: {
      labels: d.map((x) => x.period),
      datasets: [
        { type: "bar", label: "Tokens entrada+cache", yAxisID: "y", borderRadius: 3,
          data: d.map((x) => x.input_tokens + x.cache_write_tokens + x.cache_read_tokens),
          backgroundColor: "#2563eb" },
        { type: "bar", label: "Tokens salida", yAxisID: "y",
          data: d.map((x) => x.output_tokens), backgroundColor: "#16a34a", borderRadius: 3 },
        { type: "line", label: "Costo USD", yAxisID: "y1", data: d.map((x) => x.cost),
          borderColor: "#f59e0b", backgroundColor: "#f59e0b", tension: .3, pointRadius: 2 },
      ],
    },
    options: baseOpts({
      scales: {
        x: { stacked: true, ...gridX() },
        y: { stacked: true, ...gridY(), ticks: { ...tickColor(), callback: fmtK } },
        y1: { position: "right", grid: { drawOnChartArea: false },
              ticks: { ...tickColor(), callback: (v) => "$" + v } },
      },
    }),
  });
}

// ------------------------------------------------------- Sesiones ACTIVAS
function startActivePolling() {
  refreshActive();
  if (!ACTIVE_TIMER) ACTIVE_TIMER = setInterval(refreshActive, 4000);
}
function stopActivePolling() {
  if (ACTIVE_TIMER) { clearInterval(ACTIVE_TIMER); ACTIVE_TIMER = null; }
}

async function refreshActive() {
  let active = [];
  try { active = await api("/api/active"); } catch { return; }
  // indicador global en el header
  const busy = active.filter((a) => a.status === "busy").length;
  $("#live-indicator").innerHTML = active.length
    ? `<span class="w-2 h-2 rounded-full inline-block ${busy ? "dot-busy" : "dot-idle"}"></span>
       <span>${active.length} activa${active.length > 1 ? "s" : ""}${busy ? ` · ${busy} trabajando` : ""}</span>`
    : "";

  const box = $("#active-block");
  if (!active.length) { box.innerHTML = ""; return; }
  box.innerHTML = `
    <div class="flex items-center gap-2 mb-2">
      <span class="w-2 h-2 rounded-full ${busy ? "dot-busy" : "dot-idle"}"></span>
      <h3 class="font-medium">Sesiones activas ahora <span class="text-slate-500 text-sm">(en vivo · refresca cada 4s)</span></h3>
    </div>
    <div class="grid md:grid-cols-2 gap-3">
      ${active.map(activeCard).join("")}
    </div>`;
  $$("#active-block [data-sid]").forEach((el) =>
    el.addEventListener("click", () => openSession(el.dataset.sid)));
}

function activeCard(a) {
  const busy = a.status === "busy";
  const models = (a.models || []).map((m) => m.replace("claude-", "")).join(", ");
  const tools = (a.last_tools || []).map((t) => `${t[0]}×${t[1]}`).join("  ");
  return `<div data-sid="${a.session_id}" class="card p-4 cursor-pointer border-${busy ? "green" : "amber"}-600/40 hover:border-blue-500 transition">
    <div class="flex items-center gap-2">
      <span class="w-2 h-2 rounded-full ${busy ? "dot-busy" : "dot-idle"}"></span>
      <span class="text-xs font-medium ${busy ? "text-green-400" : "text-amber-400"}">${busy ? "TRABAJANDO" : "Abierta (idle)"}</span>
      <span class="pill px-2 py-0.5 rounded text-[11px] text-blue-300">${a.project}</span>
      <span class="text-[11px] text-slate-500 ml-auto">pid ${a.pid} · ${fmtDate(a.updated_at)}</span>
    </div>
    <h4 class="font-medium mt-2 leading-snug line-clamp-2">${escapeHtml(a.title || a.name || a.first_prompt || "(sin título)")}</h4>
    <div class="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-[11px] text-slate-400 kpi-num">
      <span>💬 ${fmt(a.user_prompts)} prompts</span>
      <span>🔧 ${fmt(a.tool_calls)} tools</span>
      <span>🎫 ${fmtK(a.total_tokens)} tok</span>
      <span>💵 ${usd(a.cost)}</span>
      ${models ? `<span>🤖 ${models}</span>` : ""}
    </div>
    ${tools ? `<div class="mt-1 text-[11px] text-cyan-300/70">🔧 ${escapeHtml(tools)}</div>` : ""}
  </div>`;
}

// ----------------------------------------------------------------- Sesiones
async function loadSessions() {
  SESSIONS = await api("/api/sessions");
  const projs = [...new Set(SESSIONS.map((s) => s.project))].sort();
  $("#session-project").innerHTML =
    `<option value="">Todos los proyectos</option>` +
    projs.map((p) => `<option value="${p}">${p}</option>`).join("");
  renderSessions();
}

function renderSessions() {
  const q = $("#session-search").value.toLowerCase().trim();
  const proj = $("#session-project").value;
  const hidePurged = $("#hide-purged").checked;
  const list = SESSIONS.filter((s) => {
    if (proj && s.project !== proj) return false;
    if (hidePurged && s.is_pruned) return false;
    if (!q) return true;
    return (s.title + " " + s.project + " " + s.first_prompt).toLowerCase().includes(q);
  });
  $("#sessions-list").innerHTML = list.map(sessionCard).join("") ||
    `<div class="text-slate-500 text-sm">Sin resultados.</div>`;
  $$("#sessions-list [data-sid]").forEach((el) =>
    el.addEventListener("click", () => openSession(el.dataset.sid)));
}
$("#session-search").addEventListener("input", renderSessions);
$("#session-project").addEventListener("change", renderSessions);
$("#hide-purged").addEventListener("change", renderSessions);

function sessionCard(s) {
  const models = s.models.map((m) => m.replace("claude-", "")).join(", ") || "—";
  const cmds = s.commands.map((c) => `<span class="pill px-1.5 py-0.5 rounded text-[11px]">/${c[0]}</span>`).join(" ");
  const purged = s.is_pruned;
  return `<div data-sid="${s.session_id}" class="card p-4 cursor-pointer hover:border-blue-500 transition ${purged ? "purged" : ""}">
    <div class="flex items-start gap-2">
      <span class="pill px-2 py-0.5 rounded text-[11px] text-blue-300 shrink-0">${s.project}</span>
      ${purged ? `<span class="px-2 py-0.5 rounded text-[11px] bg-amber-900/40 text-amber-300 shrink-0" title="Transcripción borrada por Claude Code; recuperada parcialmente de history.jsonl">purgada</span>` : ""}
      <span class="text-[11px] text-slate-500 ml-auto shrink-0">${fmtDate(s.start)}</span>
    </div>
    <h3 class="font-medium mt-2 leading-snug line-clamp-2">${escapeHtml(s.title)}</h3>
    <p class="text-xs text-slate-400 mt-1 line-clamp-2">${escapeHtml(s.first_prompt || "")}</p>
    <div class="flex flex-wrap gap-x-4 gap-y-1 mt-3 text-[11px] text-slate-400 kpi-num">
      <span>💬 ${fmt(s.user_prompts)} prompts</span>
      ${purged ? "" : `<span>🔧 ${fmt(s.tool_calls)} tools</span>
      <span>🎫 ${fmtK(s.total_tokens)} tok</span>
      <span>💵 ${usd(s.cost)}</span>
      <span>⏱ ${s.duration_min} min</span>
      <span>📄 ${s.files_touched_count} archivos</span>`}
    </div>
    ${purged ? "" : `<div class="mt-2 text-[11px] text-slate-500">🤖 ${models}</div>`}
    ${cmds ? `<div class="mt-2 flex flex-wrap gap-1">${cmds}</div>` : ""}
  </div>`;
}

// ------------------------------------------------------------------- Modal
let MODAL = null;
async function openSession(sid) {
  MODAL = await api("/api/sessions/" + sid);
  $("#m-title").textContent = MODAL.title + (MODAL.is_pruned ? "  ·  (purgada)" : "");
  $("#m-meta").textContent =
    `${MODAL.project}  ·  ${fmtDate(MODAL.start)} → ${fmtDate(MODAL.end)}  ·  ${MODAL.duration_min} min  ·  ${MODAL.models.map((m)=>m.replace("claude-","")).join(", ")}`;
  $("#modal").classList.remove("hidden");
  switchMTab("recap");
}
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal-close").addEventListener("click", closeModal);
$("#modal-bg").addEventListener("click", closeModal);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

$$(".mtab").forEach((b) => b.addEventListener("click", () => switchMTab(b.dataset.mtab)));
function switchMTab(tab) {
  $$(".mtab").forEach((b) => b.classList.toggle("tab-active", b.dataset.mtab === tab));
  if (tab === "recap") renderRecap();
  else if (tab === "transcript") renderTranscript();
  else renderStats();
}

function renderRecap() {
  const r = MODAL.recap;
  const ai = MODAL.ai_recap;
  $("#modal-body").innerHTML = `
    <div class="card p-4 mb-3">
      <div class="flex items-center justify-between mb-2">
        <div class="text-xs text-slate-400">Recap con IA (ejecuta <code>claude --resume</code>)</div>
        <button id="gen-recap" class="pill px-3 py-1.5 rounded-lg text-sm hover:bg-[#222c3e]">
          ${ai ? "↻ Regenerar" : "✨ Generar recap con IA"}
        </button>
      </div>
      <div id="ai-recap-body" class="text-sm text-slate-200">
        ${ai ? `<pre class="txt">${escapeHtml(ai.recap)}</pre>
                <div class="text-[11px] text-slate-500 mt-2">generado ${fmtDate(ai.generated_at)}</div>`
              : `<div class="text-slate-500 text-sm">Aún no generado. El botón reanuda la sesión con el CLI de
                 Claude y produce un /recap (consume tokens de tu suscripción/clave).</div>`}
      </div>
    </div>
    <div class="card p-4 mb-3">
      <div class="text-xs text-slate-400 mb-1">Objetivo (primer prompt)</div>
      <pre class="txt text-slate-200">${escapeHtml(r.objective || "—")}</pre>
    </div>
    <div class="card p-4 mb-3">
      <div class="text-xs text-slate-400 mb-2">Qué se hizo (heurístico)</div>
      <ul class="list-disc pl-5 space-y-1 text-sm text-slate-200">
        ${(r.bullets || []).map((b) => `<li>${escapeHtml(b)}</li>`).join("") || "<li>—</li>"}
      </ul>
    </div>
    <div class="card p-4">
      <div class="text-xs text-slate-400 mb-2">Instrucciones del usuario en la sesión</div>
      <div class="space-y-2">
        ${(r.prompt_samples || []).map((p) =>
          `<div class="msg-user pl-3 text-sm text-slate-300">${escapeHtml(p)}</div>`).join("") || "—"}
      </div>
    </div>`;
  $("#gen-recap").addEventListener("click", genRecap);
}

async function genRecap() {
  const btn = $("#gen-recap");
  const body = $("#ai-recap-body");
  btn.disabled = true;
  btn.innerHTML = `<span class="inline-block spin">↻</span> generando… (puede tardar)`;
  body.innerHTML = `<div class="text-slate-400 text-sm">Reanudando la sesión con <code>claude --resume</code>…</div>`;
  try {
    const res = await api(`/api/sessions/${MODAL.session_id}/recap?force=true`, { method: "POST" });
    if (res.ok) {
      body.innerHTML = `<pre class="txt">${escapeHtml(res.recap)}</pre>
        <div class="text-[11px] text-slate-500 mt-2">${res.cached ? "cacheado" : "recién generado"}</div>`;
      btn.innerHTML = "↻ Regenerar";
    } else {
      body.innerHTML = `<div class="text-red-300 text-sm">${escapeHtml(res.error || "Error")}</div>`;
      btn.innerHTML = "✨ Reintentar";
    }
  } catch (e) {
    body.innerHTML = `<div class="text-red-300 text-sm">Error: ${escapeHtml(e.message)}</div>`;
    btn.innerHTML = "✨ Reintentar";
  }
  btn.disabled = false;
}

function renderTranscript() {
  const t = MODAL.transcript || [];
  const head = MODAL.is_pruned
    ? `<div class="text-xs text-amber-300 mb-3">⚠️ Sesión purgada: sólo se conservan los prompts del usuario (history.jsonl).</div>`
    : "";
  $("#modal-body").innerHTML = head + (t.map((m) => {
    if (m.kind === "tool_result") return "";
    const cls = m.role === "user" ? "msg-user" : "msg-assistant";
    const who = m.kind === "command" ? `Comando /${m.command}`
      : m.role === "user" ? "Usuario" : "Claude" + (m.model ? ` · ${m.model.replace("claude-","")}` : "");
    const tools = (m.tool_uses || []).map((tu) =>
      `<div class="text-[11px] text-cyan-300/80 pl-3">🔧 ${escapeHtml(tu.name)} <span class="text-slate-500">${escapeHtml(tu.detail)}</span></div>`).join("");
    const txt = m.text ? `<pre class="txt mt-1">${escapeHtml(m.text)}</pre>` : "";
    if (!txt && !tools) return "";
    return `<div class="${cls} ${m.kind === "meta" ? "msg-meta" : ""} pl-3 mb-3">
      <div class="text-[11px] text-slate-400">${who} <span class="text-slate-600">${m.timestamp ? fmtDate(m.timestamp) : ""}</span></div>
      ${txt}${tools}
    </div>`;
  }).join("") || `<div class="text-slate-500 text-sm">Sin transcripción.</div>`);
}

function renderStats() {
  const s = MODAL;
  const tools = (s.top_tools || []).map((t) =>
    `<div class="flex justify-between text-sm"><span>${escapeHtml(t[0])}</span><span class="kpi-num text-slate-400">${t[1]}</span></div>`).join("");
  const files = (s.files_touched || []).map((f) =>
    `<div class="text-xs text-slate-400 truncate">${escapeHtml(f)}</div>`).join("");
  $("#modal-body").innerHTML = `
    <div class="grid grid-cols-2 gap-3 mb-3">
      ${miniKpi("Prompts", fmt(s.user_prompts))}
      ${miniKpi("Respuestas", fmt(s.assistant_msgs))}
      ${miniKpi("Tool calls", fmt(s.tool_calls))}
      ${miniKpi("Archivos tocados", fmt(s.files_touched_count))}
      ${miniKpi("Tokens entrada", fmtK(s.input_tokens))}
      ${miniKpi("Tokens salida", fmtK(s.output_tokens))}
      ${miniKpi("Cache write", fmtK(s.cache_write_tokens))}
      ${miniKpi("Cache read", fmtK(s.cache_read_tokens))}
      ${miniKpi("Tokens totales", fmtK(s.total_tokens))}
      ${miniKpi("Costo estimado", usd(s.cost))}
    </div>
    <div class="card p-4 mb-3"><div class="text-xs text-slate-400 mb-2">Herramientas</div>${tools || "—"}</div>
    <div class="card p-4"><div class="text-xs text-slate-400 mb-2">Archivos (${s.files_touched_count})</div>${files || "—"}</div>`;
}
const miniKpi = (l, v) =>
  `<div class="card p-3"><div class="text-[11px] text-slate-400">${l}</div><div class="text-lg font-semibold kpi-num">${v}</div></div>`;

// --------------------------------------------------------- MCP & Servicio
async function loadMcp() {
  const info = await api("/api/mcp/info");
  $("#mcp-cmd").textContent = info.install_command;
  $("#copy-mcp").onclick = () => {
    navigator.clipboard.writeText(info.install_command).then(() => {
      $("#copy-mcp").textContent = "✓ Copiado";
      setTimeout(() => ($("#copy-mcp").textContent = "Copiar"), 1500);
    });
  };
  $("#mcp-tools").innerHTML = info.tools.map((t) =>
    `<div class="pill rounded px-2 py-1"><code class="text-blue-300">${escapeHtml(t[0])}</code> — ${escapeHtml(t[1])}</div>`).join("");
  $("#mcp-examples").innerHTML = info.example_prompts.map((p) =>
    `<span class="pill rounded-lg px-2.5 py-1 text-xs text-slate-300">${escapeHtml(p)}</span>`).join("");
  await refreshService();
}

async function refreshService() {
  const st = await api("/api/service/status");
  const badge = (ok, txt, color) =>
    `<span class="px-2 py-0.5 rounded text-[11px] bg-${color}-900/40 text-${color}-300">${txt}</span>`;
  if (!st.supported) {
    $("#service-status").innerHTML = badge(false, "No soportado (sólo macOS)", "amber");
  } else if (st.loaded) {
    $("#service-status").innerHTML = badge(true, "● Activo (corriendo y al iniciar sesión)", "green");
  } else if (st.installed) {
    $("#service-status").innerHTML = badge(false, "Instalado pero no cargado", "amber");
  } else {
    $("#service-status").innerHTML = badge(false, "○ No instalado", "slate");
  }
  $("#service-msg").innerHTML =
    `<div>Servicio: <code>${st.label}</code></div>
     <div>plist: <code>${st.plist_path}</code></div>
     <div>log: <code>${st.log_path}</code></div>
     ${!st.uv_found ? `<div class="text-amber-400">⚠️ no se encontró uv</div>` : ""}`;
}

async function svcAction(path) {
  $("#service-msg").innerHTML = `<span class="text-slate-400">…</span>`;
  try {
    const r = await api(path, { method: "POST" });
    $("#service-msg").innerHTML = r.ok
      ? `<span class="text-green-300">✓ ${escapeHtml(r.note || "Listo")}</span>`
      : `<span class="text-red-300">✗ ${escapeHtml(r.error || "Error")}</span>`;
  } catch (e) {
    $("#service-msg").innerHTML = `<span class="text-red-300">✗ ${escapeHtml(e.message)}</span>`;
  }
  await refreshService();
}
$("#svc-install").addEventListener("click", () => svcAction("/api/service/install"));
$("#svc-uninstall").addEventListener("click", () => svcAction("/api/service/uninstall"));
$("#svc-refresh").addEventListener("click", refreshService);

// ------------------------------------------------------------- Chart helpers
const tickColor = () => ({ color: "#7c899e", font: { size: 10 } });
const gridX = () => ({ grid: { color: "#1a2230" }, ticks: tickColor() });
const gridY = () => ({ grid: { color: "#1a2230" } });
function baseOpts(extra) {
  return {
    responsive: true, maintainAspectRatio: true,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { labels: { color: "#cbd5e1", font: { size: 11 } } } },
    ...extra,
  };
}
function drawDoughnut(id, labels, data) {
  const ctx = $("#" + id);
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(ctx, {
    type: "doughnut",
    data: { labels, datasets: [{ data, backgroundColor: PALETTE, borderColor: "#121723", borderWidth: 2 }] },
    options: { responsive: true, plugins: { legend: { position: "bottom", labels: { color: "#cbd5e1", font: { size: 10 }, boxWidth: 12 } } } },
  });
}
function drawBar(id, labels, data, label, horizontal) {
  const ctx = $("#" + id);
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets: [{ label, data, backgroundColor: PALETTE, borderRadius: 4 }] },
    options: {
      indexAxis: horizontal ? "y" : "x",
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { ...gridX(), ticks: { ...tickColor(), callback: (v) => (label === "USD" ? "$" + v : fmtK(v)) } },
        y: { grid: { color: "#1a2230" }, ticks: tickColor() },
      },
    },
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --------------------------------------------------------------------- Init
$("#reload").addEventListener("click", async () => {
  $("#reload").textContent = "…";
  await api("/api/reload", { method: "POST" });
  await boot();
  $("#reload").textContent = "↻ Sync";
});

async function boot() {
  await loadOverview();
  await loadProjects();
  await loadTimeline();
  await loadSessions();
}
boot().catch((e) => {
  document.body.insertAdjacentHTML("afterbegin",
    `<div class="bg-red-900/60 text-red-100 p-3 text-sm">Error cargando datos: ${e.message}</div>`);
});
