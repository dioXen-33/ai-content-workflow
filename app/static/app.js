"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  jobId: null,
  job: null,
  videos: [],
  selection: new Set(),
  // Vidéos déjà vues par l'interface. Sert à distinguer « pas encore amorcée »
  // de « vidée volontairement » : sans ça, un rendu suivant « Tout décocher »
  // recochait tout depuis la base.
  known: new Set(),
  // Vidéos dont le lecteur est ouvert, pour survivre à un re-rendu.
  previewing: new Set(),
  references: [],
  refId: null,
  prefs: null,
  step: 1,
};

// ---------------------------------------------------------------------------
// Utilitaires
// ---------------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 5000);
}

const fmtInt = (n) => (n || 0).toLocaleString("fr-FR");
const fmtUsd = (n) => `${(n || 0).toFixed(2)} $`;
const fmtDuration = (s) => (s ? `${Number(s).toFixed(1)} s` : "—");

const STATE_LABEL = {
  discovered: ["À traiter", ""],
  downloaded: ["Téléchargée", "badge-run"],
  framed: ["Frame OK", "badge-run"],
  edit_batched: ["Batch en cours", "badge-run"],
  edited: ["Image générée", "badge-run"],
  motion_submitted: ["Kling en cours", "badge-run"],
  done: ["Terminée", "badge-ok"],
  failed: ["Échec", "badge-err"],
  skipped: ["Écartée", "badge-warn"],
};

const JOB_STATUS_LABEL = {
  draft: ["Brouillon", ""],
  scraping: ["Scraping…", "badge-run"],
  review: ["À valider", "badge-warn"],
  running: ["En cours", "badge-run"],
  paused: ["En pause", "badge-warn"],
  completed: ["Terminé", "badge-ok"],
  failed: ["Échec", "badge-err"],
};

function badge(map, key) {
  const [label, cls] = map[key] || [key, ""];
  return `<span class="badge ${cls}">${label}</span>`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showView(id) {
  ["#view-jobs", "#view-settings", "#view-job"].forEach((v) =>
    $(v).classList.toggle("hidden", v !== id));
}

// ---------------------------------------------------------------------------
// Santé / diagnostic
// ---------------------------------------------------------------------------

async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("#dry-badge").classList.toggle("hidden", !h.dry_run);

    const problems = [];
    if (!h.ffmpeg) problems.push("ffmpeg introuvable dans le PATH.");
    if (h.missing_keys.length && !h.dry_run) {
      problems.push(`Clés manquantes dans .env : ${h.missing_keys.join(", ")}.`);
    }
    problems.push(...h.warnings);

    const banner = $("#health-banner");
    banner.innerHTML = problems.map(esc).join(" &nbsp;·&nbsp; ");
    banner.classList.toggle("hidden", !problems.length);
  } catch (e) {
    console.error(e);
  }
}

$("#btn-diagnostics").onclick = async () => {
  $("#modal-body").innerHTML = "<p class='muted'>Tests en cours…</p>";
  $("#modal").classList.remove("hidden");
  try {
    const d = await api("/api/diagnostics", { method: "POST" });
    $("#modal-body").innerHTML = d.checks.map((c) => `
      <div class="check-row">
        <span class="dot">${c.ok ? "✅" : "❌"}</span>
        <div><b>${esc(c.name)}</b><p>${esc(c.detail)}</p></div>
      </div>`).join("");
  } catch (e) {
    $("#modal-body").innerHTML = `<p class="muted">${esc(e.message)}</p>`;
  }
};
$("#modal-close").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };

// ---------------------------------------------------------------------------
// Préférences
// ---------------------------------------------------------------------------

async function loadPrefs() {
  state.prefs = await api("/api/preferences");
  if (!state.refId && state.prefs.reference_image_id) {
    state.refId = state.prefs.reference_image_id;
  }
  applyPrefsToNewJobForm();
  applyPrefsToSettingsForm();
}

function applyPrefsToNewJobForm() {
  const p = state.prefs;
  $("#p-max").value = p.max_videos_per_account;
  $("#p-views").value = p.min_views;
  $("#p-dmin").value = p.min_duration_s;
  $("#p-dmax").value = p.max_duration_s;
}

