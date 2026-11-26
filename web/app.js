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
                ${["classic|Classic (white, outlined)", "bold|Bold (yellow, thick outline)", "boxed|Boxed (on a dark box)", "karaoke|Karaoke (word highlight)", "off|No captions"]
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
            ${c.status === "done" ? `
            <div class="row small">
              <label style="margin:0">Views <input data-views="${esc(c.id)}" type="number" min="0" value="${esc(c.views == null ? "" : c.views)}" style="width:120px"></label>
              <label style="margin:0">Likes <input data-likes="${esc(c.id)}" type="number" min="0" value="${esc(c.likes == null ? "" : c.likes)}" style="width:100px"></label>
              <button class="secondary" data-save-metrics="${esc(c.id)}">Save results</button>
            </div>` : ""}
            <div class="row">
              ${c.download_url ? `<a class="button" href="${esc(c.download_url)}">Download</a>` : ""}
              ${c.status !== "pending" && c.status !== "rendering" ? `
                <select data-r-caps="${esc(c.id)}" aria-label="Captions">${optionList(CAPTION_OPTIONS, (c.settings || {}).captions_style)}</select>
                <select data-r-orient="${esc(c.id)}" aria-label="Orientation">${optionList(ORIENTATION_OPTIONS, (c.settings || {}).orientation)}</select>
                <label class="check"><input type="checkbox" data-r-mirror="${esc(c.id)}" ${(c.settings || {}).mirror ? "checked" : ""}> Mirror</label>
                <button class="secondary" data-regen="${esc(c.id)}">Regenerate this clip</button>` : ""}
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
            <select id="d_caps">${["classic", "bold", "boxed", "karaoke", "off"].map((o) => `<option ${o === v.default_captions_style ? "selected" : ""}>${o}</option>`).join("")}</select></div>
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
        <p class="muted small">Language models and transcription services are each tried in priority order (lowest number first). If one fails, hits its limit, or returns a bad answer, the next one is used. Keys are encrypted and only the last four characters are shown. Each new key is tested before it is saved.</p>
        <p class="notice small" style="margin:0">Privacy: the transcript text of your videos is sent to the language model and transcription providers you configure. Video and audio files are not sent to language models. Use Ollama or local Whisper to keep everything on your machine.</p>
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

  // -- session -------------------------------------------------------------------
  const session = { token: null, user: null, authRequired: false };

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
    const who = document.getElementById("who");
    if (who) who.textContent = signedIn && session.authRequired ? session.user.email : "";
  }

  // -- sign in -------------------------------------------------------------------
  function viewLogin() {
    setNav("");
    stopPolling();
    app.innerHTML = `
      <div class="panel" style="max-width:420px;margin:24px auto">
        <h1>Sign in</h1>
        <div class="row" style="margin-bottom:12px">
          <button class="secondary" id="tabLogin" type="button">Sign in</button>
          <button class="secondary" id="tabRegister" type="button">Create account</button>
        </div>
        <form id="authForm" class="stack">
          <div><label for="a_email">Email</label><input id="a_email" type="email" autocomplete="email" required></div>
          <div><label for="a_pass">Password</label><input id="a_pass" type="password" autocomplete="current-password" minlength="10" required></div>
          <p class="small muted" id="authHint" style="margin:0"></p>
          <div><button type="submit" id="authBtn">Sign in</button></div>
        </form>
      </div>`;
    let mode = "login";
    const setMode = (m) => {
      mode = m;
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

  // -- account -------------------------------------------------------------------
  function viewAccount() {
    setNav("account");
    stopPolling();
    app.innerHTML = `
      <h1>Account</h1>
      <div class="panel stack">
        <p>Signed in as <strong>${esc(session.user ? session.user.email : "local profile")}</strong>
          ${session.user ? `(${esc(session.user.role)})` : ""}</p>
        ${session.authRequired ? `
        <form id="pwForm" class="stack">
          <h2>Change password</h2>
          <div><label for="pw_old">Current password</label><input id="pw_old" type="password" autocomplete="current-password" required></div>
          <div><label for="pw_new">New password</label><input id="pw_new" type="password" autocomplete="new-password" minlength="10" required></div>
          <div><button type="submit">Change password</button></div>
        </form>
        <div><button class="secondary" id="logoutBtn" type="button">Sign out</button></div>` : `<p class="muted">Sign-in is turned off in this mode. The app runs as a single local profile.</p>`}
      </div>`;
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

  // -- administration ------------------------------------------------------------
  async function viewAdmin() {
    setNav("admin");
    stopPolling();
    const [users, stats, backups] = await Promise.all([
      api("/admin/users"), api("/admin/stats"), api("/admin/backups"),
    ]);
    const statLines = [
      ["Users", stats.users], ["Projects", stats.projects], ["Clips ready", stats.clips_done],
      ["Providers", stats.providers], ["Stored uploads (MB)", stats.stored_upload_mb],
    ].map(([k, v]) => `<div class="panel"><div class="small muted">${esc(k)}</div><div style="font-size:1.4rem;font-weight:700">${esc(v)}</div></div>`).join("");
    const jobs = Object.entries(stats.jobs_by_status).map(([k, v]) => `${esc(k)}: ${v}`).join(" · ") || "none";

    app.innerHTML = `
      <h1>Administration</h1>
      <div class="grid">${statLines}</div>
      <p class="small muted">Jobs: ${jobs}</p>

      <section class="panel">
        <h2>Users</h2>
        <div style="overflow-x:auto"><table><thead><tr><th>Email</th><th>Role</th><th>Projects</th><th>Status</th><th></th></tr></thead><tbody>
          ${users.users.map((u) => `<tr>
            <td>${esc(u.email)}</td>
            <td><select data-role="${esc(u.id)}">
              <option value="user" ${u.role === "user" ? "selected" : ""}>User</option>
              <option value="admin" ${u.role === "admin" ? "selected" : ""}>Admin</option></select></td>
            <td>${esc(u.projects)}</td>
            <td>${u.disabled ? "<span class='badge failed'>Disabled</span>" : "<span class='badge done'>Active</span>"}</td>
            <td><button class="secondary" data-disable="${esc(u.id)}" data-state="${u.disabled ? 0 : 1}">${u.disabled ? "Enable" : "Disable"}</button></td>
          </tr>`).join("")}
        </tbody></table></div>
      </section>

      <section class="panel stack">
        <div class="row" style="justify-content:space-between">
          <h2 style="margin:0">Backups</h2>
          <button id="backupBtn">Back up now</button>
        </div>
        <p class="small muted">Backups include the database, settings, and effect manifests. They never include the master key. Keep a copy of the master key somewhere safe, separately.</p>
        <ul class="check-list">${backups.backups.map((b) => `<li>${esc(b.name)} <span class="small muted">${(b.size / 1024).toFixed(0)} KB · ${esc(b.created)}</span></li>`).join("") || "<li class='muted'>No backups yet.</li>"}</ul>
      </section>`;

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

  // -- batches -------------------------------------------------------------------
  const CAPTION_OPTIONS = [
    ["classic", "Classic (white, outlined)"], ["bold", "Bold (yellow, thick outline)"],
    ["boxed", "Boxed (on a dark box)"], ["karaoke", "Karaoke (word highlight)"], ["off", "No captions"],
  ];
  const ORIENTATION_OPTIONS = [
    ["landscape", "Landscape 16:9 (YouTube)"], ["vertical", "Vertical 9:16 (Shorts, Reels, TikTok)"], ["square", "Square 1:1"],
  ];

  function optionList(options, selected) {
    return options.map(([val, label]) => `<option value="${esc(val)}" ${val === selected ? "selected" : ""}>${esc(label)}</option>`).join("");
  }

  async function viewBatches() {
    setNav("batches");
    stopPolling();
    const data = await api("/batches");
    app.innerHTML = `
      <h1>Batches</h1>
      <p class="muted">A batch applies one set of clip settings to many videos. Upload or link them, set the settings once, and each video is processed as soon as its transcript is ready.</p>
      <form id="batchForm" class="panel row">
        <input id="batchName" placeholder="Batch name, for example: Season 2 episodes" maxlength="120" style="flex:1;min-width:220px">
        <button type="submit">Create batch</button>
      </form>
      <div class="grid" style="margin-top:16px">
        ${data.batches.map((b) => `
          <a class="panel" style="text-decoration:none;color:inherit" href="#/batch/${esc(b.id)}">
            <strong>${esc(b.name)}</strong>
            <div class="small muted">${b.projects} videos · ${b.settings ? "settings saved" : "no settings yet"}</div>
            <div class="small muted">${Object.entries(b.status_counts).map(([k, v]) => `${esc(STATUS_LABEL[k] || k)}: ${v}`).join(" · ") || "Empty"}</div>
          </a>`).join("") || "<p class='muted'>No batches yet.</p>"}
      </div>`;
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
      <tr><td><a href="#/project/${esc(p.id)}">${esc(p.title)}</a></td><td>${statusBadge(p.status)}</td>
      <td class="small muted">${esc(p.status_message || "")}</td></tr>`).join("");

    app.innerHTML = `
      <h1>${esc(batch.name)}</h1>
      <section class="panel stack">
        <h2>Add videos</h2>
        <div class="grid">
          <div><label for="b_files">Upload videos (MP4, MOV, WEBM, MKV)</label><input id="b_files" type="file" multiple accept=".mp4,.mov,.webm,.mkv,video/*"></div>
          <div><label for="b_links">Or paste links, one per line</label><textarea id="b_links" rows="3" placeholder="https://example.com/video1.mp4"></textarea></div>
        </div>
        <label class="check"><input id="b_rights" type="checkbox"> I own these videos or have permission to process them.</label>
        <div class="row"><button id="addBtn">Add to batch</button><span id="addStatus" class="small muted"></span></div>
      </section>

      <form id="batchSettingsForm" class="panel stack">
        <h2>Clip settings for every video</h2>
        <div class="grid">
          <div><label for="bs_theme">Theme</label>
            <select id="bs_theme">${themes.themes.map((t) => `<option ${t === (cur.theme || v.default_theme) ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></div>
          <div><label for="bs_len">Clip length (seconds)</label><input id="bs_len" type="number" min="1" value="${esc(cur.clip_length || v.default_clip_length)}" required></div>
          <div><label for="bs_count">Clips per video</label><input id="bs_count" type="number" min="1" max="20" value="${esc(cur.clip_count || v.default_clip_count)}" required></div>
          <div><label for="bs_orient">Orientation</label><select id="bs_orient">${optionList(ORIENTATION_OPTIONS, cur.orientation || v.default_orientation)}</select></div>
          <div><label for="bs_res">Resolution</label><select id="bs_res">
            <option value="1080p" ${(cur.resolution || v.default_resolution) === "1080p" ? "selected" : ""}>1080p</option>
            <option value="720p" ${(cur.resolution || v.default_resolution) === "720p" ? "selected" : ""}>720p</option></select></div>
          <div><label for="bs_caps">Captions</label><select id="bs_caps">${optionList(CAPTION_OPTIONS, cur.captions_style || v.default_captions_style)}</select></div>
        </div>
        <label class="check"><input id="bs_mirror" type="checkbox" ${cur.mirror ? "checked" : ""}> Mirror the video</label>
        <div class="row">
          <button type="submit">Save and generate for every ready video</button>
          <span class="small muted">Videos still transcribing will start as soon as they are ready.</span>
        </div>
      </form>

      <section class="panel">
        <div class="row" style="justify-content:space-between"><h2 style="margin:0">Videos</h2>
          <button class="danger secondary" id="deleteBatchBtn">Delete batch</button></div>
        <div style="overflow-x:auto;margin-top:10px"><table><thead><tr><th>Video</th><th>Status</th><th>Detail</th></tr></thead>
          <tbody>${items || "<tr><td colspan='3' class='muted'>No videos yet.</td></tr>"}</tbody></table></div>
      </section>`;

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

    if ((batch.items || []).some((p) => ["queued", "acquiring", "preparing_audio", "transcribing", "planning", "rendering"].includes(p.status))) {
      pollTimer = setInterval(() => viewBatch(id), 4000);
    }
  }

  // -- analytics -------------------------------------------------------------------
  async function viewAnalytics() {
    setNav("analytics");
    stopPolling();
    const a = await api("/analytics");
    const row = (label, stats) => `<tr><td>${esc(label)}</td><td>${stats.clips}</td>
      <td>${stats.avg_views == null ? "-" : esc(stats.avg_views)}</td><td>${stats.avg_likes == null ? "-" : esc(stats.avg_likes)}</td></tr>`;
    const block = (title, map) => `
      <section class="panel"><h2>${esc(title)}</h2>
        <div style="overflow-x:auto"><table><thead><tr><th></th><th>Clips</th><th>Avg views</th><th>Avg likes</th></tr></thead>
        <tbody>${Object.entries(map).map(([k, v]) => row(k, v)).join("")}</tbody></table></div></section>`;

    app.innerHTML = `
      <h1>Analytics</h1>
      <p class="muted">Enter views and likes on each clip (on its project page). The averages below show which kinds of clips do best.</p>
      <div class="grid">
        <div class="panel"><div class="small muted">Clips with results</div><div style="font-size:1.4rem;font-weight:700">${a.clips_with_metrics} of ${a.clips_total}</div></div>
        <div class="panel"><div class="small muted">Average views</div><div style="font-size:1.4rem;font-weight:700">${a.overall.avg_views == null ? "-" : esc(a.overall.avg_views)}</div></div>
      </div>
      ${a.clips_with_metrics ? `
        ${block("By score", a.by_score)}
        ${block("By length", a.by_length)}
        ${block("By theme", a.by_theme)}
        ${block("With or without effects", { "With effects": a.with_effects, "Without effects": a.without_effects })}
        <section class="panel"><h2>Top clips</h2>
          <ol>${a.top_clips.map((c) => `<li><strong>${esc(c.views)} views</strong> · ${esc(c.likes || 0)} likes · score ${c.score == null ? "-" : c.score.toFixed(2)}<div class="small muted">${esc(c.title)} — ${esc(c.reason || "")}</div></li>`).join("")}</ol>
        </section>` : "<div class='panel'><p>No results yet. Add views to a few clips to see patterns.</p></div>"}`;
  }

  // -- router --------------------------------------------------------------------
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
      if (hash === "#/batches") return await viewBatches();
      if (hash.startsWith("#/batch/")) return await viewBatch(decodeURIComponent(hash.slice("#/batch/".length)));
      if (hash === "#/analytics") return await viewAnalytics();
      if (hash === "#/account") return viewAccount();
      if (hash === "#/admin") return await viewAdmin();
      stopPolling();
      return await viewProjects();
    } catch (err) {
      app.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
    }
  }

  async function boot() {
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
    window.addEventListener("hashchange", route);
    document.getElementById("signOut").addEventListener("click", signOut);
    route();
  }

  boot();
})();
