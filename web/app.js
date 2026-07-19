/* Devy — web chat. A thin client of the proxy API (same as the Go `ask` TUI):
 * POST /v1/chat and consume the SSE event stream over fetch. No app logic here —
 * the proxy owns the agent loop. nginx serves this and reverse-proxies /v1 +
 * /healthz to the proxy, so everything is same-origin.
 *
 * Event contract (proxy/harness.py + proxy/app.py):
 *   session {session_id} · delta {text} · tool_call {name, arguments}
 *   tools_found {names[]} · tool_result {name, ok, preview}
 *   notice {message} · done {iterations, usage, text} · error {message}
 */

const screen = document.getElementById("screen");
const input = document.getElementById("input");
const composer = document.getElementById("composer");
const sendBtn = document.getElementById("send-btn");
const tierSelect = document.getElementById("tier-select");
const statusDot = document.getElementById("status-dot");
const connLabel = document.getElementById("conn-label");
const newBtn = document.getElementById("new-btn");
const histBtn = document.getElementById("hist-btn");
const copyBtn = document.getElementById("copy-btn");
const drawer = document.getElementById("drawer");
const drawerClose = document.getElementById("drawer-close");
const drawerScrim = document.getElementById("drawer-scrim");
const histList = document.getElementById("hist-list");
const identInput = document.getElementById("ident-input");

const state = {
  sessionId: null, tier: "", tiers: [], busy: false,
  history: [], histIdx: -1,  // input recall (up/down arrows)
  transcript: [],            // {role, content} for copy-as-markdown
};

/* ---------- identity (honor-system; the auth seam) ----------
 * History is scoped by user. For now identity is just a name kept in
 * localStorage and sent as X-User-Id. A real provider (Google auth, or a
 * Cloudflare+Okta JWT carrying an email) slots in by changing authHeaders()
 * to read that token/claim — nothing else in the app needs to know. */
const USER_KEY = "devy_user";
const getUserId = () => (localStorage.getItem(USER_KEY) || "").trim();
function setUserId(v) {
  v = (v || "").trim();
  if (v) localStorage.setItem(USER_KEY, v); else localStorage.removeItem(USER_KEY);
}
function authHeaders() {
  const u = getUserId();
  const h = u ? { "X-User-Id": u } : {};
  // Client IANA timezone → server does DST-correct local-time conversion (never the model).
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (tz) h["X-Client-TZ"] = tz;
  } catch (_) {}
  return h;
}

/* ---------- DOM helpers ---------- */
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

/* Inline Lucide icons (https://lucide.dev, ISC) — no emoji in the menus, and no
 * runtime dependency: just the path data, drawn with currentColor. */
// Each value is either an array of path `d` strings, or a raw inner-SVG string
// (verbatim Lucide body — for icons that use circles/rects/lines, not just paths).
const ICON_PATHS = {
  pencil: [
    "M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z",
    "m15 5 4 4",
  ],
  trash: ["M3 6h18", "M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6", "M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2", "M10 11v6", "M14 11v6"],
  check: ["M20 6 9 17l-5-5"],
  x: ["M18 6 6 18", "M6 6l12 12"],
  // Semantic tool-trail icons (Lucide, https://lucide.dev, ISC).
  wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  server: '<rect width="20" height="8" x="2" y="2" rx="2"/><rect width="20" height="8" x="2" y="14" rx="2"/><line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/>',
  book: '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
  history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  git: '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
};
function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const spec = ICON_PATHS[name];
  if (typeof spec === "string") {
    svg.innerHTML = spec;  // raw Lucide body (our own static markup)
  } else {
    (spec || []).forEach((d) => {
      const p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      svg.appendChild(p);
    });
  }
  return svg;
}

// Map a tool name to a semantic icon, LangSmith-style (tool vs KB vs memory vs …).
function iconForTool(name) {
  if (name === "find_tools") return "search";
  if (/^host_|^run_host|^host_details/.test(name)) return "server";
  if (name === "search_knowledge" || name === "memory_index") return "book";
  if (/^recall_|^memory_add/.test(name)) return "history";
  if (/^repo_/.test(name)) return "git";
  if (name === "web_search") return "globe";
  if (name === "correlate_timeline") return "activity";
  return "wrench";
}
const atBottom = () => screen.scrollHeight - screen.scrollTop - screen.clientHeight < 80;
const scroll = () => { screen.scrollTop = screen.scrollHeight; };
function note(html, cls) {
  const stick = atBottom();
  const n = el("div", "note " + (cls || ""));
  n.innerHTML = window.DOMPurify.sanitize(html);
  screen.appendChild(n);
  if (stick) scroll();
  return n;
}