function applyPrefsToSettingsForm() {
  const p = state.prefs;
  $("#set-prompt").value = p.prompt;
  $("#set-ratio").value = p.aspect_ratio;
  $("#set-size").value = p.image_size;
  $("#set-maxdur").value = p.max_output_duration_s;
  $("#set-mode").value = p.kling_mode;
  $("#set-audio").value = String(p.keep_audio);
  $("#set-max").value = p.max_videos_per_account;
  $("#set-views").value = p.min_views;
  $("#set-dmin").value = p.min_duration_s;
  $("#set-dmax").value = p.max_duration_s;
  $("#set-batch").checked = p.gemini_batch;
  $("#set-budget").value = p.max_spend_usd;
  renderReferences();
}

// ---------------------------------------------------------------------------
// Navigateur de scraping dédié
// ---------------------------------------------------------------------------

async function refreshBrowserState() {
  const el = $("#browser-state");
  if (!el) return;
  try {
    const s = await api("/api/browser/status");
    if (!s.browser_found) {
      el.innerHTML = `<b class="warn-text">Aucun navigateur Chromium trouvé.</b>
        Installe Chrome ou Edge, ou renseigne BROWSER_EXECUTABLE dans .env.`;
      return;
    }
    const session = s.cookies_file
      ? `Session capturée il y a <b>${s.cookies_age_h} h</b>`
      : `<span class="warn-text">Aucune session capturée</span>`;
    el.innerHTML = `
      Backend : <b>${s.backend}</b> · yt-dlp ${s.ytdlp ? "prêt" : "absent"}<br>
      Navigateur ${s.running ? "<b>ouvert</b>" : "fermé"} · ${session}`;
  } catch (e) {
    el.textContent = e.message;
  }
}

$("#btn-browser-launch").onclick = async () => {
  const btn = $("#btn-browser-launch");
  btn.disabled = true;
  btn.textContent = "Ouverture…";
  try {
    const r = await api("/api/browser/launch", { method: "POST", body: JSON.stringify({}) });
    toast(
      r.already_running
        ? "Navigateur déjà ouvert — ramené au premier plan (sinon, cherche-le dans la barre des tâches)."
        : "Navigateur ouvert. Connecte le compte dédié, puis capture la session.",
      "ok");
    refreshBrowserState();
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Ouvrir le navigateur";
  }
};

$("#btn-browser-capture").onclick = async () => {
  try {
    const r = await api("/api/browser/capture", { method: "POST" });
    const ig = r.logged_in.instagram ? "Instagram connecté" : "Instagram NON connecté";
    const tk = r.logged_in.tiktok ? ", TikTok connecté" : "";
    toast(`${r.cookies} cookies capturés — ${ig}${tk}.`,
          r.logged_in.instagram ? "ok" : "err");
    refreshBrowserState();
  } catch (e) { toast(e.message, "err"); }
};

$("#btn-browser-close").onclick = async () => {
  await api("/api/browser/close", { method: "POST" });
  toast("Navigateur fermé.", "ok");
  refreshBrowserState();
};

$("#btn-settings").onclick = () => {
  refreshBrowserState();
  showView("#view-settings");
  $("#budget-chip").classList.add("hidden");
  applyPrefsToSettingsForm();
};

$("#btn-save-settings").onclick = async () => {
  const payload = {
    prompt: $("#set-prompt").value,
    reference_image_id: state.prefs.reference_image_id || "",
    aspect_ratio: $("#set-ratio").value,
    image_size: $("#set-size").value,
    max_output_duration_s: +$("#set-maxdur").value || 30,
    kling_mode: $("#set-mode").value,
    keep_audio: $("#set-audio").value === "true",
    max_videos_per_account: +$("#set-max").value || 30,
    min_views: +$("#set-views").value || 0,
    min_duration_s: +$("#set-dmin").value || 0,
    max_duration_s: +$("#set-dmax").value || 30,
    gemini_batch: $("#set-batch").checked,
    max_spend_usd: +$("#set-budget").value || 50,
  };
  try {
    state.prefs = await api("/api/preferences", {
      method: "PUT", body: JSON.stringify(payload),
    });
    applyPrefsToNewJobForm();
    toast("Paramètres enregistrés.", "ok");
  } catch (e) { toast(e.message, "err"); }
};


// ---------------------------------------------------------------------------
// Liste des jobs
// ---------------------------------------------------------------------------

