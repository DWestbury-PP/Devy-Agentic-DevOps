/* Devy — admin control plane (Phase 9). Thin client of /v1/admin/*.
 * Exchanges a password for a short-lived signed token (sessionStorage), then
 * manages the host registry. The token-in-a-header pattern is the SSO seam. */

const TOKEN_KEY = "devy_admin_token";
const $ = (id) => document.getElementById(id);
const show = (id) => $(id).classList.remove("hidden");
const hide = (id) => $(id).classList.add("hidden");

const getToken = () => sessionStorage.getItem(TOKEN_KEY) || "";
const setToken = (t) => (t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY));
const authHeaders = () => (getToken() ? { Authorization: `Bearer ${getToken()}` } : {});

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

let currentPage = "hosts";
function view(name) {
  ["login", "shell", "disabled"].forEach(hide);
  show(name);
  $("logout").classList.toggle("hidden", name !== "shell");
  if (name === "shell") showPage(currentPage);
}

function showPage(name) {
  currentPage = name;
  $("hosts-page").classList.toggle("hidden", name !== "hosts");
  $("repos-page").classList.toggle("hidden", name !== "repos");
  $("knowledge-page").classList.toggle("hidden", name !== "knowledge");
  $("tab-hosts").classList.toggle("active", name === "hosts");
  $("tab-repos").classList.toggle("active", name === "repos");
  $("tab-knowledge").classList.toggle("active", name === "knowledge");
  if (name === "hosts") renderHosts();
  else if (name === "repos") renderRepos();
  else renderKnowledge();
}
$("tab-hosts").addEventListener("click", () => showPage("hosts"));
$("tab-repos").addEventListener("click", () => showPage("repos"));
$("tab-knowledge").addEventListener("click", () => showPage("knowledge"));

async function checkSession() {
  if (!getToken()) return view("login");
  try {
    const r = await fetch("/v1/admin/me", { headers: authHeaders() });
    if (r.ok) return view("shell");
    if (r.status === 503) return view("disabled");
    setToken(null);
    view("login");
  } catch (_) {
    view("login");
  }
}

/* ---------- login ---------- */
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("login-msg");
  msg.className = "msg";
  msg.textContent = "Signing in…";
  try {
    const r = await fetch("/v1/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("password").value }),
    });
    if (r.status === 503) return view("disabled");
    if (!r.ok) {
      msg.className = "msg err";
      msg.textContent = r.status === 401 ? "Invalid password." : `Sign-in failed (${r.status}).`;
      return;
    }
    setToken((await r.json()).token);
    $("password").value = "";
    msg.textContent = "";
    view("shell");
  } catch (_) {
    msg.className = "msg err";
    msg.textContent = "Couldn't reach the proxy.";
  }
});

$("logout").addEventListener("click", () => {
  setToken(null);
  view("login");
});

/* ---------- host registry ---------- */
const hostsMsg = (text, err) => {
  const m = $("hosts-msg");
  m.className = "msg" + (err ? " err" : "");
  m.textContent = text || "";
};

async function renderHosts() {
  const body = $("hosts-body");
  body.innerHTML = "";
  let hosts = [];
  try {
    const r = await fetch("/v1/admin/hosts", { headers: authHeaders() });
    if (!r.ok) return hostsMsg(`Couldn't load hosts (${r.status}).`, true);
    hosts = await r.json();
  } catch (_) {
    return hostsMsg("Couldn't reach the proxy.", true);
  }
  if (!hosts.length) return hostsMsg("No hosts registered yet — add one above.");
  hostsMsg("");
  hosts.forEach((h) => body.appendChild(hostRow(h)));
}

