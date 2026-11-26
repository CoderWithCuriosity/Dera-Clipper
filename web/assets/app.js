/* AI Clipper interface. Plain JavaScript, no build step.
   Anything that comes from the server, a user, or the language model is escaped before display. */
(function () {
  "use strict";

  const app = document.getElementById("app");
  const session = { token: null, user: null, authRequired: false };
  let pollTimer = null;
  let toastTimer = null;
  let libSelected = null;

  // -- helpers --------------------------------------------------------------------
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  const ICONS = {
    film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    upload: '<path d="M12 16V4M7 9l5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    layers: '<path d="M12 3 2 8l10 5 10-5-10-5Z"/><path d="m2 13 10 5 10-5"/>',
    stack: '<rect x="3" y="3" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="7" rx="1.5"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M3 20h18"/>',
    sliders: '<path d="M4 6h8M16 6h4M4 12h4M12 12h8M4 18h10M18 18h2"/><circle cx="14" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="16" cy="18" r="2"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.3-5 6.5-5s5.7 1.5 6.5 5M16 4.5a3.5 3.5 0 0 1 0 7M18 15c2 .7 3.2 2.3 3.5 5"/>',
    shield: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3Z"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
    play: '<path d="M7 5v14l12-7-12-7Z"/>',
    download: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    alert: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>',
    sparkle: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
    link: '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>',
    music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    key: '<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 9.2-9.2M16 7l3 3M14 9l2 2"/>',
    shieldCheck: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3Z"/><path d="m9 12 2 2 4-4"/>',
  };

  function icon(name, extra) {
    return `<svg class="i ${extra || ""}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
  }

  function fmtDuration(seconds) {
    if (seconds == null) return "-";
    const s = Math.round(seconds);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return (h ? h + ":" : "") + String(m).padStart(h ? 2 : 1, "0") + ":" + String(r).padStart(2, "0");
  }

  function fmtSize(bytes) {
    if (!bytes) return "-";
    if (bytes > 1e9) return (bytes / 1e9).toFixed(2) + " GB";
    return (bytes / 1e6).toFixed(1) + " MB";
  }

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? esc(iso) : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }

  const STATUS = {
    queued: ["Queued", "work"], acquiring: ["Downloading", "work"], preparing_audio: ["Preparing audio", "work"],
    transcribing: ["Transcribing", "work"], awaiting_settings: ["Ready for settings", "warn"],
    planning: ["Choosing moments", "work"], rendering: ["Rendering", "work"], done: ["Done", "ok"], failed: ["Failed", "bad"],
  };
  const ACTIVE = ["queued", "acquiring", "preparing_audio", "transcribing", "planning", "rendering"];
  const STEPS = ["Download", "Transcribe", "Settings", "Plan", "Render", "Done"];

  function statusBadge(status) {
    const [label, tone] = STATUS[status] || [status, ""];
    return `<span class="badge ${tone}">${esc(label)}</span>`;
  }

  function progressBar(value, active) {
    const pct = Math.max(0, Math.min(100, Math.round((value || 0) * 100)));
    return `<div class="progress ${active ? "active" : ""}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${pct}%"></span></div>`;
  }

  function stepper(status) {
    let current = -1;
    if (["queued", "acquiring"].includes(status)) current = 0;
    else if (["preparing_audio", "transcribing"].includes(status)) current = 1;
    else if (status === "awaiting_settings") current = 2;
    else if (status === "planning") current = 3;
    else if (status === "rendering") current = 4;
    else if (status === "done") current = 5;
    return `<div class="steps">${STEPS.map((label, i) => {
      const cls = status === "done" ? "done" : status === "failed" ? (i <= 0 ? "done" : "") : i < current ? "done" : i === current ? "current" : "";
      const mark = cls === "done" ? icon("check") : `<span>${i + 1}</span>`;
      return `<div class="step ${cls}"><div class="dot">${mark}</div>${label}</div>`;
    }).join("")}</div>`;
  }

  function toast(message) {
    const el = document.getElementById("toast");
    el.innerHTML = `${icon("sparkle")}<span>${esc(message)}</span>`;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 3800);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function setNav(name) {
    document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  }

  function setPage(html) {
    app.innerHTML = `<div class="fade-in">${html}</div>`;
  }

  function emptyState(iconName, title, text, action) {
    return `<div class="card empty"><div class="bubble lg">${icon(iconName, "i-lg")}</div><h3>${esc(title)}</h3><p>${text}</p>${action || ""}</div>`;
  }

  // -- session and API -----------------------------------------------------------------
  async function refreshSession() {
    const res = await fetch("/api/auth/refresh", { method: "POST", credentials: "same-origin" });
    if (!res.ok) return false;
    const data = await res.json();
    session.token = data.access_token;
    session.user = data.user;
    return true;
  }

  async function api(path, options, retried) {
    const opts = Object.assign({ method: "GET", headers: {} }, options || {});
    opts.credentials = "same-origin";
    if (session.token) opts.headers.Authorization = "Bearer " + session.token;
    if (opts.json !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
    }
    const res = await fetch("/api" + path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (res.status === 401 && session.authRequired && !retried && !path.startsWith("/auth/")) {
      if (await refreshSession()) return api(path, options, true);
      session.token = null;
      session.user = null;
      location.hash = "#/login";
      throw new Error("Sign in to continue");
    }
    if (!res.ok) {
      const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText;
      throw new Error(detail || "Request failed");
    }
    return data;
  }

  function renderNav() {
    const isAdmin = session.user && session.user.role === "admin";
    document.querySelectorAll("[data-admin-only]").forEach((el) => { el.hidden = !isAdmin; });
    const signedIn = !!session.user;
    document.querySelectorAll("[data-signed-in]").forEach((el) => { el.hidden = !(signedIn && session.authRequired); });
    // With sign-in on, the menu only appears once someone is signed in.
    const locked = session.authRequired && !signedIn;
    const mainNav = document.getElementById("mainNav");
    if (mainNav) mainNav.hidden = locked;
    const menuBtn = document.getElementById("menuToggle");
    if (menuBtn) menuBtn.hidden = locked;
    const who = document.getElementById("who");
    if (who) {
      who.hidden = locked;
      who.textContent = signedIn && session.authRequired ? session.user.email : (isAdmin ? "Local profile" : "");
    }
  }

  // -- theme (light and dark) ---------------------------------------------------------------
  function applyTheme(choice) {
    if (choice) document.documentElement.setAttribute("data-theme", choice);
    else document.documentElement.removeAttribute("data-theme");
    const dark = choice === "dark" || (!choice && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    const btn = document.getElementById("themeToggle");
    if (btn) btn.innerHTML = icon(dark ? "sun" : "moon");
    btn && btn.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("clipper-theme"); } catch (_) { saved = null; }
    applyTheme(saved);
    const btn = document.getElementById("themeToggle");
    btn.addEventListener("click", () => {
      const isDark = document.documentElement.getAttribute("data-theme") === "dark" ||
        (!document.documentElement.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
      const next = isDark ? "light" : "dark";
      try { localStorage.setItem("clipper-theme", next); } catch (_) { /* private mode */ }
      applyTheme(next);
    });
  }

  // -- drop zones ----------------------------------------------------------------------------
  function wireDropzone(zone) {
    const input = zone.querySelector("input[type=file]");
    const label = zone.querySelector("[data-file-name]");
    const show = () => { if (label) label.textContent = input.files[0] ? input.files[0].name : ""; };
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove("over")));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      if (e.dataTransfer && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        show();
      }
    });
  }

  // -- mobile menu -----------------------------------------------------------
  function closeMenu() {
    const nav = document.getElementById("mainNav");
    const btn = document.getElementById("menuToggle");
    if (nav) nav.classList.remove("open");
    if (btn) { btn.setAttribute("aria-expanded", "false"); btn.setAttribute("aria-label", "Open menu"); }
  }

  function initMenu() {
    const btn = document.getElementById("menuToggle");
    const nav = document.getElementById("mainNav");
    btn.addEventListener("click", () => {
      const open = !nav.classList.contains("open");
      nav.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", String(open));
      btn.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
    document.addEventListener("click", (e) => {
      if (!nav.classList.contains("open")) return;
      if (!nav.contains(e.target) && !btn.contains(e.target)) closeMenu();
    });
  }

  // -- sign in and account -------------------------------------------------------------------
  function viewLogin() {
    setNav("");
    stopPolling();
    app.innerHTML = `
      <div class="auth-shell fade-in">
        <section class="auth-hero">
          <span class="badge" style="background:rgba(255,255,255,.14);color:#fff">Clip studio</span>
          <h1>Turn long videos into clips people watch.</h1>
          <p>Choose a theme, set the length, and let the moments you want rise to the top. Captions, framing, and sound are handled for you.</p>
          <ul>
            <li>${icon("sparkle")} Smart moment picking, with a reason for every clip</li>
            <li>${icon("film")} Vertical, square, or landscape in 1080p or 720p</li>
            <li>${icon("music")} Sound effects and B-roll that you control</li>
          </ul>
        </section>
        <section class="auth-card-wrap">
          <div class="card auth-card">
            <h2>Welcome back</h2>
            <p class="muted small" style="margin-bottom:18px">Sign in or create an account to get started.</p>
            <div class="seg" role="tablist">
              <button class="on" id="tabLogin" type="button">Sign in</button>
              <button id="tabRegister" type="button">Create account</button>
            </div>
            <form id="authForm" class="stack">
              <div><label for="a_email">Email</label><input id="a_email" type="email" autocomplete="email" required></div>
              <div><label for="a_pass">Password</label><input id="a_pass" type="password" autocomplete="current-password" minlength="10" required></div>
              <p class="field-hint" id="authHint"></p>
              <button type="submit" id="authBtn" style="width:100%">Sign in</button>
            </form>
          </div>
        </section>
      </div>`;
    let mode = "login";
    const setMode = (m) => {
      mode = m;
      document.getElementById("tabLogin").classList.toggle("on", m === "login");
      document.getElementById("tabRegister").classList.toggle("on", m === "register");
      document.getElementById("authBtn").textContent = m === "login" ? "Sign in" : "Create account";
      document.getElementById("a_pass").autocomplete = m === "login" ? "current-password" : "new-password";
      document.getElementById("authHint").textContent = m === "login" ? "" : "Passwords need at least 10 characters.";
    };
    document.getElementById("tabLogin").addEventListener("click", () => setMode("login"));
    document.getElementById("tabRegister").addEventListener("click", () => setMode("register"));
    setMode("login");
    document.getElementById("authForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const body = { email: document.getElementById("a_email").value, password: document.getElementById("a_pass").value };
      try {
        const data = await api(mode === "login" ? "/auth/login" : "/auth/register", { method: "POST", json: body });
        session.token = data.access_token;
        session.user = data.user;
        renderNav();
        location.hash = "#/projects";
        if (location.hash === "#/projects") route();
      } catch (err) { toast(err.message); }
    });
  }

  function viewAccount() {
    setNav("account");
    stopPolling();
    setPage(`
      <div class="page-head"><div><span class="eyebrow">Your account</span><h1>Account</h1>
        <p>${session.user ? `Signed in as ${esc(session.user.email)}` : "Sign-in is off in this mode. The app runs as a single local profile."}</p></div></div>
      ${session.authRequired ? `
      <div class="split">
        <form id="pwForm" class="card stack">
          <div class="card-head"><div class="row"><div class="bubble">${icon("key")}</div><div><h2>Change password</h2><div class="desc">You will be signed out on other devices.</div></div></div></div>
          <div><label for="pw_old">Current password</label><input id="pw_old" type="password" autocomplete="current-password" required></div>
          <div><label for="pw_new">New password</label><input id="pw_new" type="password" autocomplete="new-password" minlength="10" required></div>
          <div><button type="submit">Update password</button></div>
        </form>
        <div class="card stack">
          <div class="card-head"><div class="row"><div class="bubble warn">${icon("logout")}</div><div><h2>Sign out</h2><div class="desc">End this session on this device.</div></div></div></div>
          <div><button class="secondary" id="logoutBtn" type="button">Sign out</button></div>
        </div>
      </div>` : ""}`);
    if (session.authRequired) {
      document.getElementById("pwForm").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        try {
          const r = await api("/auth/change-password", { method: "POST", json: {
            current_password: document.getElementById("pw_old").value,
            new_password: document.getElementById("pw_new").value,
          }});
          toast(r.message);
          ev.target.reset();
        } catch (err) { toast(err.message); }
      });
      document.getElementById("logoutBtn").addEventListener("click", signOut);
    }
  }

  async function signOut() {
    try { await api("/auth/logout", { method: "POST" }); } catch (_) { /* already signed out */ }
    session.token = null;
    session.user = null;
    renderNav();
    location.hash = "#/login";
  }

  // -- projects --------------------------------------------------------------------------------
  async function viewProjects() {
    setNav("projects");
    stopPolling();
    setPage(`<div class="page-head"><div><span class="eyebrow">Workspace</span><h1>Projects</h1>
      <p>Each project is one long video. Open one to choose a theme and get clips.</p></div>
      <div class="page-actions"><a class="btn" href="#/new">${icon("plus")} New project</a></div></div>
      <div id="stats" class="stats"></div><div id="list"></div>`);
    const data = await api("/projects");
    const projects = data.projects;
    const ready = projects.filter((p) => p.status === "done").length;
    const working = projects.filter((p) => ACTIVE.includes(p.status)).length;
    document.getElementById("stats").innerHTML = [
      ["Projects", projects.length, "film"], ["Ready", ready, "check"], ["In progress", working, "refresh"],
    ].map(([label, value, ic]) => `<div class="card stat"><div class="row between"><span class="label">${label}</span>${icon(ic, "")}</div><div class="value mono">${value}</div></div>`).join("");
    const list = document.getElementById("list");
    if (!projects.length) {
      list.innerHTML = emptyState("upload", "No projects yet", "Upload a video or paste a link to make your first clips.",
        `<a class="btn" href="#/new">${icon("plus")} Create a project</a>`);
      return;
    }
    list.innerHTML = `<div class="grid">${projects.map((p) => `
      <a class="card card-link" href="#/project/${esc(p.id)}">
        <div class="card-head">
          <div class="row grow" style="gap:12px"><div class="bubble">${icon("film")}</div>
            <div class="grow-text"><h3 class="clamp-2">${esc(p.title)}</h3>
            <div class="desc small muted">${fmtDuration(p.duration)} · ${p.source_type === "link" ? "Link" : "Upload"} · ${fmtDate(p.created_at)}</div></div>
          </div>
          ${statusBadge(p.status)}
        </div>
        ${ACTIVE.includes(p.status) ? progressBar(p.progress, true) : ""}
        <p class="small muted" style="margin-top:10px">${esc(p.status_message || "")}</p>
      </a>`).join("")}</div>`;
  }

  async function viewNew() {
    setNav("new");
    stopPolling();
    const health = await api("/health");
    const youtubeNote = health.platform_links
      ? `<div class="notice warn">${icon("alert")}<span>YouTube links are on for this server. Use them only for videos you own or have permission to process. YouTube's terms do not allow downloads, so this may stop working if YouTube blocks the server.</span></div>`
      : "";
    setPage(`
      <div class="page-head"><div><span class="eyebrow">New</span><h1>New project</h1>
        <p>Add the long video you want to cut into clips. Uploads are checked before anything else happens.</p></div></div>
      <form id="newForm" class="card stack" style="max-width:820px">
        <div class="field-grid">
          <div style="grid-column:1/-1"><label for="title">Title</label><input id="title" maxlength="120" placeholder="For example: Podcast episode 12"></div>
        </div>
        <div class="dropzone" id="dropFile">
          ${icon("upload", "i-lg")}
          <div class="title">Drop a video here, or click to choose one</div>
          <div class="field-hint">MP4, MOV, WEBM or MKV</div>
          <div class="file" data-file-name></div>
          <input id="file" type="file" accept=".mp4,.mov,.webm,.mkv,video/*">
        </div>
        <div class="row" style="justify-content:center;color:var(--muted);font-size:.85rem"><span>or paste a link</span></div>
        <div>
          <label for="url">Video link</label>
          <input id="url" type="url" placeholder="https://example.com/video.mp4">
          <p class="field-hint">Direct video file links work. Web pages that are not video files are rejected.</p>
        </div>
        ${youtubeNote}
        <label class="check"><input id="rights" type="checkbox"> I own this content or have permission to process it.</label>
        <div class="row"><button type="submit" id="createBtn">${icon("plus")} Create project</button><span class="muted small" id="newStatus"></span></div>
      </form>`);
    wireDropzone(document.getElementById("dropFile"));
    document.getElementById("newForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const file = document.getElementById("file").files[0];
      const url = document.getElementById("url").value.trim();
      const status = document.getElementById("newStatus");
      if (!document.getElementById("rights").checked) { toast("Please confirm you have the rights to this content."); return; }
      if (!file && !url) { toast("Choose a file or enter a link."); return; }
      if (file && url) { toast("Use either a file or a link, not both."); return; }
      const form = new FormData();
      if (file) form.append("file", file);
      if (url) form.append("url", url);
      form.append("title", document.getElementById("title").value);
      const btn = document.getElementById("createBtn");
      btn.disabled = true;
      status.textContent = file ? "Uploading..." : "Starting...";
      try {
        const project = await api("/projects", { method: "POST", body: form });
        location.hash = "#/project/" + project.id;
      } catch (err) {
        status.textContent = "";
        toast(err.message);
      } finally {
        btn.disabled = false;
      }
    });
  }

  async function viewProject(id) {
    setNav("projects");
    stopPolling();
    const render = async () => {
      let project;
      try {
        project = await api("/projects/" + encodeURIComponent(id));
      } catch (err) {
        stopPolling();
        setPage(`<div class="notice error">${icon("alert")}<span>${esc(err.message)}</span></div>`);
        return;
      }
      const active = ACTIVE.includes(project.status);
      const [themes, s] = await Promise.all([api("/themes"), api("/settings")]);
      const v = s.values;
      const cur = {
        theme: project.theme || v.default_theme,
        clip_length: project.clip_length || v.default_clip_length,
        clip_count: project.clip_count || v.default_clip_count,
        captions_style: project.captions_style || v.default_captions_style,
        orientation: project.orientation || v.default_orientation,
        resolution: project.resolution || v.default_resolution,
        mirror: project.theme ? project.mirror : v.default_mirror,
        effects: project.effects_enabled !== false,
      };
      const done = (project.clips || []).filter((c) => c.status === "done").length;
      const settingsForm = project.has_transcript ? `
        <form id="genForm" class="card stack">
          <div class="card-head"><div class="row"><div class="bubble">${icon("sliders")}</div>
            <div><h2>Clip settings</h2><div class="desc">Choose how the clips should look and sound.</div></div></div></div>
          <div class="field-grid">
            <div><label for="theme">Theme</label><select id="theme">${themes.themes.map((t) => `<option ${t === cur.theme ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></div>
            <div><label for="clip_length">Clip length (seconds)</label><input id="clip_length" type="number" min="1" step="1" max="${Math.floor(project.duration)}" value="${esc(Math.round(cur.clip_length))}" required>
              <p class="field-hint">Up to ${fmtDuration(project.duration)}</p></div>
            <div><label for="clip_count">Number of clips</label><input id="clip_count" type="number" min="1" max="20" value="${esc(cur.clip_count)}" required></div>
            <div><label for="orientation">Orientation</label><select id="orientation">
              ${[["landscape", "Landscape 16:9"], ["vertical", "Vertical 9:16"], ["square", "Square 1:1"]]
                .map(([val, lab]) => `<option value="${val}" ${val === cur.orientation ? "selected" : ""}>${esc(lab)}</option>`).join("")}</select></div>
            <div><label for="resolution">Resolution</label><select id="resolution">
              <option value="1080p" ${cur.resolution === "1080p" ? "selected" : ""}>1080p</option>
              <option value="720p" ${cur.resolution === "720p" ? "selected" : ""}>720p (faster)</option></select></div>
            <div><label for="captions_style">Captions</label><select id="captions_style">
              ${[["classic", "Classic"], ["bold", "Bold, yellow"], ["boxed", "Boxed"], ["karaoke", "Karaoke highlight"], ["off", "No captions"]]
                .map(([val, lab]) => `<option value="${val}" ${val === cur.captions_style ? "selected" : ""}>${esc(lab)}</option>`).join("")}</select></div>
          </div>
          <div class="toggle-row">
            <label class="check"><input id="mirror" type="checkbox" ${cur.mirror ? "checked" : ""}> Mirror the video</label>
            <label class="check"><input id="effects" type="checkbox" ${cur.effects ? "checked" : ""}> Add sound effects and B-roll</label>
          </div>
          <div class="row">
            <button type="submit">${icon("sparkle")} ${project.clips && project.clips.length ? "Regenerate clips" : "Generate clips"}</button>
            <span class="small muted">The first available language model picks the moments. Without one, speech density picks them.</span>
          </div>
        </form>` : "";

      const clips = (project.clips || []).map((c) => `
        <article class="card clip">
          <div class="clip-media">
            ${c.video_url ? `<video controls preload="metadata" playsinline src="${esc(c.video_url)}"></video>`
              : `<div class="media-placeholder">${c.status === "failed" ? `<span class="badge bad">Failed</span>` : `<span class="badge work">Rendering</span>`}</div>`}
          </div>
          <div class="clip-meta">
            <div class="clip-top">
              <div class="row" style="gap:8px"><strong>Clip ${c.idx}</strong><span class="tag">${fmtDuration(c.start)} to ${fmtDuration(c.end)}</span></div>
              ${c.score != null ? `<span class="tag score">Score ${c.score.toFixed(2)}</span>` : ""}
            </div>
            <p class="clip-reason small">${esc(c.reason || "")}</p>
            ${(c.effects || []).length ? `<div class="clip-facts">${c.effects.map((e) => `<span class="tag accent">${esc(e.effect_id.split("/").slice(-1)[0])} at ${Math.round(e.at)} s</span>`).join("")}</div>` : ""}
            ${c.error ? `<div class="notice error">${icon("alert")}<span>${esc(c.error)}</span></div>` : ""}
            <div class="row">
              ${c.download_url ? `<a class="btn sm" href="${esc(c.download_url)}">${icon("download")} Download</a>` : ""}
            </div>
            ${c.status === "done" ? `
            <div class="metrics">
              <div><label for="v_${esc(c.id)}">Views</label><input id="v_${esc(c.id)}" data-views="${esc(c.id)}" type="number" min="0" value="${esc(c.views == null ? "" : c.views)}"></div>
              <div><label for="l_${esc(c.id)}">Likes</label><input id="l_${esc(c.id)}" data-likes="${esc(c.id)}" type="number" min="0" value="${esc(c.likes == null ? "" : c.likes)}"></div>
              <button class="secondary sm" data-save-metrics="${esc(c.id)}">${icon("check")} Save results</button>
            </div>` : ""}
            ${c.status !== "pending" && c.status !== "rendering" ? `
            <div class="clip-tools">
              <div class="row">
                <select data-r-caps="${esc(c.id)}" aria-label="Captions">${optionList(CAPTION_OPTIONS, (c.settings || {}).captions_style)}</select>
                <select data-r-orient="${esc(c.id)}" aria-label="Orientation">${optionList(ORIENTATION_OPTIONS, (c.settings || {}).orientation)}</select>
                <label class="check"><input type="checkbox" data-r-mirror="${esc(c.id)}" ${(c.settings || {}).mirror ? "checked" : ""}> Mirror</label>
              </div>
              <div><button class="secondary sm" data-regen="${esc(c.id)}">${icon("refresh")} Regenerate this clip</button></div>
            </div>` : ""}
          </div>
        </article>`).join("");

      setPage(`
        <div class="page-head">
          <div><span class="eyebrow">Project</span><h1>${esc(project.title)}</h1>
            <p>${fmtDuration(project.duration)} · ${fmtSize(project.file_size)} · ${esc(project.mode)} mode${project.provider_used ? " · planned by " + esc(project.provider_used) : ""}</p></div>
          <div class="page-actions">${statusBadge(project.status)}
            ${project.status === "failed" ? `<button id="retryBtn" class="secondary">${icon("refresh")} Retry</button>` : ""}
            ${done ? `<a class="btn secondary" href="${esc(project.zip_url)}">${icon("download")} Download all</a>` : ""}
            <button class="danger secondary" id="deleteBtn" ${active ? "disabled" : ""}>${icon("trash")} Delete</button>
          </div>
        </div>
        <section class="card stack" style="margin-bottom:18px">
          ${stepper(project.status)}
          <p class="small muted">${esc(project.status_message || "")}</p>
          ${progressBar(project.progress, active)}
          ${project.error ? `<div class="notice error">${icon("alert")}<span>${esc(project.error)}</span></div>` : ""}
        </section>
        ${settingsForm}
        ${clips ? `<div class="page-head" style="margin:28px 0 14px"><div><h2>Clips</h2><p>${done} of ${project.clips.length} ready</p></div></div><div class="clip-list">${clips}</div>` : ""}`);

      if (project.has_transcript) {
        document.getElementById("genForm").addEventListener("submit", async (ev) => {
          ev.preventDefault();
          try {
            await api(`/projects/${encodeURIComponent(id)}/generate`, { method: "POST", json: {
              theme: document.getElementById("theme").value,
              clip_length: Number(document.getElementById("clip_length").value),
              clip_count: Number(document.getElementById("clip_count").value),
              mirror: document.getElementById("mirror").checked,
              effects: document.getElementById("effects").checked,
              captions_style: document.getElementById("captions_style").value,
              orientation: document.getElementById("orientation").value,
              resolution: document.getElementById("resolution").value,
            }});
            toast("Generation started");
            render();
          } catch (err) { toast(err.message); }
        });
      }
      const retry = document.getElementById("retryBtn");
      if (retry) retry.addEventListener("click", async () => {
        try { await api(`/projects/${encodeURIComponent(id)}/retry`, { method: "POST" }); render(); }
        catch (err) { toast(err.message); }
      });
      document.getElementById("deleteBtn").addEventListener("click", async () => {
        if (!confirm("Delete this project and its clips? This cannot be undone.")) return;
        try { await api(`/projects/${encodeURIComponent(id)}`, { method: "DELETE" }); location.hash = "#/projects"; }
        catch (err) { toast(err.message); }
      });
      app.querySelectorAll("[data-regen]").forEach((btn) => btn.addEventListener("click", async () => {
        const cid = btn.dataset.regen;
        const body = {
          captions_style: document.querySelector(`[data-r-caps="${cid}"]`).value,
          orientation: document.querySelector(`[data-r-orient="${cid}"]`).value,
          mirror: document.querySelector(`[data-r-mirror="${cid}"]`).checked,
        };
        try { await api(`/clips/${encodeURIComponent(cid)}/regenerate`, { method: "POST", json: body }); toast("Clip queued"); render(); }
        catch (err) { toast(err.message); }
      }));
      app.querySelectorAll("[data-save-metrics]").forEach((btn) => btn.addEventListener("click", async () => {
        const cid = btn.dataset.saveMetrics;
        const views = document.querySelector(`[data-views="${cid}"]`).value;
        const likes = document.querySelector(`[data-likes="${cid}"]`).value;
        if (views === "") { toast("Enter the number of views"); return; }
        try {
          await api(`/clips/${encodeURIComponent(cid)}/metrics`, { method: "POST", json: { views: Number(views), likes: Number(likes || 0) } });
          toast("Results saved");
        } catch (err) { toast(err.message); }
      }));

      if (active) { if (!pollTimer) pollTimer = setInterval(render, 2500); }
      else { stopPolling(); }
    };
    await render();
  }

  const CAPTION_OPTIONS = [
    ["classic", "Classic"], ["bold", "Bold, yellow"], ["boxed", "Boxed"], ["karaoke", "Karaoke highlight"], ["off", "No captions"],
  ];
  const ORIENTATION_OPTIONS = [
    ["landscape", "Landscape 16:9"], ["vertical", "Vertical 9:16"], ["square", "Square 1:1"],
  ];

  function optionList(options, selected) {
    return options.map(([val, label]) => `<option value="${esc(val)}" ${val === selected ? "selected" : ""}>${esc(label)}</option>`).join("");
  }

  // -- batches ---------------------------------------------------------------------------------
  async function viewBatches() {
    setNav("batches");
    stopPolling();
    const data = await api("/batches");
    setPage(`
      <div class="page-head"><div><span class="eyebrow">Many videos</span><h1>Batches</h1>
        <p>Apply one set of clip settings to many videos. Each video starts as soon as its transcript is ready.</p></div></div>
      <form id="batchForm" class="card row" style="margin-bottom:18px">
        <input id="batchName" placeholder="Batch name, for example: Season 2 episodes" maxlength="120" style="flex:1;min-width:240px">
        <button type="submit">${icon("plus")} Create batch</button>
      </form>
      ${data.batches.length ? `<div class="grid">${data.batches.map((b) => `
        <a class="card card-link batch-card" href="#/batch/${esc(b.id)}">
          <div class="card-head"><div class="row" style="gap:12px"><div class="bubble">${icon("stack")}</div>
            <div><h3>${esc(b.name)}</h3><div class="desc small muted">${b.projects} videos · ${b.settings ? "settings saved" : "no settings yet"}</div></div></div></div>
          <div class="row" style="gap:6px">${Object.entries(b.status_counts).map(([k, v]) => `<span class="badge ${(STATUS[k] || [])[1] || ""}">${esc((STATUS[k] || [k])[0])}: ${v}</span>`).join("") || `<span class="muted small">Empty</span>`}</div>
        </a>`).join("")}</div>` : emptyState("stack", "No batches yet", "Create a batch when you have several videos to process with the same settings.")}`);
    document.getElementById("batchForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const b = await api("/batches", { method: "POST", json: { name: document.getElementById("batchName").value } });
        location.hash = "#/batch/" + b.id;
      } catch (err) { toast(err.message); }
    });
  }

  async function viewBatch(id) {
    setNav("batches");
    stopPolling();
    const [batch, s, themes] = await Promise.all([
      api("/batches/" + encodeURIComponent(id)), api("/settings"), api("/themes"),
    ]);
    const cur = batch.settings || {};
    const v = s.values;
    const items = (batch.items || []).map((p) => `
      <tr><td><a href="#/project/${esc(p.id)}"><strong>${esc(p.title)}</strong></a></td><td>${statusBadge(p.status)}</td>
      <td class="small muted">${esc(p.status_message || "")}</td></tr>`).join("");

    setPage(`
      <div class="page-head"><div><span class="eyebrow">Batch</span><h1>${esc(batch.name)}</h1>
        <p>${batch.projects} videos in this batch.</p></div>
        <div class="page-actions"><button class="danger secondary" id="deleteBatchBtn">${icon("trash")} Delete batch</button></div></div>
      <div class="split">
        <section class="card stack">
          <div class="card-head"><div class="row"><div class="bubble">${icon("upload")}</div><div><h2>Add videos</h2><div class="desc">Upload files or paste links.</div></div></div></div>
          <div class="dropzone" id="dropBatch">${icon("upload", "i-lg")}<div class="title">Drop videos here</div><div class="field-hint">or click to choose several</div>
            <div class="file" data-file-name></div><input id="b_files" type="file" multiple accept=".mp4,.mov,.webm,.mkv,video/*"></div>
          <div><label for="b_links">Or paste links, one per line</label><textarea id="b_links" rows="3" placeholder="https://example.com/video1.mp4"></textarea></div>
          <label class="check"><input id="b_rights" type="checkbox"> I own these videos or have permission to process them.</label>
          <div class="row"><button id="addBtn">Add to batch</button><span id="addStatus" class="small muted"></span></div>
        </section>
        <form id="batchSettingsForm" class="card stack">
          <div class="card-head"><div class="row"><div class="bubble">${icon("sliders")}</div><div><h2>Settings for every video</h2><div class="desc">Saved for this batch and applied to each video.</div></div></div></div>
          <div class="field-grid">
            <div><label for="bs_theme">Theme</label><select id="bs_theme">${themes.themes.map((t) => `<option ${t === (cur.theme || v.default_theme) ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></div>
            <div><label for="bs_len">Clip length (seconds)</label><input id="bs_len" type="number" min="1" value="${esc(cur.clip_length || v.default_clip_length)}" required></div>
            <div><label for="bs_count">Clips per video</label><input id="bs_count" type="number" min="1" max="20" value="${esc(cur.clip_count || v.default_clip_count)}" required></div>
            <div><label for="bs_orient">Orientation</label><select id="bs_orient">${optionList(ORIENTATION_OPTIONS, cur.orientation || v.default_orientation)}</select></div>
            <div><label for="bs_res">Resolution</label><select id="bs_res">
              <option value="1080p" ${(cur.resolution || v.default_resolution) === "1080p" ? "selected" : ""}>1080p</option>
              <option value="720p" ${(cur.resolution || v.default_resolution) === "720p" ? "selected" : ""}>720p</option></select></div>
            <div><label for="bs_caps">Captions</label><select id="bs_caps">${optionList(CAPTION_OPTIONS, cur.captions_style || v.default_captions_style)}</select></div>
          </div>
          <div class="toggle-row">
            <label class="check"><input id="bs_mirror" type="checkbox" ${cur.mirror ? "checked" : ""}> Mirror the video</label>
            <label class="check"><input id="bs_effects" type="checkbox" ${cur.effects === false ? "" : "checked"}> Add sound effects and B-roll</label>
          </div>
          <div class="row"><button type="submit">${icon("sparkle")} Save and generate for ready videos</button></div>
          <p class="small muted">Videos still transcribing start as soon as they are ready.</p>
        </form>
      </div>
      <section class="card" style="margin-top:18px">
        <div class="card-head"><h2>Videos</h2></div>
        <div class="table-wrap"><table><thead><tr><th>Video</th><th>Status</th><th>Detail</th></tr></thead>
          <tbody>${items || "<tr><td colspan='3' class='muted'>No videos yet.</td></tr>"}</tbody></table></div>
      </section>`);

    wireDropzone(document.getElementById("dropBatch"));
    document.getElementById("addBtn").addEventListener("click", async () => {
      if (!document.getElementById("b_rights").checked) { toast("Please confirm you have the rights to these videos."); return; }
      const files = Array.from(document.getElementById("b_files").files);
      const links = document.getElementById("b_links").value.split("\n").map((l) => l.trim()).filter(Boolean);
      if (!files.length && !links.length) { toast("Choose files or paste links."); return; }
      const status = document.getElementById("addStatus");
      let added = 0;
      for (const file of files) {
        status.textContent = `Uploading ${added + 1} of ${files.length + links.length}...`;
        const form = new FormData();
        form.append("file", file); form.append("batch_id", id); form.append("title", file.name.replace(/\.[^.]+$/, ""));
        try { await api("/projects", { method: "POST", body: form }); added++; } catch (err) { toast(err.message); }
      }
      for (const link of links) {
        const form = new FormData();
        form.append("url", link); form.append("batch_id", id);
        try { await api("/projects", { method: "POST", body: form }); added++; } catch (err) { toast(err.message); }
      }
      status.textContent = `${added} added.`;
      viewBatch(id);
    });

    document.getElementById("batchSettingsForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const r = await api(`/batches/${encodeURIComponent(id)}/settings`, { method: "PUT", json: {
          theme: document.getElementById("bs_theme").value,
          clip_length: Number(document.getElementById("bs_len").value),
          clip_count: Number(document.getElementById("bs_count").value),
          orientation: document.getElementById("bs_orient").value,
          resolution: document.getElementById("bs_res").value,
          captions_style: document.getElementById("bs_caps").value,
          mirror: document.getElementById("bs_mirror").checked,
          effects: document.getElementById("bs_effects").checked,
        }});
        toast(`Started for ${r.applied} videos${r.waiting ? `, ${r.waiting} will start when ready` : ""}${r.errors.length ? `. ${r.errors.length} could not start: ${r.errors[0]}` : ""}`);
        viewBatch(id);
      } catch (err) { toast(err.message); }
    });

    document.getElementById("deleteBatchBtn").addEventListener("click", async () => {
      if (!confirm("Delete this batch? Its videos stay in your projects.")) return;
      try { await api(`/batches/${encodeURIComponent(id)}`, { method: "DELETE" }); location.hash = "#/batches"; }
      catch (err) { toast(err.message); }
    });

    if ((batch.items || []).some((p) => ACTIVE.includes(p.status))) {
      pollTimer = setInterval(() => viewBatch(id), 4000);
    }
  }

  // -- analytics -------------------------------------------------------------------------------
  async function viewAnalytics() {
    setNav("analytics");
    stopPolling();
    const a = await api("/analytics");
    const row = (label, stats) => `<tr><td>${esc(label)}</td><td class="mono">${stats.clips}</td>
      <td class="mono">${stats.avg_views == null ? "-" : esc(stats.avg_views)}</td><td class="mono">${stats.avg_likes == null ? "-" : esc(stats.avg_likes)}</td></tr>`;
    const block = (title, map) => `
      <section class="card"><div class="card-head"><h2>${esc(title)}</h2></div>
        <div class="table-wrap"><table><thead><tr><th>Group</th><th>Clips</th><th>Avg views</th><th>Avg likes</th></tr></thead>
        <tbody>${Object.entries(map).map(([k, v]) => row(k, v)).join("")}</tbody></table></div></section>`;

    setPage(`
      <div class="page-head"><div><span class="eyebrow">Performance</span><h1>Analytics</h1>
        <p>Enter views and likes on each clip, on its project page. The averages show which kinds of clips do best.</p></div></div>
      <div class="stats">
        <div class="card stat"><div class="label">Clips with results</div><div class="value">${a.clips_with_metrics} <span class="muted" style="font-size:1rem">of ${a.clips_total}</span></div></div>
        <div class="card stat"><div class="label">Average views</div><div class="value">${a.overall.avg_views == null ? "-" : esc(a.overall.avg_views)}</div></div>
      </div>
      ${a.clips_with_metrics ? `<div class="split-wide">
        <div class="stack">
          ${block("By score", a.by_score)}${block("By length", a.by_length)}
        </div>
        <div class="stack">
          ${block("By theme", a.by_theme)}
          ${block("With or without effects", { "With effects": a.with_effects, "Without effects": a.without_effects })}
          <section class="card"><div class="card-head"><h2>Top clips</h2></div>
            <ol style="margin:0;padding-left:20px">${a.top_clips.map((c) => `<li style="margin-bottom:12px"><strong>${esc(c.views)} views</strong> · ${esc(c.likes || 0)} likes · score ${c.score == null ? "-" : c.score.toFixed(2)}<div class="small muted">${esc(c.title)} — ${esc(c.reason || "")}</div></li>`).join("")}</ol>
          </section>
        </div></div>`
        : emptyState("chart", "No results yet", "Add views to a few clips on their project pages to see what works.")}`);
  }

  // -- effect library (admin) ---------------------------------------------------------------------
  async function viewLibrary() {
    setNav("library");
    stopPolling();
    const data = await api("/effects");
    const themes = data.themes;
    if (!libSelected || !themes.some((t) => t.folder === libSelected)) libSelected = themes[0] ? themes[0].folder : null;
    const sel = themes.find((t) => t.folder === libSelected);
    const swatches = ["", "s2", "s3", "s4"];

    setPage(`
      <div class="page-head"><div><span class="eyebrow">Admin</span><h1>Effect library</h1>
        <p>Sound effects and B-roll that clips can use. Add files here, and each one's licence and source is saved with it.</p></div>
        <div class="page-actions"><button class="secondary" id="rescanBtn">${icon("refresh")} Rescan library</button></div></div>
      <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(230px,1fr));margin-bottom:22px">
        ${themes.map((t, i) => `
          <div class="card theme-card ${t.folder === libSelected ? "selected" : ""}" role="button" tabindex="0" data-theme-pick="${esc(t.folder)}">
            <div class="swatch ${swatches[i % swatches.length]}"></div>
            <div class="row between"><h3>${esc(t.name)}</h3><span class="tag">${t.count} files</span></div>
            <div class="small muted" style="margin-top:4px">${t.effects.filter((e) => e.kind === "sound").length} sounds · ${t.effects.filter((e) => e.kind === "broll").length} B-roll</div>
          </div>`).join("")}
        <form id="newThemeForm" class="card stack" style="border-style:dashed">
          <div class="row" style="gap:10px"><div class="bubble">${icon("plus")}</div><h3>New theme</h3></div>
          <div><label for="th_name">Name</label><input id="th_name" maxlength="60" placeholder="For example: Laughter"></div>
          <div><label for="th_desc">Short description</label><input id="th_desc" maxlength="300" placeholder="What is this theme for?"></div>
          <div><button type="submit" class="secondary">Create theme</button></div>
        </form>
      </div>
      ${sel ? `
      <div class="split">
        <section class="card stack">
          <div class="card-head"><div><h2>${esc(sel.name)}</h2><div class="desc">${sel.count} files in this theme</div></div>
            <button class="danger secondary sm" id="delTheme">${icon("trash")} Delete theme</button></div>
          ${sel.effects.length ? `<div>${sel.effects.map((e) => `
            <div class="effect-row">
              <div class="kind-ico ${e.kind === "broll" ? "broll" : ""}">${icon(e.kind === "broll" ? "film" : "music")}</div>
              <div style="min-width:0">
                <div class="effect-name">${esc(e.file.split("/").slice(-1)[0])}</div>
                <div class="effect-tags">${(e.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}
                  ${e.license ? `<span class="tag accent">${esc(e.license)}</span>` : ""}</div>
                <div style="margin-top:8px">${e.kind === "broll"
                  ? `<video controls preload="none" src="${esc(e.url)}"></video>`
                  : `<audio controls preload="none" src="${esc(e.url)}"></audio>`}</div>
              </div>
              <button class="ghost icon-only" data-del-effect="${esc(e.file)}" aria-label="Remove ${esc(e.file)}">${icon("trash")}</button>
            </div>`).join("")}</div>`
            : `<p class="muted">No files yet. Add one with the form on the right.</p>`}
        </section>
        <form id="addEffectForm" class="card stack">
          <div class="row" style="gap:10px"><div class="bubble">${icon("upload")}</div><div><h2>Add to ${esc(sel.name)}</h2><div class="desc">Sound or short B-roll clip.</div></div></div>
          <div class="dropzone" id="dropEffect">${icon("upload", "i-lg")}<div class="title">Drop a file here</div>
            <div class="field-hint">WAV, MP3, OGG, M4A for sounds · MP4, WEBM, MOV for B-roll</div>
            <div class="file" data-file-name></div><input id="ef_file" type="file" accept="audio/*,video/*,.wav,.mp3,.ogg,.m4a,.flac,.mp4,.webm,.mov"></div>
          <div class="field-grid">
            <div><label for="ef_kind">Type</label><select id="ef_kind"><option value="sound">Sound effect</option><option value="broll">B-roll (video)</option></select></div>
            <div><label for="ef_name">Name</label><input id="ef_name" maxlength="60" placeholder="For example: big laugh"></div>
          </div>
          <div><label for="ef_tags">Tags</label><input id="ef_tags" placeholder="laugh, loud, reaction (comma separated)"><p class="field-hint">The AI model uses these to choose effects.</p></div>
          <div class="field-grid">
            <div><label for="ef_license">Licence</label><input id="ef_license" placeholder="CC0-1.0, CC BY 4.0, Owned..."></div>
            <div><label for="ef_source">Source or author</label><input id="ef_source" placeholder="Where it came from"></div>
          </div>
          <label class="check"><input id="ef_speech" type="checkbox"> The file contains speech (it will be transcribed once and used as context)</label>
          <div class="row"><button type="submit" id="efBtn">${icon("upload")} Add effect</button><span class="small muted" id="efStatus"></span></div>
        </form>
      </div>` : emptyState("layers", "No themes yet", "Create a theme to start adding sounds and B-roll.")}
      <div class="card" style="margin-top:18px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div class="row" style="gap:12px"><div class="bubble">${icon("shieldCheck")}</div><div><strong>Licences</strong><div class="small muted">Only add files you have the right to use commercially, and record the licence each time.</div></div></div>
        <span class="small muted" id="libResult"></span>
      </div>`);

    if (sel) wireDropzone(document.getElementById("dropEffect"));
    document.querySelectorAll("[data-theme-pick]").forEach((b) => b.addEventListener("click", () => {
      libSelected = b.dataset.themePick;
      viewLibrary();
    }));
    document.getElementById("newThemeForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const t = await api("/admin/themes", { method: "POST", json: {
          name: document.getElementById("th_name").value, description: document.getElementById("th_desc").value } });
        libSelected = t.folder;
        toast(`Theme "${t.name}" created`);
        viewLibrary();
      } catch (err) { toast(err.message); }
    });
    document.getElementById("rescanBtn").addEventListener("click", async () => {
      try {
        const r = await api("/effects/rescan", { method: "POST" });
        document.getElementById("libResult").textContent = `Scanned ${r.themes} themes and ${r.effects} effects.`;
        toast("Library rescanned");
        viewLibrary();
      } catch (err) { toast(err.message); }
    });
    if (sel) {
      document.getElementById("delTheme").addEventListener("click", async () => {
        if (!confirm(`Delete the theme "${sel.name}" and all its files? Projects that used it keep their clips.`)) return;
        try { await api(`/admin/themes/${encodeURIComponent(sel.folder)}`, { method: "DELETE" }); libSelected = null; viewLibrary(); }
        catch (err) { toast(err.message); }
      });
      app.querySelectorAll("[data-del-effect]").forEach((b) => b.addEventListener("click", async () => {
        if (!confirm("Remove this file from the library?")) return;
        try {
          await api(`/admin/themes/${encodeURIComponent(sel.folder)}/effects?file=${encodeURIComponent(b.dataset.delEffect)}`, { method: "DELETE" });
          viewLibrary();
        } catch (err) { toast(err.message); }
      }));
      document.getElementById("addEffectForm").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const file = document.getElementById("ef_file").files[0];
        if (!file) { toast("Choose a file first"); return; }
        const form = new FormData();
        form.append("file", file);
        form.append("kind", document.getElementById("ef_kind").value);
        form.append("name", document.getElementById("ef_name").value);
        form.append("tags", document.getElementById("ef_tags").value);
        form.append("license", document.getElementById("ef_license").value);
        form.append("source", document.getElementById("ef_source").value);
        form.append("speech", document.getElementById("ef_speech").checked ? "true" : "false");
        const status = document.getElementById("efStatus");
        document.getElementById("efBtn").disabled = true;
        status.textContent = "Uploading and checking the file...";
        try {
          await api(`/admin/themes/${encodeURIComponent(sel.folder)}/effects`, { method: "POST", body: form });
          toast("Effect added");
          viewLibrary();
        } catch (err) {
          status.textContent = "";
          toast(err.message);
          document.getElementById("efBtn").disabled = false;
        }
      });
    }
  }

  // -- setup ------------------------------------------------------------------------------------------
  async function viewSetup() {
    setNav("setup");
    stopPolling();
    const [settings, providers, models, preflight, presets, library] = await Promise.all([
      api("/settings"), api("/providers"), api("/models"), api("/preflight"), api("/providers/presets"), api("/effects"),
    ]);
    const v = settings.values;
    const llmPresets = presets.presets;
    const transPresets = presets.transcription_presets;

    const checks = preflight.checks.map((c) => `
      <li class="row between" style="padding:10px 0;border-bottom:1px solid var(--border)">
        <div><strong>${esc(c.name)}</strong><div class="small muted">${esc(c.message)}</div></div>
        <span class="badge ${c.ok ? "ok" : c.level === "warning" ? "warn" : "bad"}">${c.ok ? "OK" : c.level === "warning" ? "Warning" : "Missing"}</span>
      </li>`).join("");

    const rateText = (rate) => {
      if (!rate) return "";
      const keys = Object.keys(rate);
      return keys.length ? `<div class="tiny muted">${keys.map((k) => `${esc(k)}=${esc(rate[k])}`).join(", ")}</div>` : "";
    };

    const providerRows = providers.providers.map((p) => `
      <tr>
        <td class="mono">${esc(p.priority)}</td>
        <td><strong>${esc(p.name)}</strong><div class="sub">${p.kind === "transcription" ? "Transcription" : "Language model"} · ${esc(p.preset_label)} · ${esc(p.model)}</div>
          ${p.key_hint ? `<div class="sub">Key ${esc(p.key_hint)}</div>` : ""}</td>
        <td><span class="badge ${p.enabled ? "ok" : ""}">${p.enabled ? "On" : "Off"}</span></td>
        <td>${p.last_test_ok === null ? `<span class="badge">Not tested</span>` : p.last_test_ok ? `<span class="badge ok">Connected</span>` : `<span class="badge bad">${esc(p.last_test_error || "Failed")}</span>`}</td>
        <td class="small">Today: ${p.usage_today.requests} requests, ${p.usage_today.tokens} tokens${p.usage_today.failures ? `, <span style="color:var(--danger)">${p.usage_today.failures} failures</span>` : ""}
          ${rateText(p.last_rate_limit)}
          ${p.docs_url ? `<div><a href="${esc(p.docs_url)}" target="_blank" rel="noopener">Free-tier limits</a></div>` : ""}</td>
        <td><div class="row" style="gap:6px">
          <button class="secondary sm" data-test="${esc(p.id)}">Test</button>
          <button class="secondary sm" data-toggle="${esc(p.id)}" data-enabled="${p.enabled ? 1 : 0}">${p.enabled ? "Disable" : "Enable"}</button>
          <button class="danger secondary sm" data-del="${esc(p.id)}">${icon("trash")}</button></div></td>
      </tr>`).join("");

    const modelRows = models.models.map((m) => `
      <tr><td><strong>${esc(m.name)}</strong>${m.name === models.active ? ` <span class="tag accent">active</span>` : ""}</td>
        <td>${m.downloaded ? `<span class="badge ok">Downloaded</span>` : `<span class="badge">Not downloaded</span>`}</td>
        <td class="small muted">${m.slow_on_cpu ? "Slow on CPU" : ""}</td>
        <td>${m.downloaded ? "" : `<button class="secondary sm" data-model="${esc(m.name)}">${icon("download")} Download</button>`}</td></tr>`).join("");

    setPage(`
      <div class="page-head"><div><span class="eyebrow">Administration</span><h1>Setup</h1>
        <p>Health, defaults, AI providers, transcription, and YouTube access for this server.</p></div></div>

      <div class="split">
        <section class="card"><div class="card-head"><div class="row"><div class="bubble ok">${icon("shieldCheck")}</div><div><h2>Health check</h2><div class="desc">What this server can and cannot do right now.</div></div></div></div>
          <ul style="list-style:none;margin:0;padding:0">${checks}</ul></section>
        <div class="stack">
          <section class="card"><div class="card-head"><div class="row"><div class="bubble">${icon("layers")}</div><div><h2>Effect library</h2><div class="desc">${library.themes.reduce((n, t) => n + t.count, 0)} files in ${library.themes.length} themes.</div></div></div>
            <a class="btn secondary sm" href="#/library">Manage</a></div></section>
          <section class="card" id="ytCookies"><div class="card-head"><div class="row"><div class="bubble warn">${icon("key")}</div><div><h2>YouTube cookies</h2><div class="desc">Only needed when YouTube asks the server to prove it is not a bot. Export cookies.txt while signed in to YouTube.</div></div></div></div>
            <p id="ytStatus" class="small muted" style="margin-bottom:12px"></p>
            <div class="dropzone" id="dropYt" style="padding:20px">${icon("upload")}<div class="title" style="font-size:.92rem">Choose cookies.txt</div><div class="file" data-file-name></div><input id="ytFile" type="file" accept=".txt,text/plain"></div>
            <div class="row" style="margin-top:12px"><button id="ytUpload">Upload</button><button class="danger secondary" id="ytRemove">Remove</button></div>
          </section>
        </div>
      </div>

      <form id="defaultsForm" class="card stack" style="margin-top:18px">
        <div class="card-head"><div class="row"><div class="bubble">${icon("sliders")}</div><div><h2>Default clip settings</h2><div class="desc">Used for new projects. You can change them for each project.</div></div></div></div>
        <div class="field-grid">
          <div><label for="d_theme">Theme</label><select id="d_theme">${library.themes.map((t) => `<option ${t.name === v.default_theme ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select></div>
          <div><label for="d_len">Clip length (seconds)</label><input id="d_len" type="number" min="1" value="${esc(v.default_clip_length)}"></div>
          <div><label for="d_count">Clips per video</label><input id="d_count" type="number" min="1" max="20" value="${esc(v.default_clip_count)}"></div>
          <div><label for="d_orient">Orientation</label><select id="d_orient">${["landscape", "vertical", "square"].map((o) => `<option ${o === v.default_orientation ? "selected" : ""}>${o}</option>`).join("")}</select></div>
          <div><label for="d_res">Resolution</label><select id="d_res">${["1080p", "720p"].map((o) => `<option ${o === v.default_resolution ? "selected" : ""}>${o}</option>`).join("")}</select></div>
          <div><label for="d_caps">Captions</label><select id="d_caps">${["classic", "bold", "boxed", "karaoke", "off"].map((o) => `<option ${o === v.default_captions_style ? "selected" : ""}>${o}</option>`).join("")}</select></div>
          <div><label for="d_tmode">Transcription</label><select id="d_tmode">
            <option value="local" ${v.transcription_mode === "local" ? "selected" : ""}>Local Whisper (on this server)</option>
            <option value="cloud" ${v.transcription_mode === "cloud" ? "selected" : ""}>Cloud provider, then local</option></select></div>
        </div>
        <div class="toggle-row"><label class="check"><input id="d_mirror" type="checkbox" ${v.default_mirror ? "checked" : ""}> Mirror by default</label></div>
        <div><button type="submit">Save defaults</button></div>
      </form>

      <section class="card" style="margin-top:18px">
        <div class="card-head"><div class="row"><div class="bubble">${icon("stack")}</div><div><h2>Transcription models</h2><div class="desc">Whisper runs on this server. Model: <strong>${esc(settings.whisper_model)}</strong>.</div></div></div></div>
        <div class="table-wrap"><table><thead><tr><th>Model</th><th>Status</th><th></th><th></th></tr></thead><tbody>${modelRows}</tbody></table></div>
      </section>

      <section class="card stack" style="margin-top:18px">
        <div class="card-head"><div class="row"><div class="bubble">${icon("sparkle")}</div><div><h2>AI providers</h2><div class="desc">Tried in priority order (lowest first). If one fails or hits its limit, the next one is used. Each key is tested before it is saved.</div></div></div></div>
        <div class="notice">${icon("shieldCheck")}<span>Transcript text is sent to the language model and transcription providers you configure. Video and audio files are not sent to language models.</span></div>
        <div class="table-wrap"><table><thead><tr><th>Priority</th><th>Provider</th><th>State</th><th>Test</th><th>Usage</th><th></th></tr></thead>
          <tbody>${providerRows || "<tr><td colspan='6' class='muted'>No providers yet. Add one below.</td></tr>"}</tbody></table></div>
        <form id="providerForm" class="stack" style="border-top:1px solid var(--border);padding-top:18px">
          <h3>Add a provider</h3>
          <div class="field-grid">
            <div><label for="p_kind">Type</label>
              <select id="p_kind"><option value="llm">Language model (picks clips)</option><option value="transcription">Transcription (speech to text)</option></select></div>
            <div><label for="p_preset">Service</label><select id="p_preset"></select></div>
            <div><label for="p_name">Name</label><input id="p_name" placeholder="Shown in the list"></div>
            <div><label for="p_base">Base URL</label><input id="p_base" placeholder="Leave blank for the default"></div>
            <div><label for="p_model">Model</label><input id="p_model" placeholder="Leave blank for the default"></div>
            <div><label for="p_key">API key</label><input id="p_key" type="password" autocomplete="off" placeholder="Not needed for Ollama"></div>
            <div><label for="p_prio">Priority</label><input id="p_prio" type="number" value="100"></div>
          </div>
          <div><button type="submit">${icon("plus")} Save provider</button></div>
        </form>
      </section>`);

    const kindSelect = document.getElementById("p_kind");
    const presetSelect = document.getElementById("p_preset");
    const fillPresets = () => {
      const map = kindSelect.value === "transcription" ? transPresets : llmPresets;
      presetSelect.innerHTML = Object.entries(map).map(([k, p]) => `<option value="${esc(k)}">${esc(p.label)}</option>`).join("");
    };
    kindSelect.addEventListener("change", fillPresets);
    fillPresets();

    document.getElementById("defaultsForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        await api("/settings", { method: "PUT", json: { values: {
          default_theme: document.getElementById("d_theme").value,
          default_clip_length: Number(document.getElementById("d_len").value),
          default_clip_count: Number(document.getElementById("d_count").value),
          default_orientation: document.getElementById("d_orient").value,
          default_resolution: document.getElementById("d_res").value,
          default_captions_style: document.getElementById("d_caps").value,
          default_mirror: document.getElementById("d_mirror").checked,
          transcription_mode: document.getElementById("d_tmode").value,
        }}});
        toast("Defaults saved");
      } catch (err) { toast(err.message); }
    });

    document.getElementById("providerForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        await api("/providers", { method: "POST", json: {
          kind: kindSelect.value,
          preset: presetSelect.value,
          name: document.getElementById("p_name").value || null,
          base_url: document.getElementById("p_base").value || null,
          model: document.getElementById("p_model").value || null,
          api_key: document.getElementById("p_key").value || null,
          priority: Number(document.getElementById("p_prio").value || 100),
        }});
        toast("Provider saved");
        viewSetup();
      } catch (err) { toast(err.message); }
    });

    app.querySelectorAll("[data-test]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "Testing...";
      try {
        const r = await api(`/providers/${encodeURIComponent(b.dataset.test)}/test`, { method: "POST" });
        toast(r.ok ? "Connection OK" : "Failed: " + r.error);
      } catch (err) { toast(err.message); }
      viewSetup();
    }));
    app.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", async () => {
      await api(`/providers/${encodeURIComponent(b.dataset.toggle)}`, { method: "PATCH", json: { enabled: b.dataset.enabled !== "1" } });
      viewSetup();
    }));
    app.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm("Remove this provider?")) return;
      await api(`/providers/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
      viewSetup();
    }));
    app.querySelectorAll("[data-model]").forEach((b) => b.addEventListener("click", async () => {
      await api(`/models/${encodeURIComponent(b.dataset.model)}/download`, { method: "POST" });
      toast("Model download started. This can take several minutes.");
    }));

    // YouTube cookies
    wireDropzone(document.getElementById("dropYt"));
    const showYt = async () => {
      const st = await api("/admin/youtube-cookies");
      document.getElementById("ytStatus").textContent = st.saved
        ? `Saved ${fmtDate(st.saved_at)} · ${st.youtube_cookies} YouTube cookies`
        : "No cookies saved yet.";
    };
    showYt();
    document.getElementById("ytUpload").addEventListener("click", async () => {
      const f = document.getElementById("ytFile").files[0];
      if (!f) { toast("Choose the cookies.txt file first"); return; }
      const form = new FormData();
      form.append("file", f);
      try { await api("/admin/youtube-cookies", { method: "POST", body: form }); toast("Cookies saved"); showYt(); }
      catch (err) { toast(err.message); }
    });
    document.getElementById("ytRemove").addEventListener("click", async () => {
      if (!confirm("Remove the saved YouTube cookies?")) return;
      try { await api("/admin/youtube-cookies", { method: "DELETE" }); toast("Cookies removed"); showYt(); }
      catch (err) { toast(err.message); }
    });
  }

  // -- administration ----------------------------------------------------------------------------------
  async function viewAdmin() {
    setNav("admin");
    stopPolling();
    const [users, stats, backups] = await Promise.all([api("/admin/users"), api("/admin/stats"), api("/admin/backups")]);
    const jobs = Object.entries(stats.jobs_by_status).map(([k, v]) => `${esc(k)}: ${v}`).join(" · ") || "none";
    const cards = [
      ["Users", stats.users, "users"], ["Projects", stats.projects, "film"], ["Clips ready", stats.clips_done, "check"],
      ["Providers", stats.providers, "sparkle"], ["Stored uploads", `${stats.stored_upload_mb} MB`, "upload"],
    ].map(([label, value, ic]) => `<div class="card stat"><div class="row between"><span class="label">${label}</span>${icon(ic)}</div><div class="value mono">${esc(value)}</div></div>`).join("");

    setPage(`
      <div class="page-head"><div><span class="eyebrow">Administration</span><h1>Administration</h1>
        <p>Users, usage, and backups for this server.</p></div></div>
      <div class="stats">${cards}</div>
      <p class="small muted" style="margin:-8px 0 18px">Jobs: ${jobs}</p>
      <section class="card">
        <div class="card-head"><div class="row"><div class="bubble">${icon("users")}</div><div><h2>Users</h2></div></div></div>
        <div class="table-wrap"><table><thead><tr><th>Email</th><th>Role</th><th>Projects</th><th>Status</th><th></th></tr></thead><tbody>
          ${users.users.map((u) => `<tr>
            <td>${esc(u.email)}</td>
            <td><select data-role="${esc(u.id)}" style="width:auto;padding:7px 10px">
              <option value="user" ${u.role === "user" ? "selected" : ""}>User</option>
              <option value="admin" ${u.role === "admin" ? "selected" : ""}>Admin</option></select></td>
            <td class="mono">${esc(u.projects)}</td>
            <td><span class="badge ${u.disabled ? "bad" : "ok"}">${u.disabled ? "Disabled" : "Active"}</span></td>
            <td><button class="secondary sm" data-disable="${esc(u.id)}" data-state="${u.disabled ? 0 : 1}">${u.disabled ? "Enable" : "Disable"}</button></td>
          </tr>`).join("")}
        </tbody></table></div>
      </section>
      <section class="card stack" style="margin-top:18px">
        <div class="card-head"><div class="row"><div class="bubble">${icon("download")}</div><div><h2>Backups</h2><div class="desc">Database, settings, and effect manifests. The master key is never included.</div></div></div>
          <button id="backupBtn">${icon("plus")} Back up now</button></div>
        <ul style="list-style:none;margin:0;padding:0">${backups.backups.map((b) => `<li class="row between" style="padding:10px 0;border-bottom:1px solid var(--border)"><span>${esc(b.name)}</span><span class="small muted">${(b.size / 1024).toFixed(0)} KB · ${esc(b.created)}</span></li>`).join("") || "<li class='muted'>No backups yet.</li>"}</ul>
      </section>`);

    app.querySelectorAll("[data-role]").forEach((sel) => sel.addEventListener("change", async () => {
      try { await api(`/admin/users/${encodeURIComponent(sel.dataset.role)}`, { method: "PATCH", json: { role: sel.value } }); toast("Role updated"); }
      catch (err) { toast(err.message); viewAdmin(); }
    }));
    app.querySelectorAll("[data-disable]").forEach((btn) => btn.addEventListener("click", async () => {
      try { await api(`/admin/users/${encodeURIComponent(btn.dataset.disable)}`, { method: "PATCH", json: { disabled: btn.dataset.state === "1" } }); viewAdmin(); }
      catch (err) { toast(err.message); }
    }));
    document.getElementById("backupBtn").addEventListener("click", async () => {
      try { const r = await api("/admin/backups", { method: "POST" }); toast("Backup written: " + r.name); viewAdmin(); }
      catch (err) { toast(err.message); }
    });
  }

  // -- router --------------------------------------------------------------------------------------------
  async function route() {
    renderNav();
    const hash = location.hash || "#/projects";
    if (session.authRequired && !session.user && hash !== "#/login") {
      location.hash = "#/login";
      return;
    }
    try {
      if (hash === "#/login") return viewLogin();
      if (hash.startsWith("#/project/")) return await viewProject(decodeURIComponent(hash.slice("#/project/".length)));
      if (hash === "#/new") return await viewNew();
      if (hash === "#/setup") return await viewSetup();
      if (hash === "#/library") return await viewLibrary();
      if (hash === "#/batches") return await viewBatches();
      if (hash.startsWith("#/batch/")) return await viewBatch(decodeURIComponent(hash.slice("#/batch/".length)));
      if (hash === "#/analytics") return await viewAnalytics();
      if (hash === "#/account") return viewAccount();
      if (hash === "#/admin") return await viewAdmin();
      stopPolling();
      return await viewProjects();
    } catch (err) {
      setPage(`<div class="notice error">${icon("alert")}<span>${esc(err.message)}</span></div>`);
    }
  }

  async function boot() {
    initTheme();
    try {
      const me = await fetch("/api/auth/me", { credentials: "same-origin" });
      if (me.status === 401) {
        session.authRequired = true;
        if (!(await refreshSession())) { session.user = null; }
      } else {
        const data = await me.json();
        session.authRequired = !!data.auth_required;
        session.user = data.user;
      }
    } catch (_) {
      session.user = null;
    }
    if (!session.authRequired) renderNav();
    window.addEventListener("hashchange", () => { closeMenu(); route(); });
    document.getElementById("signOut").addEventListener("click", signOut);
    initMenu();
    route();
  }

  boot();
})();
