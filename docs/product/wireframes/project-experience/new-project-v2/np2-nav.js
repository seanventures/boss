/* Walk the v2/v3 screens with → / ← (Esc returns to the wireframe index). Pages with several states take
   one query key (a: ?mode=, d: ?state=); the walk is a(idea) → a(fix) → a(repo) → b → c → d(typing → chat →
   ready → refused) → e. Other query flags (?theme, ?notes) are kept while walking. */
(function () {
  const KEY = { "a-add-project.html": ["mode", "idea"], "d-new-work.html": ["state", "typing"] };
  const ORDER = ["a-add-project.html?mode=idea", "a-add-project.html?mode=fix", "a-add-project.html?mode=repo",
    "b-adjust.html", "c-after-create.html",
    "d-new-work.html?state=typing", "d-new-work.html?state=chat", "d-new-work.html?state=ready", "d-new-work.html?state=refused",
    "e-new-work-parity.html"];
  const file = (location.pathname.split("/").pop() || "");
  const q = new URLSearchParams(location.search);
  const k = KEY[file];
  const here = k ? `${file}?${k[0]}=${q.get(k[0]) || k[1]}` : file;
  const i = ORDER.indexOf(here);
  if (i < 0 || navigator.webdriver) return;
  if (k) q.delete(k[0]);
  const extra = q.toString();
  const go = (j) => {
    if (j < 0 || j >= ORDER.length) return;
    const t = ORDER[j];
    location.href = t + (extra ? (t.includes("?") ? "&" : "?") + extra : "");
  };
  document.addEventListener("keydown", (e) => {
    if (e.target && /input|textarea|select/i.test(e.target.tagName)) return;
    if (e.key === "ArrowRight" || e.key === " ") { e.preventDefault(); go(i + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); go(i - 1); }
    else if (e.key === "Escape") { location.href = "../index.html"; }
  });
  const tag = document.createElement("div");
  tag.textContent = `v3 · ${i + 1} / ${ORDER.length}  ·  ← →  ·  Esc index`;
  tag.style.cssText = "position:fixed;right:14px;bottom:12px;z-index:9999;font:12px/1 system-ui;" +
    "padding:7px 10px;border-radius:8px;background:rgba(15,23,42,.85);color:#cbd5e1;" +
    "border:1px solid rgba(148,163,184,.25);pointer-events:none";
  document.body.appendChild(tag);
})();