function hostRow(h) {
  const tr = el("tr");
  const address = h.private_ip || h.public_ip || h.fqdn;
  tr.appendChild(el("td", null, h.fqdn));
  tr.appendChild(el("td", null, `${address}:${h.mcp_port}`));
  tr.appendChild(el("td", null, h.aws_region || "—"));

  const st = el("td");
  st.appendChild(el("span", "pill " + (h.last_status || ""), h.last_status || "unknown"));
  tr.appendChild(st);

  const act = el("td");
  const toggle = el("span", "pill " + (h.active ? "" : "inactive"), h.active ? "active" : "inactive");
  toggle.style.cursor = "pointer";
  toggle.title = "toggle active";
  toggle.addEventListener("click", () => patchHost(h.id, { active: !h.active }));
  act.appendChild(toggle);
  tr.appendChild(act);

  const actions = el("td");
  const wrap = el("div", "acts");
  const test = el("button", "btn ghost-btn", "Test");
  test.addEventListener("click", () => testHost(h.id));
  const del = el("button", "btn ghost-btn", "Delete");
  del.addEventListener("click", () => confirmDelete(wrap, h.id));
  wrap.append(test, del);
  actions.appendChild(wrap);
  tr.appendChild(actions);
  return tr;
}

function confirmDelete(wrap, id) {
  wrap.innerHTML = "";
  const yes = el("button", "btn", "Confirm");
  yes.addEventListener("click", async () => {
    await fetch(`/v1/admin/hosts/${id}`, { method: "DELETE", headers: authHeaders() });
    renderHosts();
  });
  const no = el("button", "btn ghost-btn", "Cancel");
  no.addEventListener("click", renderHosts);
  wrap.append(yes, no);
}

async function patchHost(id, body) {
  await fetch(`/v1/admin/hosts/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  renderHosts();
}

async function testHost(id) {
  hostsMsg("Testing connection…");
  try {
    const r = await fetch(`/v1/admin/hosts/${id}/check`, { method: "POST", headers: authHeaders() });
    const data = await r.json();
    hostsMsg(
      data.status === "reachable"
        ? `Reachable — ${data.checks.length} checks available.`
        : "Unreachable — verify the address, port, and token.",
      data.status !== "reachable",
    );
  } catch (_) {
    hostsMsg("Connection test failed.", true);
  }
  renderHosts();
}

/* ---------- add host ---------- */
$("add-toggle").addEventListener("click", () => $("host-form").classList.toggle("hidden"));
$("cancel-add").addEventListener("click", () => {
  $("host-form").reset();
  hide("host-form");
});

$("host-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {};
  for (const [k, v] of fd.entries()) {
    const val = String(v).trim();
    if (!val) continue;
    body[k] = k === "mcp_port" ? parseInt(val, 10) : val;
  }
  if (!body.fqdn) return hostsMsg("FQDN is required.", true);
  try {
    const r = await fetch("/v1/admin/hosts", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      return hostsMsg(`Couldn't add host: ${d.detail || r.status}`, true);
    }
    e.target.reset();
    hide("host-form");
    renderHosts();
  } catch (_) {
    hostsMsg("Couldn't reach the proxy.", true);
  }
});

/* ---------- GitHub accounts (repo connector) ---------- */
const ghMsg = (text, err) => {
  const m = $("gh-msg");
  m.className = "msg" + (err ? " err" : "");
  m.textContent = text || "";
};

async function renderRepos() {
  const body = $("gh-body");
  body.innerHTML = "";
  let accounts = [];
  try {
    const r = await fetch("/v1/admin/github/accounts", { headers: authHeaders() });
    if (!r.ok) return ghMsg(`Couldn't load accounts (${r.status}).`, true);
    accounts = await r.json();
  } catch (_) {
    return ghMsg("Couldn't reach the proxy.", true);
  }
  if (!accounts.length) {
    ghMsg("No GitHub accounts yet — add a read-only PAT above.");
  } else {
    ghMsg("");
    accounts.forEach((a) => body.appendChild(accountRow(a)));
  }
  renderCrawls();
}

async function renderCrawls() {
  const body = $("crawls-body");
  if (!body) return;
  body.innerHTML = "";
  const msg = $("crawls-msg");
  let crawls = [];
  try {
    const r = await fetch("/v1/admin/github/crawls", { headers: authHeaders() });
    if (!r.ok) { msg.textContent = `Couldn't load scan history (${r.status}).`; return; }
    crawls = await r.json();
  } catch (_) {
    msg.textContent = "Couldn't reach the proxy.";
    return;
  }
  if (!crawls.length) {
    msg.textContent = "No repos scanned yet — crawl one above.";
    return;
  }
  msg.textContent = "";
  crawls.forEach((c) => body.appendChild(crawlRow(c)));
}

