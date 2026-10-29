// ─── state ────────────────────────────────────────────────────────────
const S = {
  connected: false,
  records: [],
  filtered: [],
  selected: null,
  sortKey: "date-desc",
  filterText: "",
  projectFilter: "",
  lastOutput: null,
  exporting: false,
  currentStream: null,
};

// ─── helpers ──────────────────────────────────────────────────────────
const $  = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
  }[c]));
}

function renderMarkdown(md) {
  if (!window.marked || !window.DOMPurify) return `<p>${escapeHtml(md)}</p>`;
  const html = window.marked.parse(md, { breaks: true, gfm: true });
  return window.DOMPurify.sanitize(html);
}

function shortDate(iso) {
  return (iso || "").slice(0, 10) || "????-??-??";
}
function relativeDate(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return shortDate(iso);
  const diff = (Date.now() - t) / 1000;
  if (diff < 60)        return "just now";
  if (diff < 3600)      return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)     return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
  const d = new Date(t);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// Deterministic HSL color from project name
const PROJECT_PALETTE = [
  "#8b6dff", "#34d399", "#60a5fa", "#f472b6", "#fbbf24",
  "#22d3ee", "#a78bfa", "#4ade80", "#fb923c", "#e879f9",
];
function projectColor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = ((h << 5) - h + name.charCodeAt(i)) | 0;
  return PROJECT_PALETTE[Math.abs(h) % PROJECT_PALETTE.length];
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (!res.ok) {
    const details = await res.json().catch(() => ({}));
    throw new Error(details.error || "HTTP " + res.status);
  }
  return res.json();
}

// ─── toasts ───────────────────────────────────────────────────────────
function toast(kind, title, subtitle = "", ms = 4200) {
  const wrap = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const icon = kind === "ok" ? "✓" : kind === "err" ? "✕" : "•";
  el.innerHTML =
    `<div class="toast-icon">${icon}</div>` +
    `<div class="toast-body"><b>${escapeHtml(title)}</b>` +
    (subtitle ? `<em>${escapeHtml(subtitle)}</em>` : "") +
    `</div>`;
  wrap.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 220);
  }, ms);
}

// ─── status / detect ──────────────────────────────────────────────────
function setStatus(connected, text, cls) {
  const dot = $("#status-dot");
  dot.classList.remove("ok", "err", "pending");
  dot.classList.add(cls);
  $("#status-text").textContent = text;
  S.connected = connected;
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    if (status.defaultOutput && !$("#opt-output").value) {
      $("#opt-output").value = status.defaultOutput;
    }
    if (status.defaultOutput && !$("#native-backup-path").value) {
      const directory = status.defaultOutput.replace(/[\\/][^\\/]+$/, "");
      const day = new Date().toISOString().slice(0, 10).replace(/-/g, "");
      $("#native-backup-path").value = directory + "/AntigravityNative-" + day + ".zip";
    }
    if (status.lastOutput) S.lastOutput = status.lastOutput;

    // A failed startup discovery does not retry on the server by itself.
    // Calling /api/status repeatedly would otherwise leave the UI stuck
    // on "Detecting Antigravity" indefinitely.
    const r = status.connected
      ? status
      : await api("/api/detect", { method: "POST", body: "{}" });
    if (r.connected) {
      setStatus(true, `pid ${r.pid} · port ${r.port} · ${r.count} conversations`, "ok");
      $("#status-text").title = "";
      await loadConversations();
    } else {
      setStatus(false, "Antigravity not ready · retrying…", "pending");
      $("#status-text").title = r.error || "";
      setTimeout(loadStatus, 2500);
    }
  } catch (e) {
    setStatus(false, "Connection failed · retrying…", "err");
    $("#status-text").title = String(e.message || e);
    setTimeout(loadStatus, 2500);
  }
}

async function redetect() {
  setStatus(false, "Re-detecting…", "pending");
  try {
    const r = await api("/api/detect", { method: "POST", body: "{}" });
    if (r.connected) {
      setStatus(true, `pid ${r.pid} · port ${r.port} · ${r.count} conversations`, "ok");
      await loadConversations();
      toast("ok", "Reconnected", `${r.count} conversations loaded`);
    } else {
      setStatus(false, r.error || "Not reachable", "err");
      toast("err", "Detection failed", r.error || "");
    }
  } catch {
    setStatus(false, "Detect failed", "err");
  }
}

// ─── conversations list ───────────────────────────────────────────────
async function loadConversations() {
  const items = await api("/api/conversations");
  S.records = items;
  populateProjectSelects();
  applyFilter();
}