async function loadJobs() {
  const jobs = await api("/api/jobs");
  const el = $("#jobs-list");
  if (!jobs.length) {
    el.innerHTML = "<p class='muted'>Aucun job.</p>";
    return;
  }
  el.innerHTML = jobs.map((j) => `
    <button type="button" class="job-item" data-id="${j.id}">
      <span>
        <b>${esc(j.name)}</b>
        <small>${fmtInt(j.n_videos)} vidéo(s) · ${fmtInt(j.n_done)} terminée(s) · ${fmtUsd(j.spent_usd)}</small>
      </span>
      ${badge(JOB_STATUS_LABEL, j.status)}
    </button>`).join("");
  $$("#jobs-list .job-item").forEach((n) => { n.onclick = () => openJob(n.dataset.id); });
}

$("#btn-home").onclick = () => {
  state.jobId = null;
  showView("#view-jobs");
  $("#budget-chip").classList.add("hidden");
  loadJobs();
};

$("#btn-create").onclick = async () => {
  const name = $("#job-name").value.trim();
  const accounts = $("#job-accounts").value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (!name) return toast("Donne un nom au job.", "err");
  if (!accounts.length) return toast("Ajoute au moins un compte.", "err");

  const scrape = {
    max_videos_per_account: +$("#p-max").value || 30,
    posted_after: $("#p-after").value || null,
    min_views: +$("#p-views").value || 0,
    min_duration_s: +$("#p-dmin").value || 0,
    max_duration_s: +$("#p-dmax").value || 30,
  };

  try {
    const { id } = await api("/api/jobs", {
      method: "POST", body: JSON.stringify({ name, accounts, scrape }),
    });
    await api(`/api/jobs/${id}/scrape`, { method: "POST" });
    $("#job-name").value = "";
    $("#job-accounts").value = "";
    openJob(id);
  } catch (e) { toast(e.message, "err"); }
};

$("#btn-create-upload").onclick = async () => {
  const name = $("#job-name").value.trim();
  if (!name) return toast("Donne un nom au job.", "err");

  try {
    // Ni compte ni scraping : les vidéos viendront de l'étape 1.
    const { id } = await api("/api/jobs", {
      method: "POST", body: JSON.stringify({ name, accounts: [], scrape: {} }),
    });
    $("#job-name").value = "";
    $("#job-accounts").value = "";
    await openJob(id);
    goStep(1);
    toast("Job créé. Importe tes vidéos.", "ok");
  } catch (e) { toast(e.message, "err"); }
};

// ---------------------------------------------------------------------------
// Vue job
// ---------------------------------------------------------------------------

async function openJob(id) {
  state.jobId = id;
  state.selection.clear();
  state.known.clear();
  state.previewing.clear();
  showView("#view-job");
  await refreshJob();
  goStep(pickStep());
}

function pickStep() {
  const s = state.job?.status;
  if (s === "review") return 2;
  if (s === "running" || s === "paused") return 3;
  if (s === "completed") return 4;
  return 1;
}

async function refreshJob() {
  if (!state.jobId) return;
  state.job = await api(`/api/jobs/${state.jobId}`);
  state.videos = await api(`/api/jobs/${state.jobId}/videos`);

  // Une vidéo qui apparaît (scraping en cours, import) hérite de son état en
  // base. Une vidéo déjà connue garde le choix de l'utilisateur, y compris
  // quand ce choix est « aucune ».
  state.videos.forEach((v) => {
    if (state.known.has(v.id)) return;
    state.known.add(v.id);
    if (v.selected) state.selection.add(v.id);
  });

  const j = state.job;
  const hasAccounts = j.accounts.length > 0;
  $("#job-title").textContent = j.name;
  $("#job-subtitle").textContent = hasAccounts
    ? `${j.accounts.length} compte(s) · ${state.videos.length} vidéo(s)`
    : `Import direct · ${state.videos.length} vidéo(s)`;
  $("#job-status").outerHTML =
    badge(JOB_STATUS_LABEL, j.status).replace("<span", '<span id="job-status"');

  $("#btn-pause").classList.toggle("hidden", !j.running);
  $("#btn-resume").classList.toggle("hidden", j.running || j.status !== "paused");
  $("#btn-retry").classList.toggle("hidden", !(j.stats.failed > 0));

  // Suivi du budget consommé, visible en permanence pendant le job.
  const chip = $("#budget-chip");
  chip.classList.remove("hidden");
  chip.textContent = `${fmtUsd(j.budget.spent_usd)} / ${fmtUsd(j.budget.limit_usd)}`;
  chip.classList.toggle("chip-warn", j.budget.pct !== null && j.budget.pct >= 80);

  $("#scrape-info").textContent = hasAccounts
    ? `${state.videos.length} vidéo(s) · ${j.accounts.join(", ")}`
    : `${state.videos.length} vidéo(s) importée(s)`;
  $("#btn-rescrape").classList.toggle("hidden", !hasAccounts);
  $("#scrape-none").classList.toggle("hidden", hasAccounts);

  applyGeneration(j.generation);
  renderReview();
  renderRun();
  renderResults();
}