function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function crawlRow(c) {
  const tr = el("tr");
  tr.appendChild(el("td", null, c.full_name));
  tr.appendChild(el("td", null, c.corpus || "—"));
  tr.appendChild(el("td", null, fmtWhen(c.crawled_at)));
  const sha = el("td");
  if (c.commit_sha) {
    const code = el("span", "pill", c.commit_sha.slice(0, 7));
    code.title = c.commit_sha + (c.default_branch ? ` (${c.default_branch})` : "");
    sha.appendChild(code);
  } else {
    sha.textContent = "—";
  }
  tr.appendChild(sha);
  tr.appendChild(el("td", null, String(c.doc_count)));
  tr.appendChild(el("td", null, String(c.chunk_count)));
  const actions = el("td");
  const rescan = el("button", "btn ghost-btn", "Rescan");
  rescan.addEventListener("click", () => doCrawl(c.full_name, c.corpus));
  actions.appendChild(rescan);
  tr.appendChild(actions);
  return tr;
}

function accountRow(a) {
  const tr = el("tr");
  tr.appendChild(el("td", null, a.label));
  tr.appendChild(el("td", null, a.login || "—"));
  tr.appendChild(el("td", null, a.default_corpus || "—"));
  const st = el("td");
  st.appendChild(el("span", "pill " + (a.last_status || ""), a.last_status || "unknown"));
  tr.appendChild(st);
  const act = el("td");
  const toggle = el("span", "pill " + (a.active ? "" : "inactive"), a.active ? "active" : "inactive");
  toggle.style.cursor = "pointer";
  toggle.title = "toggle active";
  toggle.addEventListener("click", () => patchAccount(a.id, { active: !a.active }));
  act.appendChild(toggle);
  tr.appendChild(act);
  const actions = el("td");
  const wrap = el("div", "acts");
  const test = el("button", "btn ghost-btn", "Test");
  test.addEventListener("click", () => testAccount(a.id));
  const del = el("button", "btn ghost-btn", "Delete");
  del.addEventListener("click", () => confirmDeleteAccount(wrap, a.id));
  wrap.append(test, del);
  actions.appendChild(wrap);
  tr.appendChild(actions);
  return tr;
}

async function patchAccount(id, body) {
  await fetch(`/v1/admin/github/accounts/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  renderRepos();
}

function confirmDeleteAccount(wrap, id) {
  wrap.innerHTML = "";
  const yes = el("button", "btn", "Confirm");
  yes.addEventListener("click", async () => {
    await fetch(`/v1/admin/github/accounts/${id}`, { method: "DELETE", headers: authHeaders() });
    renderRepos();
  });
  const no = el("button", "btn ghost-btn", "Cancel");
  no.addEventListener("click", renderRepos);
  wrap.append(yes, no);
}

async function testAccount(id) {
  ghMsg("Verifying PAT…");
  try {
    const r = await fetch(`/v1/admin/github/accounts/${id}/test`, { method: "POST", headers: authHeaders() });
    const data = await r.json();
    ghMsg(data.ok ? `Valid — authenticated as ${data.login}.` : `Invalid: ${data.error || "check the PAT"}`, !data.ok);
  } catch (_) {
    ghMsg("Verification failed.", true);
  }
  renderRepos();
}

$("gh-add-toggle").addEventListener("click", () => $("gh-form").classList.toggle("hidden"));
$("gh-cancel").addEventListener("click", () => {
  $("gh-form").reset();
  hide("gh-form");
});

$("gh-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {};
  for (const [k, v] of fd.entries()) {
    const val = String(v).trim();
    if (val) body[k] = val;
  }
  if (!body.label) return ghMsg("Label is required.", true);
  try {
    const r = await fetch("/v1/admin/github/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      return ghMsg(`Couldn't add account: ${d.detail || r.status}`, true);
    }
    e.target.reset();
    hide("gh-form");
    renderRepos();
  } catch (_) {
    ghMsg("Couldn't reach the proxy.", true);
  }
});