function populateProjectSelects() {
  const counts = new Map();
  for (const r of S.records) counts.set(r.project, (counts.get(r.project) || 0) + 1);
  const sorted = [...counts.entries()].sort((a, b) =>
    a[0].toLowerCase().localeCompare(b[0].toLowerCase())
  );
  for (const selId of ["#project-select", "#opt-project"]) {
    const sel = $(selId);
    const keep = sel.value;
    sel.innerHTML = `<option value="">(All projects)</option>` +
      sorted.map(([p, c]) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}  (${c})</option>`).join("");
    if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
  }
}

function applyFilter() {
  const needle = S.filterText.trim().toLowerCase();
  let recs = S.records.slice();
  if (S.projectFilter) recs = recs.filter(r => r.project === S.projectFilter);
  if (needle) recs = recs.filter(r =>
    r.title.toLowerCase().includes(needle) || r.project.toLowerCase().includes(needle)
  );
  switch (S.sortKey) {
    case "date-desc":
      recs.sort((a, b) => (b.created || "").localeCompare(a.created || "")); break;
    case "date-asc":
      recs.sort((a, b) => (a.created || "zzzz").localeCompare(b.created || "zzzz")); break;
    case "title":
      recs.sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase())); break;
    case "project":
      recs.sort((a, b) => {
        const p = a.project.toLowerCase().localeCompare(b.project.toLowerCase());
        return p !== 0 ? p : (b.created || "").localeCompare(a.created || "");
      });
      break;
  }
  S.filtered = recs;
  renderList();
}

function renderList() {
  const list = $("#conv-list");
  $("#list-count").textContent =
    `${S.filtered.length} of ${S.records.length}`;

  const frag = document.createDocumentFragment();
  for (const r of S.filtered) {
    const el = document.createElement("div");
    el.className = "conv-item" + (r.cid === S.selected ? " active" : "");
    el.dataset.cid = r.cid;
    el.setAttribute("role", "option");
    const color = projectColor(r.project);
    el.style.setProperty("--proj-color", color);
    el.innerHTML =
      `<div class="t1">${escapeHtml(r.title)}</div>` +
      `<div class="t2">` +
      `<span>${escapeHtml(relativeDate(r.created))}</span>` +
      `<span class="sep">·</span>` +
      `<span class="project-tag"><span class="proj-dot"></span>${escapeHtml(r.project)}</span>` +
      `<span class="sep">·</span>` +
      `<span>${r.stepCount} steps</span>` +
      `</div>`;
    el.addEventListener("click", () => selectConversation(r.cid));
    frag.appendChild(el);
  }
  list.innerHTML = "";
  list.appendChild(frag);
}

// ─── viewer ───────────────────────────────────────────────────────────
function showSkeleton() {
  $("#viewer-body").innerHTML = `
    <div class="skeleton-turn user"><div class="sk-bubble"></div></div>
    <div class="skeleton-turn agent"><div class="sk-bubble" style="width:72%;height:92px"></div></div>
    <div class="skeleton-turn user"><div class="sk-bubble" style="width:48%;height:44px"></div></div>
    <div class="skeleton-turn agent"><div class="sk-bubble" style="width:68%;height:120px"></div></div>
  `;
}

async function selectConversation(cid) {
  if (S.selected === cid) return;
  for (const el of $$("#conv-list .conv-item")) {
    el.classList.toggle("active", el.dataset.cid === cid);
  }
  S.selected = cid;
  const rec = S.records.find(r => r.cid === cid);
  if (!rec) return;

  $("#viewer-title").textContent = rec.title;
  $("#viewer-meta").innerHTML =
    `<span class="meta-item"><span class="meta-key">Created</span> ${escapeHtml(shortDate(rec.created))}</span>` +
    `<span class="meta-item"><span class="meta-key">Project</span> ${escapeHtml(rec.project)}</span>` +
    `<span class="meta-item"><span class="meta-key">Steps</span> ${rec.stepCount}</span>` +
    `<span class="meta-item mono"><span class="meta-key">ID</span> ${escapeHtml(cid.slice(0, 8))}</span>`;
  $("#btn-export-one").disabled = true;
  showSkeleton();

  try {
    const data = await api(`/api/conversations/${encodeURIComponent(cid)}`);
    if (S.selected !== cid) return;
    renderTurns(data.turns || []);
    $("#btn-export-one").disabled = false;
  } catch (e) {
    $("#viewer-body").innerHTML =
      `<div class="empty"><div class="empty-illo">⚠️</div>
       <h2>Failed to load</h2><p>${escapeHtml(String(e))}</p></div>`;
  }
}

function renderTurns(turns) {
  const body = $("#viewer-body");
  if (!turns.length) {
    body.innerHTML = `<div class="empty">
      <svg class="empty-illo" viewBox="0 0 64 64" fill="none"
           stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="32" cy="32" r="22" />
        <line x1="20" y1="38" x2="44" y2="38" stroke-dasharray="2 3" />
      </svg>
      <h2>No chat to show</h2>
      <p>This conversation has no user / agent messages.<br>Tool runs only appear in the full on-disk export.</p></div>`;
    return;
  }
  const parts = [];
  for (const t of turns) {
    const roleLabel = t.role === "user" ? "You" : "Agent";
    parts.push(
      `<div class="turn ${t.role}"><div class="bubble">` +
        `<div class="role">${roleLabel}</div>` +
        renderMarkdown(t.markdown || "") +
      `</div></div>`
    );
  }
  body.innerHTML = parts.join("");
  body.scrollTop = 0;
}

// ─── tabs ─────────────────────────────────────────────────────────────
function switchTab(name) {
  for (const t of $$(".tab")) t.classList.toggle("active", t.dataset.tab === name);
  for (const p of $$(".panel")) p.classList.toggle("active", p.id === `panel-${name}`);
}

// ─── export form → options ───────────────────────────────────────────
function readOptions() {
  return {
    output:              $("#opt-output").value.trim(),
    includeConversations:$("#opt-conversations").checked,
    includeKnowledge:    $("#opt-knowledge").checked,
    includeArtifacts:    $("#opt-artifacts").checked,
    projectFilter:       $("#opt-project").value,
    naming:              $("#opt-naming").value,
    layout:              $("#opt-layout").value,
    format:              $("#opt-format").value,
    detail:              $("#opt-detail").value,
    applyTimestamps:     $("#opt-apply-ts").checked,
    wipe:                $("#opt-wipe").checked,
  };
}

// ─── export streams ───────────────────────────────────────────────────
async function streamExport(path, opts, { single = false } = {}) {
  if (S.exporting) return;
  S.exporting = true;
  resetProgress();
  $("#btn-export").disabled = true;
  $("#btn-export-one").disabled = true;
  $("#btn-open").disabled = true;

  const ctrl = new AbortController();
  S.currentStream = ctrl;

  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts),
      signal: ctrl.signal,
    });
    if (!res.ok || !res.body) {
      const text = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}${text ? ` — ${text}` : ""}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const evt = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = evt.split("\n").find(l => l.startsWith("data:"));
        if (!line) continue;
        try { handleEvent(JSON.parse(line.slice(5).trim()), { single }); } catch {}
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      logLine(`ERROR: ${e.message || e}`, "err");
      toast("err", "Export failed", String(e.message || e));
    }
    setPhase("Failed", "err");
  } finally {
    S.exporting = false;
    S.currentStream = null;
    $("#btn-export").disabled = false;
    $("#btn-export-one").disabled = !S.selected;
    $("#btn-open").disabled = !S.lastOutput;
  }
}

function handleEvent(evt, ctx = {}) {
  switch (evt.type) {
    case "start":
      setPhase("Starting", "active");
      logLine(`→ ${ctx.single ? "Exporting selected conversation" : "Exporting"} to ${evt.output}`);
      break;
    case "log":
      logLine(evt.message || "");
      break;
    case "progress":
      setProgress(evt);
      break;
    case "done":
      S.lastOutput = evt.output;
      setPhase("Complete", "done");
      $("#bar-fill").style.width = "100%";
      $("#count-label").textContent = "";
      $("#progress-detail").textContent = "";
      logLine(`✓ Finished → ${evt.output}`, "ok");
      toast("ok", ctx.single ? "Conversation exported" : "Export complete",
            evt.output);
      break;
    case "error":
      setPhase("Failed", "err");
      logLine(`ERROR: ${evt.message}`, "err");
      toast("err", "Export failed", evt.message || "");
      break;
  }
}

// ─── progress / log ───────────────────────────────────────────────────
const PHASE_TITLES = {
  init: "Preparing", knowledge: "Knowledge items", artifacts: "Brain artifacts",
  conversations: "Conversations", finalizing: "Finalizing", done: "Complete",
};
function setPhase(text, cls) {
  const el = $("#phase-label");
  el.textContent = text;
  el.classList.remove("active", "done", "err");
  if (cls) el.classList.add(cls);
}
function setProgress({ phase, label, current, total }) {
  setPhase(PHASE_TITLES[phase] || phase, "active");
  $("#progress-detail").textContent = label || "";
  if (total > 0) {
    const frac = Math.max(0, Math.min(1, current / total));
    $("#bar-fill").style.width = (frac * 100).toFixed(1) + "%";
    $("#count-label").textContent = `${current}/${total}`;
  } else {
    $("#bar-fill").style.width = "5%";
    $("#count-label").textContent = "";
  }
}
function resetProgress() {
  setPhase("Idle");
  $("#progress-detail").textContent = "";
  $("#count-label").textContent = "";
  $("#bar-fill").style.width = "0";
  $("#log-box").textContent = "";
}
function logLine(msg, cls) {
  const box = $("#log-box");
  const span = document.createElement("span");
  if (cls) span.className = cls;
  span.textContent = msg + "\n";
  box.appendChild(span);
  box.scrollTop = box.scrollHeight;
}

// ─── server lifecycle: heartbeat + explicit shutdown ─────────────────
function startHeartbeat() {
  const tick = () => {
    fetch("/api/heartbeat", { method: "POST", body: "{}",
                              headers: { "Content-Type": "application/json" } })
      .catch(() => {});
  };
  tick();
  setInterval(tick, 10_000);
}
function stopServerBeacon() {
  try {
    const blob = new Blob(["{}"], { type: "application/json" });
    navigator.sendBeacon("/api/shutdown", blob);
  } catch {}
}
async function quitServer() {
  stopServerBeacon();
  toast("ok", "Server stopped", "You can close this tab.");
  setTimeout(() => window.close(), 400);
}

// ─── native offline session migration ────────────────────────────────
let nativeLastPreview = null;

function nativeReport(text) {
  $("#native-report").textContent = text;
}

async function nativeCreateBackup() {
  const path = $("#native-backup-path").value.trim();
  if (!path) { toast("err", "Enter a ZIP file path"); return; }
  const btn = $("#btn-native-backup");
  btn.disabled = true;
  nativeReport("Creating native archive and verifying SQLite snapshots…");
  try {
    const r = await api("/api/native/export", {
      method: "POST", body: JSON.stringify({ path }),
    });
    nativeReport("Created " + r.conversations + " native sessions, " + r.files + " files\n" +
                 "Archive: " + r.archive + "\nSize: " + (r.size / 1048576).toFixed(2) + " MiB");
    toast("ok", "Native backup created", r.conversations + " sessions");
  } catch (e) {
    nativeReport("Backup failed: " + String(e.message || e));
    toast("err", "Native backup failed", String(e.message || e));
  } finally {
    btn.disabled = false;
  }
}

async function nativePreviewImport() {
  const path = $("#native-import-path").value.trim();
  nativeLastPreview = null;
  $("#btn-native-apply").disabled = true;
  if (!path) { toast("err", "Enter the full path of the native ZIP"); return; }
  const btn = $("#btn-native-preview");
  btn.disabled = true;
  nativeReport("Verifying ZIP integrity and comparing conversation UUIDs…");
  try {
    const r = await api("/api/native/plan", {
      method: "POST", body: JSON.stringify({ path }),
    });
    const names = r.add.map(x => "+ " + x.title + " (" + x.steps + " steps)");
    nativeReport("New sessions: " + r.add.length + "\nExisting IDs skipped: " + r.skip.length + "\n\n" +
                 (names.length ? names.join("\n") : "Nothing new to import.") +
                 "\n\nClose Antigravity before clicking Import missing sessions.");
    nativeLastPreview = { path, expectedAddIds: r.add.map(x => x.id) };
    $("#btn-native-apply").disabled = r.add.length === 0;
  } catch (e) {
    nativeReport("Preview failed: " + String(e.message || e));
    toast("err", "Import preview failed", String(e.message || e));
  } finally {
    btn.disabled = false;
  }
}

async function nativeApplyImport() {
  if (!nativeLastPreview || nativeLastPreview.path !== $("#native-import-path").value.trim()) {
    toast("err", "Run Preview import again");
    return;
  }
  const n = nativeLastPreview.expectedAddIds.length;
  if (!window.confirm("Import " + n + " new Antigravity session(s)?\n\n" +
      "Antigravity must be CLOSED. Existing conversation IDs will never be overwritten. " +
      "A pre-import backup of the target indexes will be saved.")) return;
  const btn = $("#btn-native-apply");
  btn.disabled = true;
  nativeReport("Importing native session databases and updating indexes…");
  try {
    const result = await api("/api/native/apply", {
      method: "POST", body: JSON.stringify(nativeLastPreview),
    });
    nativeReport("Imported: " + (result.inserted || 0) + "\n" +
                 "Existing preserved: " + (result.preserved_existing || result.skip.length) + "\n" +
                 "Rollback snapshot: " + (result.backup || "(not needed)") + "\n" +
                 "Restart Antigravity to inspect imported sessions.");
    toast("ok", "Import completed", (result.inserted || 0) + " new sessions");
    nativeLastPreview = null;
  } catch (e) {
    nativeReport("Import failed: " + String(e.message || e) + "\nNo existing sessions should be overwritten.");
    toast("err", "Native import failed", String(e.message || e));
  }
}

// ─── keyboard shortcuts ──────────────────────────────────────────────
function onKey(e) {
  const tag = (document.activeElement?.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select";

  if (e.key === "/" && !typing) {
    e.preventDefault();
    $("#filter-input").focus();
    return;
  }
  if (e.key === "Escape") {
    const f = $("#filter-input");
    if (document.activeElement === f) {
      f.blur();
      if (f.value) { f.value = ""; S.filterText = ""; applyFilter(); }
    }
    return;
  }
  if (typing) return;

  if (e.key === "r" || e.key === "R") { redetect(); return; }
  if (e.key === "q" || e.key === "Q") { quitServer(); return; }
  if (e.key === "j" || e.key === "k") {
    if (!S.filtered.length) return;
    const idx = S.filtered.findIndex(r => r.cid === S.selected);
    const next = e.key === "j"
      ? Math.min(S.filtered.length - 1, idx < 0 ? 0 : idx + 1)
      : Math.max(0, idx < 0 ? 0 : idx - 1);
    const rec = S.filtered[next];
    if (rec) {
      selectConversation(rec.cid);
      const el = document.querySelector(`.conv-item[data-cid="${CSS.escape(rec.cid)}"]`);
      el?.scrollIntoView({ block: "nearest" });
    }
  }
}

// ─── wire up ──────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  $$(".tab").forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));

  $("#btn-native-backup").addEventListener("click", nativeCreateBackup);
  $("#btn-native-preview").addEventListener("click", nativePreviewImport);
  $("#btn-native-apply").addEventListener("click", nativeApplyImport);
  $("#native-import-path").addEventListener("input", () => {
    nativeLastPreview = null;
    $("#btn-native-apply").disabled = true;
  });

  $("#btn-refresh").addEventListener("click", redetect);
  $("#btn-quit").addEventListener("click", quitServer);

  $("#sort-select").addEventListener("change", (e) => { S.sortKey = e.target.value; applyFilter(); });
  $("#filter-input").addEventListener("input", (e) => { S.filterText = e.target.value; applyFilter(); });
  $("#project-select").addEventListener("change", (e) => { S.projectFilter = e.target.value; applyFilter(); });

  $("#btn-export").addEventListener("click", () => {
    const opts = readOptions();
    if (!opts.output) { toast("err", "Output folder required", "Enter a path first."); return; }
    streamExport("/api/export", opts);
  });

  $("#btn-export-one").addEventListener("click", () => {
    if (!S.selected) return;
    switchTab("export");
    const opts = readOptions();
    opts.wipe = false;
    streamExport(`/api/export/${encodeURIComponent(S.selected)}`, opts, { single: true });
  });

  $("#btn-browse-output").addEventListener("click", async () => {
    const btn = $("#btn-browse-output");
    btn.disabled = true;
    try {
      const r = await api("/api/pick-folder", {
        method: "POST",
        body: JSON.stringify({ initial: $("#opt-output").value || "" }),
      });
      if (r.path) $("#opt-output").value = r.path;
    } catch (e) {
      toast("err", "Folder picker failed", String(e.message || e));
    } finally {
      btn.disabled = false;
    }
  });

  $("#btn-open").addEventListener("click", () => {
    if (!S.lastOutput) return;
    api("/api/open-folder", {
      method: "POST",
      body: JSON.stringify({ path: S.lastOutput }),
    }).catch(() => {});
  });

  document.addEventListener("keydown", onKey);
  // The heartbeat watchdog already handles closed tabs. Shutting down on
  // beforeunload/pagehide also kills the server during a normal page reload.

  startHeartbeat();
  loadStatus();
});