function greet() {
  const ok = statusDot.classList.contains("ok");
  note(
    "Hi, I'm <strong>Devy</strong> — your DevOps &amp; SRE co-pilot. Ask me about your " +
    "systems and I'll discover the right tools, pull live data and runbooks, and explain " +
    "what's going on — grounded in real data, not guesses." +
    (ok ? "" : "<br><br><span class='err'>Proxy unreachable — start it with <code>docker compose up -d</code>.</span>"),
    "welcome"
  );
}

// Start a fresh chat: clear the view outright (not a scrollback divider) and reset state.
function newConversation() {
  state.sessionId = null;
  state.transcript = [];
  screen.replaceChildren();
  greet();
  input.focus();
}

/* ---------- markdown / mermaid ---------- */
let mermaidReady = false;
function initMermaid() {
  if (mermaidReady || !window.mermaid) return;
  window.mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
  mermaidReady = true;
}
function renderMarkdown(container, md) {
  const rawHtml = window.marked.parse(md, { breaks: true, gfm: true });
  container.innerHTML = window.DOMPurify.sanitize(rawHtml);
  container.querySelectorAll("code.language-mermaid").forEach((code) => {
    const holder = el("div", "mermaid");
    holder.textContent = code.textContent;
    code.closest("pre").replaceWith(holder);
  });
  container.querySelectorAll("pre code").forEach((b) => {
    try { window.hljs.highlightElement(b); } catch (_) {}
  });
  if (container.querySelector(".mermaid")) {
    initMermaid();
    try { window.mermaid.run({ nodes: container.querySelectorAll(".mermaid") }); } catch (_) {}
  }
}

/* ---------- a conversation turn ---------- */
function msg(kind, label) {
  const stick = atBottom();
  const m = el("div", "msg " + kind);
  const lbl = el("div", "msg-label");
  if (kind === "devy") lbl.appendChild(el("span", "tick", "◉"));
  lbl.appendChild(document.createTextNode(label));
  const body = el("div", "msg-body");
  m.append(lbl, body);
  screen.appendChild(m);
  if (stick) scroll();
  return { el: m, body };
}

function addCopy(msgEl, getText) {
  const b = el("button", "msg-copy", "copy");
  b.title = "copy message";
  b.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(getText());
      b.textContent = "copied";
      setTimeout(() => { b.textContent = "copy"; }, 1200);
    } catch (_) { b.textContent = "!"; }
  });
  msgEl.querySelector(".msg-label").appendChild(b);
}

/* Render one stored message (used when loading a past conversation). */
function renderMessage(role, content) {
  if (role === "user") {
    const u = msg("user", "you");
    u.body.textContent = content;
    addCopy(u.el, () => content);
  } else {
    const d = msg("devy", "Devy");
    const answer = el("div", "answer");
    renderMarkdown(answer, content || "");
    d.body.appendChild(answer);
    addCopy(d.el, () => content);
  }
}

function startTurn(promptText) {
  const u = msg("user", "you");
  u.body.textContent = promptText;
  addCopy(u.el, () => promptText);
  state.transcript.push({ role: "user", content: promptText });
  const turn = msg("devy", "Devy");
  const body = turn.body;
  const tools = el("div", "tools");
  tools.style.display = "none";
  const head = el("div", "tools-head");
  const gear = el("span", "gear");
  gear.appendChild(icon("wrench"));
  head.append(gear, el("span", null, "tools"));
  tools.appendChild(head);
  const stream = el("div", "stream");
  body.append(tools, stream);
  return { msgEl: turn.el, body, tools, stream, toolNodes: {} };
}

function addTool(ctx, name, detail) {
  ctx.tools.style.display = "";
  const node = el("div", "tool");
  const ic = icon(iconForTool(name));
  ic.classList.add("ticon");
  node.appendChild(ic);
  const tn = el("span", "tname", name);
  node.appendChild(tn);
  if (detail) node.appendChild(document.createTextNode("  " + detail));
  ctx.tools.appendChild(node);
  ctx.toolNodes[name] = node;
  if (atBottom()) scroll();
  return node;
}

