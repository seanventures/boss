/* Walk the v2 screens with → / ← (Esc returns to the wireframe index). Screen a has two states:
   ?mode=idea (default) and ?mode=repo, so the walk is a(idea) → a(repo) → b → c. */
(function () {
  const ORDER = ["a-add-project.html?mode=idea", "a-add-project.html?mode=repo", "b-adjust.html", "c-after-create.html"];
  const file = (location.pathname.split("/").pop() || "");
  const mode = new URLSearchParams(location.search).get("mode") || "idea";
  const here = file === "a-add-project.html" ? `${file}?mode=${mode}` : file;
  const i = ORDER.indexOf(here);
  if (i < 0 || navigator.webdriver) return;
  const keep = new URLSearchParams(location.search); keep.delete("mode");
  const extra = keep.toString();
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
  tag.textContent = `v2 · ${i + 1} / ${ORDER.length}  ·  ← →  ·  Esc index`;
  tag.style.cssText = "position:fixed;right:14px;bottom:12px;z-index:9999;font:12px/1 system-ui;" +
    "padding:7px 10px;border-radius:8px;background:rgba(15,23,42,.85);color:#cbd5e1;" +
    "border:1px solid rgba(148,163,184,.25);pointer-events:none";
  document.body.appendChild(tag);
})();
