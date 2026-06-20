#!/usr/bin/env python3
"""Render GOVERNED_CODING_PIPELINE.md to a standalone HTML page whose mermaid diagrams are
interactive — mouse-wheel zoom toward the cursor, drag-to-pan at any zoom, +/-/Fit, and
fullscreen. Re-run after editing the .md:  python3 docs/product/build_doc_viewer.py
"""
from pathlib import Path

HERE = Path(__file__).parent
md = (HERE / "GOVERNED_CODING_PIPELINE.md").read_text()

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>The Governed Coding Pipeline</title>
<style>
  :root { --bg:#f6f8fa; --card:#fff; --line:#d8dee4; --ink:#1f2328; --muted:#636c76; --accent:#0969da; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family: ui-sans-serif, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }
  .doc { max-width: 960px; margin: 0 auto; padding: 36px 28px 80px; line-height: 1.6; }
  .doc h1 { font-size: 28px; border-bottom:1px solid var(--line); padding-bottom:.3em; }
  .doc h2 { font-size: 21px; margin-top: 1.6em; border-bottom:1px solid var(--line); padding-bottom:.3em; }
  .doc h3 { font-size: 17px; margin-top: 1.4em; }
  .doc code { background:#eef1f4; padding:.15em .4em; border-radius:5px; font-size:.88em; }
  .doc pre { background:#0d1117; color:#e6edf3; padding:14px; border-radius:10px; overflow:auto; }
  .doc pre code { background:none; padding:0; color:inherit; }
  .doc table { border-collapse: collapse; width:100%; font-size:14px; margin: .6em 0; }
  .doc th, .doc td { border:1px solid var(--line); padding:7px 10px; text-align:left; }
  .doc th { background:#f0f3f6; }
  .doc blockquote { border-left:4px solid var(--line); margin:0; padding:.2em 1em; color:var(--muted); }
  .doc a { color:var(--accent); }

  /* Interactive diagram */
  .diagram { border:1px solid var(--line); border-radius:12px; overflow:hidden; margin: 18px 0; background:#fff; }
  .diagram-head { display:flex; align-items:center; justify-content:flex-end; gap:6px;
                  padding:8px 12px; border-bottom:1px solid var(--line); background:#fafbfc; }
  .diagram-head .hint { margin-right:auto; color:var(--muted); font-size:11.5px; }
  .diagram button { font-size:12.5px; padding:4px 9px; border:1px solid #c2c8cf; background:#fff;
                    color:var(--ink); border-radius:7px; cursor:pointer; line-height:1; }
  .diagram button:hover { background:#f0f3f6; }
  .diagram button.primary { border-color:var(--accent); color:var(--accent); }
  .zlabel { font-size:11.5px; color:var(--muted); min-width:42px; text-align:center; font-variant-numeric:tabular-nums; }
  .stage { position:relative; height:460px; overflow:hidden; cursor:grab; touch-action:none;
           background: radial-gradient(circle at 1px 1px, #e7ebef 1px, transparent 0) 0 0/22px 22px; }
  .stage.grabbing { cursor:grabbing; }
  .layer { position:absolute; top:0; left:0; transform-origin:0 0; will-change:transform; }
  .layer svg { display:block; max-width:none !important; height:auto !important; }
  .stage:fullscreen, .stage:-webkit-full-screen { height:100vh; width:100vw; background:#fff; }
  .fs-exit { position:absolute; top:12px; right:12px; z-index:5; display:none; }
  .stage:fullscreen .fs-exit, .stage:-webkit-full-screen .fs-exit { display:inline-block; }
</style>
</head>
<body>
<div class="doc" id="doc">Rendering…</div>
<script type="text/markdown" id="src">__MARKDOWN__</script>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>
const MIN = 0.2, MAX = 12;

function panzoom(stage, layer, zlabel) {
  const st = { scale: 1, x: 0, y: 0 };
  const apply = () => { layer.style.transform = `translate(${st.x}px,${st.y}px) scale(${st.scale})`;
                        if (zlabel) zlabel.textContent = Math.round(st.scale * 100) + "%"; };
  const clamp = (s) => Math.max(MIN, Math.min(MAX, s));
  function fit() {
    const svg = layer.querySelector("svg"); if (!svg) return;
    const vb = svg.viewBox && svg.viewBox.baseVal;
    const w = vb && vb.width ? vb.width : svg.getBoundingClientRect().width || 600;
    const h = vb && vb.height ? vb.height : svg.getBoundingClientRect().height || 400;
    svg.setAttribute("width", w); svg.setAttribute("height", h);
    const sw = stage.clientWidth, sh = stage.clientHeight;
    st.scale = clamp(Math.min(sw / w, sh / h) * 0.92);
    st.x = (sw - w * st.scale) / 2; st.y = (sh - h * st.scale) / 2; apply();
  }
  function zoomAt(cx, cy, f) {
    const ns = clamp(st.scale * f); if (ns === st.scale) return;
    st.x = cx - (cx - st.x) * (ns / st.scale); st.y = cy - (cy - st.y) * (ns / st.scale);
    st.scale = ns; apply();
  }
  stage.addEventListener("wheel", (e) => { e.preventDefault(); const r = stage.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : 1 / 1.12); }, { passive: false });
  let drag = null;
  stage.addEventListener("mousedown", (e) => { drag = { sx:e.clientX, sy:e.clientY, ox:st.x, oy:st.y };
    stage.classList.add("grabbing"); });
  window.addEventListener("mousemove", (e) => { if (!drag) return;
    st.x = drag.ox + (e.clientX - drag.sx); st.y = drag.oy + (e.clientY - drag.sy); apply(); });
  window.addEventListener("mouseup", () => { drag = null; stage.classList.remove("grabbing"); });
  let pinch = null;
  stage.addEventListener("touchstart", (e) => {
    if (e.touches.length === 1) drag = { sx:e.touches[0].clientX, sy:e.touches[0].clientY, ox:st.x, oy:st.y };
    else if (e.touches.length === 2) { const [a,b]=e.touches; pinch = { d: Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY) }; }
  }, { passive: true });
  stage.addEventListener("touchmove", (e) => { const r = stage.getBoundingClientRect();
    if (e.touches.length === 2 && pinch) { const [a,b]=e.touches;
      const d = Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);
      zoomAt((a.clientX+b.clientX)/2-r.left,(a.clientY+b.clientY)/2-r.top, d/pinch.d); pinch.d = d;
    } else if (e.touches.length === 1 && drag) {
      st.x = drag.ox + (e.touches[0].clientX-drag.sx); st.y = drag.oy + (e.touches[0].clientY-drag.sy); apply(); }
  }, { passive: true });
  stage.addEventListener("touchend", () => { drag = null; pinch = null; });
  return { fit, zoom: (f) => { const r = stage.getBoundingClientRect(); zoomAt(r.width/2, r.height/2, f); } };
}

(async function () {
  const md = document.getElementById("src").textContent;
  document.getElementById("doc").innerHTML = marked.parse(md);
  mermaid.initialize({ startOnLoad:false, theme:"default", securityLevel:"loose",
                       flowchart:{ htmlLabels:true, curve:"basis" } });
  const blocks = [...document.querySelectorAll("code.language-mermaid")];
  for (let i = 0; i < blocks.length; i++) {
    const code = blocks[i].textContent.trim();
    const pre = blocks[i].closest("pre");
    let svg;
    try { ({ svg } = await mermaid.render("mm" + i, code)); }
    catch (e) { pre.outerHTML = `<pre>Diagram failed: ${(e&&e.message)||e}</pre>`; continue; }
    const card = document.createElement("div"); card.className = "diagram";
    card.innerHTML = `
      <div class="diagram-head">
        <span class="hint">scroll = zoom · drag = pan</span>
        <button data-act="out">−</button><span class="zlabel">100%</span><button data-act="in">+</button>
        <button data-act="fit">Fit</button><button class="primary" data-act="fs">⤢ Fullscreen</button>
      </div>
      <div class="stage"><button class="fs-exit" data-act="exit">Exit ✕</button><div class="layer"></div></div>`;
    pre.replaceWith(card);
    const stage = card.querySelector(".stage"), layer = card.querySelector(".layer");
    layer.innerHTML = svg;
    const pz = panzoom(stage, layer, card.querySelector(".zlabel"));
    requestAnimationFrame(() => pz.fit());
    card.addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-act]"); if (!b) return; const a = b.dataset.act;
      if (a === "in") pz.zoom(1.25); else if (a === "out") pz.zoom(1/1.25); else if (a === "fit") pz.fit();
      else if (a === "fs") (stage.requestFullscreen||stage.webkitRequestFullscreen).call(stage);
      else if (a === "exit") (document.exitFullscreen||document.webkitExitFullscreen).call(document);
    });
    document.addEventListener("fullscreenchange", () => setTimeout(() => pz.fit(), 80));
  }
})();
</script>
</body>
</html>
"""

out = TEMPLATE.replace("__MARKDOWN__", md)
(HERE / "GOVERNED_CODING_PIPELINE.html").write_text(out)
print("wrote", HERE / "GOVERNED_CODING_PIPELINE.html", "(", len(out), "bytes )")