function applyGeneration(g) {
  // Un job déjà lancé garde ses propres réglages ; sinon on part des préférences.
  const p = g || state.prefs || {};
  $("#gen-prompt").value = p.prompt || "";
  $("#gen-ratio").value = p.aspect_ratio || "9:16";
  $("#gen-size").value = p.image_size || "2K";
  $("#k-maxdur").value = String(p.max_output_duration_s || 30);
  $("#k-mode").value = p.kling_mode || "pro";
  $("#k-audio").value = String(p.keep_audio ?? true);
  $("#gen-batch").checked = !!p.gemini_batch;
  if (p.reference_image_id) state.refId = p.reference_image_id;
  renderReferences();
}

function goStep(n) {
  state.step = n;
  $$(".step").forEach((b) => b.classList.toggle("active", +b.dataset.step === n));
  $$(".step-panel").forEach((p) => p.classList.toggle("hidden", +p.dataset.panel !== n));
  if (n === 3) updateEstimate();
}
$$(".step").forEach((b) => (b.onclick = () => goStep(+b.dataset.step)));

$("#btn-rescrape").onclick = async () => {
  try {
    await api(`/api/jobs/${state.jobId}/scrape`, { method: "POST" });
    toast("Scraping relancé.", "ok");
  } catch (e) { toast(e.message, "err"); }
};

$("#btn-pause").onclick = async () => {
  await api(`/api/jobs/${state.jobId}/pause`, { method: "POST" });
  toast("Pause demandée. Les tâches Kling déjà soumises restent récupérables.", "ok");
  refreshJob();
};

$("#btn-resume").onclick = async () => {
  try {
    await api(`/api/jobs/${state.jobId}/resume`, { method: "POST" });
    toast("Reprise en cours.", "ok");
    refreshJob();
  } catch (e) { toast(e.message, "err"); }
};

$("#btn-retry").onclick = async () => {
  const r = await api(`/api/jobs/${state.jobId}/retry`, { method: "POST" });
  toast(`${r.reset} vidéo(s) remise(s) en file.`, "ok");
  refreshJob();
};

$("#btn-delete").onclick = async () => {
  if (!confirm("Supprimer ce job et toutes ses données ?")) return;
  await api(`/api/jobs/${state.jobId}`, { method: "DELETE" });
  $("#btn-home").click();
};

// ---------------------------------------------------------------------------
// Étape 1 — import de vidéos
//
// Les fichiers peuvent peser plusieurs centaines de Mo : on passe par XHR, seul
// moyen d'obtenir une progression d'envoi (fetch ne l'expose pas). Sans elle,
// l'interface paraît figée pendant toute la durée du transfert.
// ---------------------------------------------------------------------------

function postForm(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error((body && body.detail) || xhr.statusText || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Envoi interrompu."));
    xhr.send(formData);
  });
}

async function uploadVideos(files) {
  const list = Array.from(files || []);
  if (!list.length || !state.jobId) return;

  const zone = $("#dropzone");
  const bar = $("#upload-progress");
  const report = $("#upload-report");

  const fd = new FormData();
  list.forEach((f) => fd.append("files", f));

  zone.classList.add("busy");
  report.classList.add("hidden");
  bar.classList.remove("hidden");
  bar.firstElementChild.style.width = "0%";

  try {
    const r = await postForm(`/api/jobs/${state.jobId}/upload`, fd, (p) => {
      bar.firstElementChild.style.width = `${Math.round(p * 100)}%`;
    });

    const lines = [];
    if (r.added) lines.push(`<b>${fmtInt(r.added)} vidéo(s) importée(s).</b>`);
    lines.push(...r.errors.map((e) => `<span class="warn-text">${esc(e)}</span>`));
    report.innerHTML = lines.join("<br>") || "Aucun fichier retenu.";
    report.classList.remove("hidden");

    toast(r.added ? `${r.added} vidéo(s) importée(s).` : "Aucun fichier retenu.",
          r.added ? "ok" : "err");
    if (r.added) { await refreshJob(); goStep(2); }
  } catch (e) {
    toast(e.message, "err");
  } finally {
    zone.classList.remove("busy");
    bar.classList.add("hidden");
  }
}

