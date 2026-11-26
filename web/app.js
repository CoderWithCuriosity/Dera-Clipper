/* AI Clipper interface. Plain JavaScript, no build step.
   All text that comes from the server or from the model is escaped before it is shown. */
(function () {
  "use strict";

  const app = document.getElementById("app");
  let pollTimer = null;
  let toastTimer = null;

  // -- helpers -----------------------------------------------------------------
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function toast(message) {
    const el = document.getElementById("toast");
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
  }

  async function api(path, options) {
    const opts = Object.assign({ method: "GET", headers: {} }, options || {});
    if (opts.json !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
    }
    const res = await fetch("/api" + path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText;
      throw new Error(detail || "Request failed");
    }
    return data;
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

  const STATUS_LABEL = {
    queued: "Queued", acquiring: "Downloading", preparing_audio: "Preparing audio",
    transcribing: "Transcribing", awaiting_settings: "Ready for settings", planning: "Choosing moments",
    rendering: "Rendering", done: "Done", failed: "Failed",
  };

  function statusBadge(status) {
    const cls = status === "done" ? "done" : status === "failed" ? "failed"
      : status === "awaiting_settings" ? "warn" : "working";
    return `<span class="badge ${cls}">${esc(STATUS_LABEL[status] || status)}</span>`;
  }

  function progressBar(value) {
    const pct = Math.max(0, Math.min(100, Math.round((value || 0) * 100)));
    return `<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${pct}%"></span></div>`;
  }

  function setNav(name) {
    document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // -- views -------------------------------------------------------------------
  async function viewProjects() {
    setNav("projects");
    app.innerHTML = `<h1>Projects</h1><p class="muted">Each project is one long video. Open a project to pick a theme and get clips.</p><div id="list" class="stack"></div>`;
    const data = await api("/projects");
    const list = document.getElementById("list");
    if (!data.projects.length) {
      list.innerHTML = `<div class="panel"><p>No projects yet.</p><a class="button" href="#/new">Create your first project</a></div>`;
      return;
    }
    list.innerHTML = data.projects.map((p) => `
      <a class="panel" style="display:block;text-decoration:none;color:inherit" href="#/project/${esc(p.id)}">
        <div class="row" style="justify-content:space-between">
          <strong>${esc(p.title)}</strong>${statusBadge(p.status)}
        </div>
        <div class="small muted">${fmtDuration(p.duration)} · ${esc(p.source_type === "link" ? "Link" : "Upload")} · ${esc(p.mode)} mode</div>
        <div style="margin-top:8px">${progressBar(p.progress)}</div>
        <div class="small muted" style="margin-top:6px">${esc(p.status_message || "")}</div>
      </a>`).join("");
  }

  async function viewNew() {
    setNav("new");
    const themes = (await api("/themes")).themes;
    app.innerHTML = `
      <h1>New project</h1>
      <form id="newForm" class="panel stack">
        <div>
          <label for="title">Title (optional)</label>
          <input id="title" name="title" maxlength="120" placeholder="For example: Podcast episode 12">
        </div>
        <div>
          <label for="file">Upload a video (MP4, MOV, WEBM or MKV)</label>
          <input id="file" name="file" type="file" accept=".mp4,.mov,.webm,.mkv,video/*">
        </div>
        <p class="muted small" style="margin:0">or</p>
        <div>
          <label for="url">Link to a video file</label>
          <input id="url" name="url" type="url" placeholder="https://example.com/video.mp4">
          <p class="small muted">Only use links to content you own or have permission to process. Links to web pages that are not direct video files will be rejected.</p>
        </div>
        <label class="check"><input id="rights" type="checkbox"> I own this content or have permission to process it.</label>
        <div class="row">
          <button type="submit" id="createBtn">Create project</button>
          <span class="muted small" id="newStatus"></span>
        </div>
      </form>
      <p class="small muted">Themes available: ${themes.map(esc).join(", ")}</p>`;

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
        app.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
        return;
      }
      const active = ["queued", "acquiring", "preparing_audio", "transcribing", "planning", "rendering"].includes(project.status);
      const themes = (await api("/themes")).themes;
      const s = await api("/settings");
      const v = s.values;
      const current = {
        theme: project.theme || v.default_theme,
        clip_length: project.clip_length || v.default_clip_length,
        clip_count: project.clip_count || v.default_clip_count,
        captions_style: project.captions_style || v.default_captions_style,
        orientation: project.orientation || v.default_orientation,
        resolution: project.resolution || v.default_resolution,
        mirror: project.theme ? project.mirror : v.default_mirror,
      };

      const settingsForm = project.has_transcript ? `
        <form id="genForm" class="panel stack">
          <h2>Clip settings</h2>
          <div class="grid">
            <div><label for="theme">Theme</label>
              <select id="theme">${themes.map((t) => `<option ${t === current.theme ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></div>
            <div><label for="clip_length">Clip length (seconds)</label>
              <input id="clip_length" type="number" min="1" step="1" max="${Math.floor(project.duration)}" value="${esc(Math.round(current.clip_length))}" required>
              <p class="small muted">Maximum ${fmtDuration(project.duration)} (the source length).</p></div>
            <div><label for="clip_count">Number of clips</label>
              <input id="clip_count" type="number" min="1" max="20" value="${esc(current.clip_count)}" required></div>
            <div><label for="orientation">Orientation</label>
              <select id="orientation">
                ${["landscape|Landscape 16:9 (YouTube)", "vertical|Vertical 9:16 (Shorts, Reels, TikTok)", "square|Square 1:1"]
                  .map((o) => { const [val, lab] = o.split("|"); return `<option value="${val}" ${val === current.orientation ? "selected" : ""}>${esc(lab)}</option>`; }).join("")}
              </select></div>
            <div><label for="resolution">Resolution</label>
              <select id="resolution">
                <option value="1080p" ${current.resolution === "1080p" ? "selected" : ""}>1080p</option>
                <option value="720p" ${current.resolution === "720p" ? "selected" : ""}>720p (faster)</option>
              </select></div>
            <div><label for="captions_style">Captions</label>
              <select id="captions_style">
                ${["classic|Classic (white, outlined)", "bold|Bold (yellow, thick outline)", "boxed|Boxed (on a dark box)", "off|No captions"]
                  .map((o) => { const [val, lab] = o.split("|"); return `<option value="${val}" ${val === current.captions_style ? "selected" : ""}>${esc(lab)}</option>`; }).join("")}
              </select></div>
          </div>
          <label class="check"><input id="mirror" type="checkbox" ${current.mirror ? "checked" : ""}> Mirror the video</label>
          <div class="row">
            <button type="submit">${project.clips && project.clips.length ? "Regenerate clips" : "Generate clips"}</button>
            <span class="small muted">Generation uses the first available language model, then falls back to the next one.</span>
          </div>
        </form>` : "";

      const clips = (project.clips || []).map((c) => `
        <div class="panel clip">
          ${c.video_url ? `<video controls preload="metadata" src="${esc(c.video_url)}"></video>`
            : `<div class="notice">${c.status === "failed" ? "<span class='error'>Failed</span>" : "Rendering..."}</div>`}
          <div class="meta stack">
            <div class="row" style="justify-content:space-between">
              <strong>Clip ${c.idx}</strong>
              <span class="score">Score ${c.score != null ? c.score.toFixed(2) : "-"}</span>
            </div>
            <div class="small muted">${fmtDuration(c.start)} to ${fmtDuration(c.end)} · ${Math.round(c.length)} s</div>
            <div>${esc(c.reason || "")}</div>
            ${(c.effects || []).length ? `<div class="small muted">Effects: ${c.effects.map((e) => esc(e.effect_id) + " at " + Math.round(e.at) + " s").join(", ")}</div>` : ""}
            ${c.error ? `<div class="error small">${esc(c.error)}</div>` : ""}
            <div class="row">
              ${c.download_url ? `<a class="button" href="${esc(c.download_url)}">Download</a>` : ""}
              ${c.status !== "pending" && c.status !== "rendering" ? `<button class="secondary" data-regen="${esc(c.id)}">Regenerate this clip</button>` : ""}
            </div>
          </div>
        </div>`).join("");

      const done = (project.clips || []).filter((c) => c.status === "done").length;
      const header = `
        <div class="row" style="justify-content:space-between">
          <h1 style="margin-bottom:0">${esc(project.title)}</h1>${statusBadge(project.status)}
        </div>
        <p class="muted small">${fmtDuration(project.duration)} · ${fmtSize(project.file_size)} · created in ${esc(project.mode)} mode${project.provider_used ? " · planned by " + esc(project.provider_used) : ""}</p>
        <div class="panel stack">
          <div>${esc(project.status_message || "")}</div>
          ${progressBar(project.progress)}
          ${project.error ? `<div class="notice error">${esc(project.error)}</div>` : ""}
          <div class="row">
            ${project.status === "failed" ? `<button id="retryBtn">Retry</button>` : ""}
            ${done ? `<a class="button secondary" href="/api/projects/${esc(project.id)}/zip">Download all as zip</a>` : ""}
            <button class="danger secondary" id="deleteBtn" ${active ? "disabled" : ""}>Delete project</button>
          </div>
        </div>`;

      app.innerHTML = header + settingsForm + (clips ? `<h2>Clips</h2><div class="stack">${clips}</div>` : "");

      if (project.has_transcript) {
        document.getElementById("genForm").addEventListener("submit", async (ev) => {
          ev.preventDefault();
          try {
            await api(`/projects/${encodeURIComponent(id)}/generate`, { method: "POST", json: {
              theme: document.getElementById("theme").value,
              clip_length: Number(document.getElementById("clip_length").value),
              clip_count: Number(document.getElementById("clip_count").value),
              mirror: document.getElementById("mirror").checked,
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
        try { await api(`/clips/${encodeURIComponent(btn.dataset.regen)}/regenerate`, { method: "POST", json: {} }); toast("Clip queued"); render(); }
        catch (err) { toast(err.message); }
      }));

      if (active) { if (!pollTimer) pollTimer = setInterval(render, 2500); }
      else { stopPolling(); }
    };
    await render();
  }

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
      <li><span class="badge ${c.ok ? "done" : c.level === "warning" ? "warn" : "failed"}">${c.ok ? "OK" : c.level === "warning" ? "Warning" : "Missing"}</span>
        <strong>${esc(c.name)}</strong>: ${esc(c.message)}</li>`).join("");

    const rateText = (rate) => {
      if (!rate) return "";
      const keys = Object.keys(rate);
      return keys.length ? `<div class="small muted">Last reported: ${keys.map((k) => `${esc(k)}=${esc(rate[k])}`).join(", ")}</div>` : "";
    };

    const providerRows = providers.providers.map((p) => `
      <tr>
        <td>${esc(p.priority)}</td>
        <td><strong>${esc(p.name)}</strong><div class="small muted">${p.kind === "transcription" ? "Transcription" : "Language model"} · ${esc(p.preset_label)} · ${esc(p.model)}</div>
          ${p.key_hint ? `<div class="small muted">Key ${esc(p.key_hint)}</div>` : ""}</td>
        <td>${p.enabled ? "On" : "Off"}</td>
        <td>${p.last_test_ok === null ? "Not tested" : p.last_test_ok ? "<span class='badge done'>Connected</span>" : `<span class='badge failed'>${esc(p.last_test_error || "Failed")}</span>`}</td>
        <td class="small">Today: ${p.usage_today.requests} requests, ${p.usage_today.tokens} tokens${p.usage_today.failures ? `, <span class="error">${p.usage_today.failures} failures</span>` : ""}
          ${rateText(p.last_rate_limit)}
          ${p.docs_url ? `<div><a href="${esc(p.docs_url)}" target="_blank" rel="noopener">Free-tier limits (provider docs)</a></div>` : ""}</td>
        <td><div class="row">
          <button class="secondary" data-test="${esc(p.id)}">Test</button>
          <button class="secondary" data-toggle="${esc(p.id)}" data-enabled="${p.enabled ? 1 : 0}">${p.enabled ? "Disable" : "Enable"}</button>
          <button class="danger secondary" data-del="${esc(p.id)}">Remove</button></div></td>
      </tr>`).join("");

    const modelRows = models.models.map((m) => `
      <tr><td>${esc(m.name)}${m.name === models.active ? " <span class='badge'>active</span>" : ""}</td>
        <td>${m.downloaded ? "<span class='badge done'>Downloaded</span>" : "<span class='badge'>Not downloaded</span>"}</td>
        <td>${m.slow_on_cpu ? "<span class='small muted'>Slow on CPU</span>" : ""}</td>
        <td>${m.downloaded ? "" : `<button class="secondary" data-model="${esc(m.name)}">Download</button>`}</td></tr>`).join("");

    const themeRows = library.themes.map((t) => `
      <li><strong>${esc(t.name)}</strong> · ${t.count} effect${t.count === 1 ? "" : "s"}
        ${t.effects.map((e) => `<div class="small muted">${esc(e.kind === "sound" ? "Sound" : "B-roll")}: ${esc(e.file)}${e.duration ? ` (${e.duration.toFixed(1)} s)` : ""}${e.license ? ` · ${esc(e.license)}` : ""}</div>`).join("")}
      </li>`).join("");

    const presetOptions = (map) => Object.entries(map).map(([k, p]) => `<option value="${esc(k)}">${esc(p.label)}</option>`).join("");

    app.innerHTML = `
      <h1>Setup</h1>
      <section class="panel"><h2>Health check</h2><ul class="check-list">${checks}</ul></section>

      <form id="defaultsForm" class="panel stack">
        <h2>Default clip settings</h2>
        <div class="grid">
          <div><label for="d_theme">Theme</label>
            <select id="d_theme">${library.themes.map((t) => `<option ${t.name === v.default_theme ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select></div>
          <div><label for="d_len">Clip length (s)</label><input id="d_len" type="number" min="1" value="${esc(v.default_clip_length)}"></div>
          <div><label for="d_count">Clip count</label><input id="d_count" type="number" min="1" max="20" value="${esc(v.default_clip_count)}"></div>
          <div><label for="d_orient">Orientation</label>
            <select id="d_orient">${["landscape", "vertical", "square"].map((o) => `<option ${o === v.default_orientation ? "selected" : ""}>${o}</option>`).join("")}</select></div>
          <div><label for="d_res">Resolution</label>
            <select id="d_res">${["1080p", "720p"].map((o) => `<option ${o === v.default_resolution ? "selected" : ""}>${o}</option>`).join("")}</select></div>
          <div><label for="d_caps">Captions</label>
            <select id="d_caps">${["classic", "bold", "boxed", "off"].map((o) => `<option ${o === v.default_captions_style ? "selected" : ""}>${o}</option>`).join("")}</select></div>
          <div><label for="d_tmode">Transcription</label>
            <select id="d_tmode">
              <option value="local" ${v.transcription_mode === "local" ? "selected" : ""}>Local Whisper (on this machine)</option>
              <option value="cloud" ${v.transcription_mode === "cloud" ? "selected" : ""}>Cloud provider (falls back to local)</option>
            </select></div>
        </div>
        <label class="check"><input id="d_mirror" type="checkbox" ${v.default_mirror ? "checked" : ""}> Mirror by default</label>
        <div><button type="submit">Save defaults</button></div>
      </form>

      <section class="panel">
        <h2>Transcription</h2>
        <p class="muted small">Whisper model: <strong>${esc(settings.whisper_model)}</strong> · Mode: ${esc(settings.mode)}</p>
        <div style="overflow-x:auto"><table><thead><tr><th>Model</th><th>Status</th><th></th><th></th></tr></thead><tbody>${modelRows}</tbody></table></div>
      </section>

      <section class="panel stack">
        <h2>Providers</h2>
        <p class="muted small">Language models and transcription services are each tried in priority order (lowest number first). If one fails, hits its limit, or returns a bad answer, the next one is used. Keys are encrypted and only the last four characters are shown.</p>
        <div style="overflow-x:auto"><table><thead><tr><th>Priority</th><th>Provider</th><th>State</th><th>Test</th><th>Usage</th><th></th></tr></thead>
          <tbody>${providerRows || "<tr><td colspan='6' class='muted'>No providers yet. Add one below.</td></tr>"}</tbody></table></div>
        <form id="providerForm" class="stack" style="border-top:1px solid var(--line);padding-top:12px">
          <h2>Add a provider</h2>
          <div class="grid">
            <div><label for="p_kind">Type</label>
              <select id="p_kind"><option value="llm">Language model (picks clips)</option><option value="transcription">Transcription (speech to text)</option></select></div>
            <div><label for="p_preset">Service</label><select id="p_preset"></select></div>
            <div><label for="p_name">Name</label><input id="p_name" placeholder="Shown in the list"></div>
            <div><label for="p_base">Base URL</label><input id="p_base" placeholder="Leave blank to use the default"></div>
            <div><label for="p_model">Model</label><input id="p_model" placeholder="Leave blank to use the default"></div>
            <div><label for="p_key">API key</label><input id="p_key" type="password" autocomplete="off" placeholder="Not needed for Ollama"></div>
            <div><label for="p_prio">Priority</label><input id="p_prio" type="number" value="100"></div>
          </div>
          <div><button type="submit">Save provider</button></div>
        </form>
      </section>

      <section class="panel stack">
        <div class="row" style="justify-content:space-between">
          <h2 style="margin:0">Effect library</h2>
          <button class="secondary" id="rescanBtn">Rescan library</button>
        </div>
        <p class="muted small">Add files to a theme folder, list them in its manifest.json, then rescan. Each file's license is read from the manifest.</p>
        <ul class="check-list">${themeRows || "<li class='muted'>No themes found. Check the effects folder.</li>"}</ul>
        <div id="scanResult" class="small"></div>
      </section>`;

    const kindSelect = document.getElementById("p_kind");
    const presetSelect = document.getElementById("p_preset");
    const fillPresets = () => {
      const map = kindSelect.value === "transcription" ? transPresets : llmPresets;
      presetSelect.innerHTML = presetOptions(map);
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

    document.getElementById("rescanBtn").addEventListener("click", async () => {
      try {
        const r = await api("/effects/rescan", { method: "POST" });
        document.getElementById("scanResult").innerHTML =
          `Scanned ${r.themes} themes, ${r.effects} effects, ${r.transcribed} transcribed.` +
          (r.warnings.length ? `<ul>${r.warnings.map((w) => `<li class="error">${esc(w)}</li>`).join("")}</ul>` : " No problems found.");
        toast("Library rescanned");
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
      toast("Model download started. This can take several minutes. Refresh this page to check.");
    }));
  }

  // -- router --------------------------------------------------------------------
  async function route() {
    const hash = location.hash || "#/projects";
    try {
      if (hash.startsWith("#/project/")) return await viewProject(decodeURIComponent(hash.slice("#/project/".length)));
      if (hash === "#/new") return await viewNew();
      if (hash === "#/setup") return await viewSetup();
      stopPolling();
      return await viewProjects();
    } catch (err) {
      app.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
    }
  }

  window.addEventListener("hashchange", route);
  route();
})();