/* ---------- the stream ---------- */
async function send(message) {
  if (state.busy) return;
  setBusy(true);
  const ctx = startTurn(message);
  let liveText = "";

  try {
    const resp = await fetch("/v1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ message, session_id: state.sessionId, tier: state.tier || undefined }),
    });
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
    setStatus("ok");

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // sse-starlette separates events with \r\n\r\n — normalize to \n.
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

      let sep;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const evt = parseFrame(frame);
        if (!evt) continue;

        if (evt.type === "session") {
          state.sessionId = evt.data.session_id;
        } else if (evt.type === "delta") {
          liveText += evt.data.text || "";
          ctx.stream.textContent = liveText;
          if (atBottom()) scroll();
        } else if (evt.type === "tool_call") {
          // Pre-tool narration is dropped (mirrors the TUI); the final answer is
          // whatever streams after the last tool round.
          liveText = "";
          ctx.stream.textContent = "";
          const a = evt.data.arguments || {};
          const detail = evt.data.name === "find_tools"
            ? (a.intent ? `“${a.intent}”` : "")
            : Object.keys(a).length ? compactArgs(a) : "";
          addTool(ctx, evt.data.name, detail);
        } else if (evt.type === "tools_found") {
          const node = ctx.toolNodes["find_tools"];
          if (node) node.appendChild(el("span", "found", "  → " + (evt.data.names || []).join(", ")));
        } else if (evt.type === "tool_result") {
          const node = ctx.toolNodes[evt.data.name];
          if (node) {
            node.classList.add(evt.data.ok ? "ok" : "fail");
            const det = el("details");
            const sum = el("summary");
            const chev = icon("chevron");
            chev.classList.add("disc");
            sum.append(chev, document.createTextNode(evt.data.ok ? "result" : "error"));
            det.append(sum, el("pre", null, evt.data.preview || ""));
            node.appendChild(det);
          }
        } else if (evt.type === "notice") {
          // Subtle operator note (e.g. answered via a backup model) — dim, in the trail.
          ctx.body.appendChild(el("div", "notice", evt.data.message || ""));
          if (atBottom()) scroll();
        } else if (evt.type === "done") {
          ctx.stream.classList.add("done");
          const finalText = evt.data.text || liveText;
          if (finalText.trim()) {
            const answer = el("div", "answer");
            renderMarkdown(answer, finalText);
            ctx.stream.replaceWith(answer);
            addCopy(ctx.msgEl, () => finalText);
            state.transcript.push({ role: "assistant", content: finalText });
          }
          const u = evt.data.usage || {};
          const tok = u.total_tokens ? ` · ${u.total_tokens} tokens` : "";
          ctx.body.appendChild(el("div", "meta", `${evt.data.iterations} step(s)${tok}`));
          if (atBottom()) scroll();
        } else if (evt.type === "error") {
          ctx.stream.classList.add("done");
          ctx.body.appendChild(el("div", "err", "✖ " + (evt.data.message || "stream error")));
        }
      }
    }
  } catch (err) {
    setStatus("err");
    ctx.stream.classList.add("done");
    ctx.body.appendChild(el("div", "err", "✖ " + err.message + " — is the proxy running?"));
  } finally {
    setBusy(false);
    input.focus();
  }
}

function compactArgs(a) {
  // short, human-ish summary of tool args for the trail
  const parts = Object.entries(a).map(([k, v]) => {
    if (Array.isArray(v)) return `${k}=${v.length} item${v.length === 1 ? "" : "s"}`;
    if (v && typeof v === "object") return `${k}={…}`;
    return `${k}=${String(v).slice(0, 40)}`;
  });
  return parts.join(" ").slice(0, 90);
}

function parseFrame(frame) {
  let event = "message";
  const dataLines = [];
  for (const raw of frame.split("\n")) {
    if (raw.startsWith("event:")) event = raw.slice(6).trim();
    else if (raw.startsWith("data:")) dataLines.push(raw.slice(5).trim());
  }
  if (!dataLines.length) return null;
  let data;
  try { data = JSON.parse(dataLines.join("\n")); } catch (_) { data = {}; }
  return { type: data.type || event, data };
}

/* ---------- proxy meta ---------- */
function setStatus(s) {
  statusDot.className = "dot" + (s ? " " + s : "");
  connLabel.textContent = s === "ok" ? "connected" : s === "err" ? "offline" : "connecting";
}
function setBusy(b) {
  state.busy = b;
  composer.classList.toggle("busy", b);
  sendBtn.disabled = b;
  statusDot.classList.toggle("pulse", b);
}
async function loadTiers() {
  try {
    state.tiers = await (await fetch("/v1/tiers")).json();
    tierSelect.innerHTML = "";
    state.tiers.forEach((t, i) => {
      const opt = el("option", null, t.label);
      opt.value = t.name;
      tierSelect.appendChild(opt);
      if (i === 0 && !state.tier) state.tier = t.name;
    });
    const h = await (await fetch("/healthz")).json();
    if (h.default_tier && state.tiers.some((t) => t.name === h.default_tier)) state.tier = h.default_tier;
    tierSelect.value = state.tier;
    setStatus("ok");
  } catch (_) {
    setStatus("err");
  }
}