$("#dropzone").onclick = () => $("#upload-files").click();
$("#dropzone").onkeydown = (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    $("#upload-files").click();
  }
};

$("#upload-files").onchange = async (e) => {
  await uploadVideos(e.target.files);
  e.target.value = "";   // re-sélectionner le même fichier doit redéclencher
};

["dragenter", "dragover"].forEach((evt) =>
  $("#dropzone").addEventListener(evt, (e) => {
    e.preventDefault();
    $("#dropzone").classList.add("over");
  }));

["dragleave", "drop"].forEach((evt) =>
  $("#dropzone").addEventListener(evt, (e) => {
    e.preventDefault();
    $("#dropzone").classList.remove("over");
  }));

$("#dropzone").addEventListener("drop", (e) => uploadVideos(e.dataTransfer && e.dataTransfer.files));

// Un fichier lâché à côté de la zone ouvrirait la vidéo dans l'onglet et ferait
// perdre la page.
["dragover", "drop"].forEach((evt) =>
  window.addEventListener(evt, (e) => e.preventDefault()));

// ---------------------------------------------------------------------------
// Étape 2 — validation
// ---------------------------------------------------------------------------

function reviewables() {
  const q = ($("#filter-text").value || "").toLowerCase();
  const plat = $("#filter-platform").value;
  return state.videos.filter((v) => {
    if (["failed", "skipped"].includes(v.state) && !v.selected) return false;
    if (plat && v.platform !== plat) return false;
    if (q && !(`${v.account} ${v.caption}`.toLowerCase().includes(q))) return false;
    return true;
  });
}

/** Récupère les lecteurs ouverts d'une grille avant qu'elle soit reconstruite.
 *
 *  Regénérer leur balise repartirait de zéro : la vidéo se relance, et toutes
 *  celles déjà ouvertes se relancent en même temps. On réutilise donc les nœuds
 *  existants, qui conservent leur position et leur état lecture/pause.
 */
function detachPlayers(grid) {
  const players = new Map();
  grid.querySelectorAll("video.thumb").forEach((el) => {
    const id = el.closest(".vcard") && el.closest(".vcard").dataset.id;
    if (id) players.set(id, el);
  });
  return players;
}

function restorePlayers(grid, players) {
  const kept = new Set();
  grid.querySelectorAll(".vcard").forEach((card) => {
    const el = players.get(card.dataset.id);
    const fresh = card.querySelector("video.thumb");
    if (!el || !fresh) return;
    fresh.replaceWith(el);
    kept.add(card.dataset.id);
  });
  // Un lecteur dont la carte a disparu (filtre, changement d'état) continuerait
  // à jouer son son hors écran.
  players.forEach((el, id) => { if (!kept.has(id)) el.pause(); });
}