async function doCrawl(repo, corpus) {
  const msg = $("crawl-msg");
  // The crawl is synchronous (fetch → tree → contents → ingest), so give a clear
  // busy state: spinner on the button + a "working" message until it returns.
  const btn = $("crawl-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Crawling…';
  msg.className = "msg";
  msg.textContent = `Crawling ${repo} — fetching markdown, redacting, embedding…`;
  try {
    const r = await fetch("/v1/admin/github/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(corpus ? { repo, corpus } : { repo }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      msg.className = "msg err";
      msg.textContent = `Crawl failed: ${d.detail || r.status}`;
      return;
    }
    let note = `Crawled into '${d.corpus}'`;
    if (d.commit_sha) note += ` @ ${d.commit_sha.slice(0, 7)}`;
    note += `: ${d.files_ingested} ingested, ${d.chunks_written} chunks`;
    if (d.secrets_redacted) note += `, ${d.secrets_redacted} secrets redacted`;
    if (d.files_quarantined) note += `, ${d.files_quarantined} quarantined (suspected secrets)`;
    msg.className = "msg";
    msg.textContent = note + ".";
    $("crawl-form").reset();
    renderCrawls();
  } catch (_) {
    msg.className = "msg err";
    msg.textContent = "Couldn't reach the proxy.";
  } finally {
    btn.disabled = false;
    btn.textContent = "Crawl markdown";
  }
}

$("crawl-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const repo = $("crawl-repo").value.trim();
  const corpus = $("crawl-corpus").value.trim();
  if (!repo) {
    const msg = $("crawl-msg");
    msg.className = "msg err";
    msg.textContent = "Repo (owner/name) is required.";
    return;
  }
  doCrawl(repo, corpus);
});

/* ---------- knowledge (document import) ---------- */
const kbMsg = (text, err) => {
  const m = $("kb-msg");
  m.className = "msg" + (err ? " err" : "");
  m.textContent = text || "";
};
let kbFilter = null; // selected corpus, or null = all

$("kb-add-toggle").addEventListener("click", () => $("kb-form").classList.toggle("hidden"));
$("kb-cancel").addEventListener("click", () => {
  $("kb-form").reset();
  hide("kb-form");
});

$("kb-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const corpus = $("kb-corpus").value.trim();
  const files = $("kb-files").files;
  if (!corpus) return kbMsg("Corpus is required.", true);
  if (!files.length) return kbMsg("Choose at least one .md file.", true);

  const fd = new FormData();
  fd.append("corpus", corpus);
  for (const f of files) fd.append("files", f);

  const prog = $("kb-progress");
  prog.classList.remove("hidden");
  prog.className = "msg";
  prog.textContent = "Uploading…";
  try {
    const r = await fetch("/v1/admin/documents", { method: "POST", headers: authHeaders(), body: fd });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      prog.className = "msg err";
      prog.textContent = `Upload failed: ${d.detail || r.status}`;
      return;
    }
    const { job } = await r.json();
    $("kb-form").reset();
    hide("kb-form");
    kbFilter = corpus;
    await pollJob(job.id);
  } catch (_) {
    prog.className = "msg err";
    prog.textContent = "Couldn't reach the proxy.";
  }
});

async function pollJob(jobId) {
  const prog = $("kb-progress");
  prog.classList.remove("hidden");
  for (let i = 0; i < 900; i++) {
    let job;
    try {
      job = await (await fetch(`/v1/admin/jobs/${jobId}`, { headers: authHeaders() })).json();
    } catch (_) {
      break;
    }
    prog.className = "msg";
    prog.textContent = `Ingesting… ${job.done}/${job.total}`;
    renderKnowledge();
    if (job.status === "done" || job.status === "failed") {
      prog.className = "msg" + (job.status === "failed" ? " err" : "");
      prog.textContent =
        job.status === "failed"
          ? `Ingest failed: ${job.error || "unknown error"}`
          : `Done — ${job.done}/${job.total} document(s) ingested.`;
      setTimeout(() => prog.classList.add("hidden"), 4500);
      return;
    }
    await new Promise((res) => setTimeout(res, 800));
  }
}

