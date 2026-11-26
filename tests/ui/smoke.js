// Loads the real interface (index.html + app.js from the running server) in jsdom and visits
// every page. Reports runtime errors and whether each page rendered content.
const { JSDOM, VirtualConsole } = require("jsdom");
const fs = require("fs");

const BASE = "http://127.0.0.1:8011";
const SPEECH = process.argv[2];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, opts = {}) {
  const res = await fetch(BASE + "/api" + path, opts);
  const text = await res.text();
  let body = null;
  try { body = JSON.parse(text); } catch (_) { body = text; }
  return { status: res.status, body };
}

async function waitFor(path, predicate, timeoutMs = 120000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    const r = await api(path);
    if (predicate(r.body)) return r.body;
    await sleep(1000);
  }
  throw new Error("timed out waiting for " + path);
}

(async () => {
  const problems = [];

  // Seed data through the real API.
  const batch = (await api("/batches", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "Smoke batch" }) })).body;
  const form = new FormData();
  form.append("file", new Blob([fs.readFileSync(SPEECH)], { type: "video/mp4" }), "speech.mp4");
  form.append("batch_id", batch.id);
  const project = (await api("/projects", { method: "POST", body: form })).body;
  await waitFor(`/projects/${project.id}`, (p) => p.status === "awaiting_settings" || p.status === "failed", 900000);
  await api(`/projects/${project.id}/generate`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme: "Funny", clip_length: 6, clip_count: 1, captions_style: "karaoke",
      orientation: "vertical", resolution: "720p", mirror: false }) });
  await waitFor(`/projects/${project.id}`, (p) => p.status === "done" || p.status === "failed", 240000);

  // Load the real page.
  const html = await (await fetch(BASE + "/")).text();
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => problems.push("jsdom: " + (e.message || e)));
  vc.on("error", (e) => problems.push("console.error: " + e));
  const dom = new JSDOM(html, {
    url: BASE + "/#/projects", runScripts: "dangerously", resources: "usable",
    pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(window) {
      window.fetch = (u, o) => fetch(new URL(u, BASE), o);
      window.confirm = () => true;
      window.alert = () => {};
    },
  });
  const w = dom.window;
  w.addEventListener("error", (e) => problems.push("uncaught: " + e.message));
  await new Promise((r) => w.addEventListener("load", r));
  await sleep(1500);

  const pages = [
    ["#/projects", "Projects"],
    ["#/new", "New project"],
    ["#/setup", "Setup"],
    ["#/analytics", "Analytics"],
    ["#/batches", "Batches"],
    [`#/batch/${batch.id}`, "Smoke batch"],
    [`#/project/${project.id}`, "speech"],
    ["#/library", "Effect library"],
    ["#/admin", "Administration"],
    ["#/account", "Account"],
  ];
  for (const [hash, expect] of pages) {
    w.location.hash = hash;
    await sleep(1200);
    const app = w.document.getElementById("app");
    const text = app.textContent || "";
    const errorBox = app.querySelector(".notice.error");
    const ok = text.includes(expect) && !errorBox;
    console.log(`${ok ? "OK  " : "FAIL"} ${hash.padEnd(34)} ${errorBox ? "error: " + errorBox.textContent : ""}`);
    if (!ok) problems.push(`page ${hash} did not render "${expect}"`);
  }

  // Exercise a few controls on the project page: regenerate options and saving results.
  w.location.hash = `#/project/${project.id}`;
  await sleep(1200);
  const saveBtn = w.document.querySelector("[data-save-metrics]");
  if (saveBtn) {
    const input = w.document.querySelector(`[data-views="${saveBtn.dataset.saveMetrics}"]`);
    input.value = "321";
    saveBtn.click();
    await sleep(1000);
    const check = (await api(`/projects/${project.id}`)).body.clips[0];
    console.log("metrics saved via UI:", check.views === 321 ? "yes" : "no (" + check.views + ")");
    if (check.views !== 321) problems.push("metrics not saved from the UI");
  } else {
    problems.push("no results entry on the clip card");
  }

  console.log("\nproblems:", problems.length ? problems : "none");
  w.close();
  process.exit(problems.length ? 1 : 0);
})().catch((e) => {
  console.error("smoke test crashed:", e);
  process.exit(2);
});