function renderReview() {
  const list = reviewables();
  const grid = $("#review-grid");
  const players = detachPlayers(grid);

  if (!list.length) {
    players.forEach((el) => el.pause());
    grid.innerHTML = "<p class='muted'>Aucune vidéo.</p>";
    updateSelectionCount();
    return;
  }

  grid.innerHTML = list.map((v) => {
    const poster = v.has_frame ? `/api/videos/${v.id}/frame` : (v.thumbnail_url || "");
    // Un lecteur ouvert le reste après un re-rendu (filtre, événement SSE).
    // Pas d'`autoplay` ici : ce gabarit ne sert qu'aux cartes dont le nœud n'a
    // pas pu être réutilisé, et rien ne doit démarrer sans clic de l'utilisateur.
    const visual = state.previewing.has(v.id)
      ? `<video class="thumb" src="/api/videos/${v.id}/source" controls
                playsinline preload="metadata" poster="${esc(poster)}"></video>`
      : `${poster
            ? `<img class="thumb" src="${esc(poster)}" loading="lazy" alt="">`
            : `<div class="thumb"></div>`}
         <button type="button" class="play" title="Prévisualiser la vidéo"
                 aria-label="Prévisualiser la vidéo">▶</button>`;
    return `
      <div class="vcard ${state.selection.has(v.id) ? "selected" : ""}" data-id="${v.id}">
        <input type="checkbox" class="pick" ${state.selection.has(v.id) ? "checked" : ""}>
        <div class="state-tag">${badge(STATE_LABEL, v.state)}</div>
        <div class="thumb-wrap">${visual}</div>
        <div class="meta">
          <b>@${esc(v.account)}</b>
          <span>${v.platform} · ${fmtInt(v.view_count)} vues · ${fmtDuration(v.duration_s)}</span>
        </div>
      </div>`;
  }).join("");

  restorePlayers(grid, players);

  grid.querySelectorAll(".vcard").forEach((card) => {
    const id = card.dataset.id;
    const box = card.querySelector(".pick");
    const toggle = () => {
      state.selection.has(id) ? state.selection.delete(id) : state.selection.add(id);
      box.checked = state.selection.has(id);
      card.classList.toggle("selected", state.selection.has(id));
      updateSelectionCount();
    };
    // Piloter le lecteur ne doit pas (dé)sélectionner la vidéo.
    card.onclick = (e) => {
      if (e.target === box || e.target.closest("video, .play")) return;
      toggle();
    };
    box.onclick = (e) => { e.stopPropagation(); toggle(); };
    const play = card.querySelector(".play");
    if (play) play.onclick = (e) => { e.stopPropagation(); openPreview(card, id); };
  });

  updateSelectionCount();
}

/** Rapatrie la vidéo si besoin, puis remplace la vignette par un lecteur. */
async function openPreview(card, videoId) {
  const btn = card.querySelector(".play");
  const wrap = card.querySelector(".thumb-wrap");
  if (!btn || !wrap) return;

  btn.disabled = true;
  btn.textContent = "…";
  btn.title = "Téléchargement de la vidéo…";
  try {
    // Une vidéo scrapée n'est pas encore sur disque à ce stade : le serveur la
    // récupère à la demande. Gratuit, et la génération n'aura plus à le faire.
    await api(`/api/videos/${videoId}/prepare`, { method: "POST" });
  } catch (e) {
    toast(`Prévisualisation impossible : ${e.message}`, "err");
    btn.disabled = false;
    btn.textContent = "▶";
    btn.title = "Prévisualiser la vidéo";
    return;
  }

  const poster = wrap.querySelector("img")?.getAttribute("src") || "";
  const player = document.createElement("video");
  player.className = "thumb";
  player.src = `/api/videos/${videoId}/source`;
  player.controls = true;
  player.autoplay = true;
  player.playsInline = true;
  player.preload = "auto";
  if (poster) player.poster = poster;
  player.onerror = () => {
    toast("Le fichier vidéo n'a pas pu être lu.", "err");
    state.previewing.delete(videoId);
  };

  state.previewing.add(videoId);
  wrap.innerHTML = "";
  wrap.appendChild(player);
}

function updateSelectionCount() {
  $("#selection-count").textContent = `${state.selection.size} sélectionnée(s)`;
}

$("#filter-text").oninput = renderReview;
$("#filter-platform").onchange = renderReview;
$("#btn-all").onclick = () => { reviewables().forEach((v) => state.selection.add(v.id)); renderReview(); };
$("#btn-none").onclick = () => { state.selection.clear(); renderReview(); };

$("#btn-validate").onclick = async () => {
  try {
    await api(`/api/jobs/${state.jobId}/selection`, {
      method: "POST", body: JSON.stringify({ video_ids: [...state.selection] }),
    });
    toast(`${state.selection.size} vidéo(s) validée(s).`, "ok");
    await refreshJob();
    goStep(3);
  } catch (e) { toast(e.message, "err"); }
};

// ---------------------------------------------------------------------------
// Images de référence
// ---------------------------------------------------------------------------

async function loadReferences() {
  state.references = await api("/api/references");
  if (!state.refId && state.references.length) state.refId = state.references[0].id;
  renderReferences();
}

function refListHtml(activeId) {
  if (!state.references.length) return "<p class='muted'>Aucune image.</p>";
  // Conteneur en <div> et non en <button> : on ne peut pas imbriquer un bouton
  // de suppression dans un bouton de selection.
  return state.references.map((r) => `
    <div class="ref-item ${r.id === activeId ? "active" : ""}"
         data-id="${r.id}" title="${esc(r.filename)}" role="button" tabindex="0">
      <img src="${r.url}" alt="${esc(r.filename)}">
      <button type="button" class="ref-del" data-del="${r.id}"
              title="Supprimer ${esc(r.filename)}" aria-label="Supprimer">×</button>
    </div>`).join("");
}