async function renderKnowledge() {
  let corpora;
  try {
    corpora = await (await fetch("/v1/admin/corpora", { headers: authHeaders() })).json();
  } catch (_) {
    return kbMsg("Couldn't reach the proxy.", true);
  }
  const cdiv = $("kb-corpora");
  cdiv.innerHTML = "";
  const allPill = el("span", "corpus-pill" + (kbFilter === null ? " active" : ""), "all");
  allPill.addEventListener("click", () => { kbFilter = null; renderKnowledge(); });
  cdiv.appendChild(allPill);
  if (!corpora.length) cdiv.appendChild(el("span", "corpus-empty", "  no corpora yet — upload markdown above."));
  corpora.forEach((c) => cdiv.appendChild(corpusPill(c)));

  const url = kbFilter
    ? `/v1/admin/documents?corpus=${encodeURIComponent(kbFilter)}`
    : "/v1/admin/documents";
  let docs;
  try {
    docs = await (await fetch(url, { headers: authHeaders() })).json();
  } catch (_) {
    return;
  }
  const body = $("kb-body");
  body.innerHTML = "";
  if (!docs.length) return kbMsg("No documents yet — upload markdown above.");
  kbMsg("");
  docs.forEach((d) => body.appendChild(docRow(d)));
}

function corpusPill(c) {
  const pill = el("span", "corpus-pill" + (kbFilter === c.name ? " active" : ""));
  pill.appendChild(document.createTextNode(`${c.name} · ${c.documents} docs · ${c.chunks} chunks`));
  const x = el("span", "x", "✕");
  x.title = "delete corpus";
  x.addEventListener("click", (ev) => { ev.stopPropagation(); armCorpusDelete(pill, c.name); });
  pill.appendChild(x);
  pill.addEventListener("click", () => { kbFilter = c.name; renderKnowledge(); });
  return pill;
}

function armCorpusDelete(pill, name) {
  pill.replaceChildren(document.createTextNode(`delete ${name}? `));
  pill.classList.add("active");
  const yes = el("span", "x", "yes");
  yes.style.color = "var(--red)";
  yes.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    await fetch(`/v1/admin/corpora/${encodeURIComponent(name)}`, { method: "DELETE", headers: authHeaders() });
    if (kbFilter === name) kbFilter = null;
    renderKnowledge();
  });
  const no = el("span", "x", "no");
  no.addEventListener("click", (ev) => { ev.stopPropagation(); renderKnowledge(); });
  pill.append(yes, document.createTextNode(" / "), no);
}

function docRow(d) {
  const tr = el("tr");
  tr.appendChild(el("td", null, d.title || d.source_path));
  tr.appendChild(el("td", null, d.corpus));
  tr.appendChild(el("td", null, d.doc_type));
  const st = el("td");
  st.appendChild(el("span", "pill " + d.status, d.status));
  if (d.status === "failed" && d.error) st.title = d.error;
  tr.appendChild(st);
  tr.appendChild(el("td", null, String(d.chunk_count)));
  tr.appendChild(el("td", null, "v" + d.version));
  const actions = el("td");
  const wrap = el("div", "acts");
  const del = el("button", "btn ghost-btn", "Delete");
  del.addEventListener("click", () => confirmDeleteDoc(wrap, d.id));
  wrap.appendChild(del);
  actions.appendChild(wrap);
  tr.appendChild(actions);
  return tr;
}

function confirmDeleteDoc(wrap, id) {
  wrap.innerHTML = "";
  const yes = el("button", "btn", "Confirm");
  yes.addEventListener("click", async () => {
    await fetch(`/v1/admin/documents/${id}`, { method: "DELETE", headers: authHeaders() });
    renderKnowledge();
  });
  const no = el("button", "btn ghost-btn", "Cancel");
  no.addEventListener("click", renderKnowledge);
  wrap.append(yes, no);
}

checkSession();