async function ensureTools() {
  if (state.tools) return;
  try {
    state.tools = await (await fetch("/v1/tools")).json();
  } catch (_) { state.tools = []; }
  state.toolNames = new Set((state.tools || []).map((t) => t.name));
}

/* ---------- slash commands ----------
 * Slash commands are UI commands handled here. They are NOT how you run Devy's
 * *tools* — those are invoked by the agent when you ask in plain language. */
async function handleCommand(line) {
  const [cmd, ...rest] = line.slice(1).split(/\s+/);
  const arg = rest.join(" ").trim();
  switch (cmd) {
    case "help":
      note(
        "<strong>commands</strong> <span style='opacity:.6'>(UI commands — to use Devy's tools, just ask in plain language)</span><br>" +
        "<code>/model &lt;tier&gt;</code> switch model tier · <code>/models</code> list tiers · " +
        "<code>/tools</code> what Devy can do · <code>/new</code> fresh conversation · " +
        "<code>/clear</code> clear screen · <code>/help</code> this",
        "welcome"
      );
      break;
    case "models":
      note(state.tiers.map((t) => `${t.name === state.tier ? "● " : "  "}${t.name} — ${t.label}`).join("<br>") || "(none)");
      break;
    case "model":
      if (!arg) { note("usage: <code>/model &lt;tier&gt;</code> — see <code>/models</code>", "err"); break; }
      if (state.tiers.some((t) => t.name === arg)) { state.tier = arg; tierSelect.value = arg; note(`tier → <code>${arg}</code>`); }
      else note(`unknown tier '${arg}'. try <code>/models</code>`, "err");
      break;
    case "tools": {
      await ensureTools();
      const list = (state.tools && state.tools.length)
        ? state.tools.map((t) => `${t.name} <span style="opacity:.6">[${t.category}/${t.safety_tier}]</span>`).join("<br>")
        : "(none registered yet)";
      note(
        "<strong>What Devy can do</strong> — these are the agent's <em>tools</em>, not commands. " +
        "You don't call them directly; just describe what you want and Devy discovers and runs the right one " +
        "(e.g. ask <em>“check host disk usage”</em>).<br><br>" + list,
        "welcome"
      );
      break;
    }
    case "new": newConversation(); break;
    case "clear": screen.innerHTML = ""; break;
    default: {
      await ensureTools();
      if (state.toolNames && state.toolNames.has(cmd)) {
        note(
          `<code>${cmd}</code> is one of Devy's <strong>tools</strong>, not a slash command — you don't run it directly. ` +
          `Just ask in plain language and Devy will use it. e.g. <em>“use ${cmd.replace(/_/g, " ")}”</em> or describe what you want to find out.`,
          "welcome"
        );
      } else {
        note(`unknown command '/${cmd}'. try <code>/help</code>`, "err");
      }
    }
  }
}

/* ---------- input handling ---------- */
function submit() {
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = "";
  autosize();
  state.history.push(text);
  state.histIdx = state.history.length;
  if (text.startsWith("/")) { handleCommand(text); return; }
  send(text);
}
function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px";
}
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  else if (e.key === "ArrowUp" && !input.value.includes("\n") && state.histIdx > 0) {
    state.histIdx--; input.value = state.history[state.histIdx]; autosize();
  } else if (e.key === "ArrowDown" && state.histIdx < state.history.length - 1) {
    state.histIdx++; input.value = state.history[state.histIdx]; autosize();
  }
});
composer.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
tierSelect.addEventListener("change", () => { state.tier = tierSelect.value; });
newBtn.addEventListener("click", newConversation);

/* ---------- history slide-out ---------- */
function relTime(iso) {
  const t = Date.parse(iso);
  if (isNaN(t)) return "";
  const s = Math.max(1, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60); if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60); if (h < 24) return h + "h ago";
  const d = Math.floor(h / 24); if (d < 7) return d + "d ago";
  return new Date(t).toLocaleDateString();
}

function openDrawer() {
  identInput.value = getUserId();
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  drawerScrim.hidden = false;
  loadHistory();
}
function closeDrawer() {
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  drawerScrim.hidden = true;
}