async function deleteReference(refId) {
  const ref = state.references.find((r) => r.id === refId);
  if (!confirm(`Supprimer définitivement « ${ref ? ref.filename : refId} » ?`)) return;
  try {
    await api(`/api/references/${refId}`, { method: "DELETE" });
    if (state.refId === refId) state.refId = null;
    if (state.prefs && state.prefs.reference_image_id === refId) {
      state.prefs.reference_image_id = "";
    }
    await loadReferences();
    toast("Image supprimée.", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

function wireRefList(container, onPick) {
  container.querySelectorAll(".ref-del").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();          // ne pas selectionner en supprimant
      deleteReference(b.dataset.del);
    };
  });
  container.querySelectorAll(".ref-item").forEach((n) => {
    n.onclick = () => onPick(n.dataset.id);
  });
}

function renderReferences() {
  const genList = $("#ref-list");
  genList.innerHTML = refListHtml(state.refId);
  wireRefList(genList, (id) => { state.refId = id; renderReferences(); });

  const settingsList = $("#set-ref-list");
  const prefRef = state.prefs?.reference_image_id || "";
  settingsList.innerHTML = refListHtml(prefRef);
  wireRefList(settingsList, (id) => {
    if (state.prefs) state.prefs.reference_image_id = id;
    renderReferences();
  });
}

async function uploadReference(file, setAsDefault) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await api("/api/references", { method: "POST", body: fd });
  await loadReferences();
  if (setAsDefault) state.prefs.reference_image_id = r.id;
  else state.refId = r.id;
  renderReferences();
  toast("Image ajoutée.", "ok");
}

$("#btn-upload-ref").onclick = () => $("#ref-file").click();
$("#ref-file").onchange = async (e) => {
  const file = e.target.files[0];
  if (file) { try { await uploadReference(file, false); } catch (err) { toast(err.message, "err"); } }
  e.target.value = "";
};

$("#btn-set-upload-ref").onclick = () => $("#set-ref-file").click();
$("#set-ref-file").onchange = async (e) => {
  const file = e.target.files[0];
  if (file) { try { await uploadReference(file, true); } catch (err) { toast(err.message, "err"); } }
  e.target.value = "";
};

// ---------------------------------------------------------------------------
// Étape 3 — génération
// ---------------------------------------------------------------------------

function generationPayload() {
  return {
    prompt: $("#gen-prompt").value.trim(),
    reference_image_id: state.refId || "",
    aspect_ratio: $("#gen-ratio").value,
    image_size: $("#gen-size").value,
    max_output_duration_s: +$("#k-maxdur").value || 30,
    kling_mode: $("#k-mode").value,
    keep_audio: $("#k-audio").value === "true",
    gemini_batch: $("#gen-batch").checked,
  };
}

async function updateEstimate() {
  const g = generationPayload();
  if (!g.prompt) g.prompt = "-";
  if (!g.reference_image_id) g.reference_image_id = "-";
  try {
    const e = await api(`/api/jobs/${state.jobId}/estimate`, {
      method: "POST", body: JSON.stringify(g),
    });
    const unknown = e.unknown_durations
      ? `<br><span class="warn-text">${fmtInt(e.unknown_durations)} de durée inconnue,
         comptée(s) au plafond.</span>`
      : "";
    $("#estimate-box").innerHTML = `
      <div class="big">${fmtUsd(e.total_with_retries_usd)}</div>
      <div class="muted">
        ${fmtInt(e.count)} vidéo(s) × ${fmtUsd(e.unit_with_retries_usd)} · retries inclus<br>
        Durée moyenne ${e.avg_duration_s} s · sans retries ${fmtUsd(e.total_usd)}
        ${unknown}
      </div>`;
  } catch (e) {
    $("#estimate-box").textContent = e.message;
  }
}

["#k-maxdur", "#k-mode", "#gen-size", "#gen-batch"].forEach((s) => {
  $(s).addEventListener("change", updateEstimate);
});

$("#btn-run").onclick = async () => {
  const g = generationPayload();
  if (!g.prompt) return toast("Le prompt Nano Banana Pro est obligatoire.", "err");
  if (!g.reference_image_id) return toast("Sélectionne une image de référence.", "err");

  try {
    await api(`/api/jobs/${state.jobId}/run`, {
      method: "POST", body: JSON.stringify({ generation: g }),
    });
    toast("Génération lancée.", "ok");
    refreshJob();
  } catch (e) { toast(e.message, "err"); }
};

