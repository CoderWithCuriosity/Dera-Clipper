const { JSDOM, VirtualConsole } = require("jsdom");
const BASE = "http://127.0.0.1:8012";
(async () => {
  const problems = [];
  const html = await (await fetch(BASE + "/")).text();
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => problems.push("jsdom: " + (e.message || e)));
  const dom = new JSDOM(html, { url: BASE + "/#/projects", runScripts: "dangerously", resources: "usable",
    pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, BASE), o); w.confirm = () => true; } });
  const w = dom.window;
  w.addEventListener("error", (e) => problems.push("uncaught: " + e.message));
  await new Promise((r) => w.addEventListener("load", r));
  await new Promise((r) => setTimeout(r, 1500));
  const heading = w.document.querySelector("#app h1");
  const signedOutView = heading && heading.textContent === "Sign in";
  console.log("signed-out page shows sign-in:", signedOutView);
  if (!signedOutView) problems.push("sign-in page not shown when signed out");
  // Create the account through the form, the way a user would.
  w.document.getElementById("tabRegister").click();
  w.document.getElementById("a_email").value = "smoke@example.com";
  w.document.getElementById("a_pass").value = "a long enough password";
  w.document.getElementById("authForm").dispatchEvent(new w.Event("submit", { cancelable: true, bubbles: true }));
  await new Promise((r) => setTimeout(r, 2000));
  const who = w.document.getElementById("who").textContent;
  const adminLink = w.document.querySelector('[data-nav="admin"]');
  console.log("after register, signed in as:", who || "(nobody)", "| admin link visible:", adminLink && !adminLink.hidden);
  if (who !== "smoke@example.com") problems.push("register did not sign the user in");
  if (!(adminLink && !adminLink.hidden)) problems.push("first account should see the admin link");
  console.log("problems:", problems.length ? problems : "none");
  w.close();
  process.exit(problems.length ? 1 : 0);
})().catch((e) => { console.error("crashed:", e); process.exit(2); });