async function loadHistory() {
  histList.innerHTML = "";
  if (!getUserId()) {
    histList.appendChild(el("div", "hist-empty", "Set a name above to save and recall your conversations."));
    return;
  }
  let rows = [];
  try {
    rows = await (await fetch("/v1/sessions", { headers: authHeaders() })).json();
  } catch (_) {
    histList.appendChild(el("div", "hist-empty", "Couldn't load history — is the proxy running?"));
    return;
  }
  if (!rows.length) {
    histList.appendChild(el("div", "hist-empty", "No saved conversations yet."));
    return;
  }
  rows.forEach((r) => histList.appendChild(histItem(r)));
}

function histItem(r) {
  const row = el("div", "hist-item" + (r.id === state.sessionId ? " current" : ""));
  const main = el("button", "hist-main");
  main.appendChild(el("div", "hist-title", r.title || r.preview || "(untitled)"));
  main.appendChild(el("div", "hist-meta", `${relTime(r.updated_at)} · ${r.turns || 0} msgs`));
  main.addEventListener("click", () => loadConversation(r.id));

  const actions = el("div", "hist-actions");
  const ren = el("button", "hist-act"); ren.title = "rename"; ren.appendChild(icon("pencil"));
  ren.addEventListener("click", (e) => { e.stopPropagation(); startRename(row, r); });
  const del = el("button", "hist-act"); del.title = "delete"; del.appendChild(icon("trash"));
  del.addEventListener("click", (e) => { e.stopPropagation(); confirmDelete(row, r); });
  actions.append(ren, del);

  row.append(main, actions);
  return row;
}

function startRename(row, r) {
  const titleEl = row.querySelector(".hist-title");
  const inp = el("input", "hist-rename");
  inp.value = r.title || "";
  titleEl.replaceWith(inp);
  inp.focus();
  inp.select();
  let done = false;
  const commit = async () => {
    if (done) return; done = true;
    const v = inp.value.trim();
    if (v && v !== r.title) {
      try {
        await fetch("/v1/sessions/" + r.id, {
          method: "PATCH",
          headers: { "Content-Type": "application/json", ...authHeaders() },
          body: JSON.stringify({ title: v }),
        });
      } catch (_) {}
    }
    loadHistory();
  };
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    else if (e.key === "Escape") { done = true; loadHistory(); }
  });
  inp.addEventListener("blur", commit);
}

function confirmDelete(row, r) {
  const actions = row.querySelector(".hist-actions");
  actions.innerHTML = "";
  const yes = el("button", "hist-act danger"); yes.title = "confirm delete"; yes.appendChild(icon("check"));
  const no = el("button", "hist-act"); no.title = "cancel"; no.appendChild(icon("x"));
  yes.addEventListener("click", async (e) => {
    e.stopPropagation();
    try { await fetch("/v1/sessions/" + r.id, { method: "DELETE", headers: authHeaders() }); } catch (_) {}
    if (state.sessionId === r.id) { state.sessionId = null; state.transcript = []; }
    loadHistory();
  });
  no.addEventListener("click", (e) => { e.stopPropagation(); loadHistory(); });
  actions.append(yes, no);
}

async function loadConversation(id) {
  let data;
  try {
    data = await (await fetch("/v1/sessions/" + id, { headers: authHeaders() })).json();
  } catch (_) { return; }
  screen.innerHTML = "";
  state.transcript = [];
  state.sessionId = id;
  (data.messages || []).forEach((m) => {
    renderMessage(m.role, m.content || "");
    state.transcript.push({ role: m.role, content: m.content || "" });
  });
  closeDrawer();
  scroll();
  input.focus();
}

async function copyConversation() {
  const md = state.transcript
    .map((m) => (m.role === "user" ? "**You:**\n\n" : "**Devy:**\n\n") + (m.content || ""))
    .join("\n\n---\n\n");
  if (!md) { note("Nothing to copy yet."); return; }
  try {
    await navigator.clipboard.writeText(md);
    note("Conversation copied as Markdown.");
  } catch (_) { note("Copy failed — clipboard unavailable.", "err"); }
}

histBtn.addEventListener("click", () => (drawer.classList.contains("open") ? closeDrawer() : openDrawer()));
drawerClose.addEventListener("click", closeDrawer);
drawerScrim.addEventListener("click", closeDrawer);
identInput.addEventListener("change", () => { setUserId(identInput.value); loadHistory(); });
copyBtn.addEventListener("click", copyConversation);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer.classList.contains("open")) closeDrawer(); });

/* ---------- boot ---------- */
async function boot() {
  await loadTiers();
  greet();
  input.focus();
}
boot();