const PIPE_STATES = ["discovered", "downloaded", "framed", "edit_batched",
                     "edited", "motion_submitted"];

function renderRun() {
  const active = state.videos.filter(
    (v) => v.selected && [...PIPE_STATES, "done", "failed", "skipped"].includes(v.state));
  const done = active.filter((v) => ["done", "failed", "skipped"].includes(v.state)).length;
  const pct = active.length ? (done / active.length) * 100 : 0;

  $("#progress-bar").firstElementChild.style.width = `${pct}%`;
  const s = state.job?.stats || {};
  $("#progress-summary").textContent =
    `${done}/${active.length} · ${s.done || 0} OK · ${s.failed || 0} échec(s) · ${s.skipped || 0} écartée(s)`;

  $("#run-list").innerHTML = active.slice(0, 200).map((v) => `
    <div class="run-row" data-id="${v.id}">
      <span class="who">@${esc(v.account)} · ${esc(v.external_id)}</span>
      <span>${v.error ? `<span class="muted" title="${esc(v.error)}">${esc(v.error.slice(0, 60))}</span> ` : ""}${badge(STATE_LABEL, v.state)}</span>
    </div>`).join("");
}

// ---------------------------------------------------------------------------
// Étape 4 — résultats
// ---------------------------------------------------------------------------

function renderResults() {
  const done = state.videos.filter((v) => v.state === "done");
  const grid = $("#results-grid");
  // Même précaution qu'à la validation : pendant un job, cette grille est
  // reconstruite à chaque événement du pipeline.
  const players = detachPlayers(grid);

  if (!done.length) {
    players.forEach((el) => el.pause());
    grid.innerHTML = "<p class='muted'>Aucun résultat.</p>";
    return;
  }
  grid.innerHTML = done.map((v) => `
    <div class="vcard" data-id="${v.id}">
      <video class="thumb" src="/api/videos/${v.id}/output" controls preload="metadata" playsinline></video>
      <div class="meta">
        <b>@${esc(v.account)}</b>
        <span>${v.platform} · ${fmtUsd(v.cost_usd)}</span>
      </div>
      <div class="links">
        <a href="/api/videos/${v.id}/output" download>Télécharger</a>
        <a href="/api/videos/${v.id}/edited" target="_blank" rel="noopener">Image NB Pro</a>
        <a href="/api/videos/${v.id}/frame" target="_blank" rel="noopener">Frame</a>
      </div>
    </div>`).join("");

  restorePlayers(grid, players);
}

$("#btn-download-all").onclick = () => {
  window.location.href = `/api/jobs/${state.jobId}/download`;
};

// ---------------------------------------------------------------------------
// Flux temps réel
// ---------------------------------------------------------------------------

function appendLog(msg, level) {
  const el = $("#log");
  const row = document.createElement("div");
  row.className = level || "";
  row.textContent = `${new Date().toLocaleTimeString("fr-FR")}  ${msg}`;
  el.appendChild(row);
  while (el.childElementCount > 400) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

let refreshTimer = null;
function scheduleRefresh() {
  // Le pipeline émet beaucoup d'événements : on regroupe les rafraîchissements.
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => { if (state.jobId) refreshJob(); }, 900);
}

function connectStream() {
  const es = new EventSource("/api/stream");

  es.onmessage = (ev) => {
    let payload;
    try { payload = JSON.parse(ev.data); } catch { return; }
    if (payload.job_id && payload.job_id !== state.jobId) return;

    if (payload.type === "log") {
      appendLog(payload.message, payload.level);
      scheduleRefresh();
    } else if (payload.type === "video_state") {
      const v = state.videos.find((x) => x.id === payload.video_id);
      if (v) { v.state = payload.state; renderRun(); }
      scheduleRefresh();
    } else if (payload.type === "progress") {
      scheduleRefresh();
    }
  };

  es.onerror = () => {
    es.close();
    setTimeout(connectStream, 3000);
  };
}

// ---------------------------------------------------------------------------
// Démarrage
// ---------------------------------------------------------------------------

(async () => {
  await loadPrefs();
  await loadReferences();
  loadHealth();
  loadJobs();
  connectStream();
})();
