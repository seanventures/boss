"""THE POPOVER GUARD, shared by the Add a project, Add a server and Watch this app suites.

The report (operator, 2026-11-08, laptop 1440x860): Add a project's CODE popover opened under the
sheet's sticky footer and the sheet did not scroll to it — "when I click on something it does not
auto expand". Every box and chip popover is one rule (apps/web/components/box-pop.tsx): when it
opens it is WHOLE in view.

This guard enforces it everywhere the suites go:

  * `install(ctx)` puts a watcher on every page: each popover that opens is measured once it has
    settled, and anything outside what can be seen is recorded on the page;
  * `recorded(page)` is what every test's `check()` asserts empty;
  * `assert_whole(page, where)` measures every popover open now, for the dedicated walk;
  * `SIZES`: 1440, 1024 and 390, and a short 1440x760 window.

"What can be seen" is the viewport cut by every ancestor that clips (a sheet's scroll body ends
at its footer) and by the sheet itself; a popover must also not overlap a footer it is not in.
"""
from __future__ import annotations

from typing import Any

#: Every popover a box or chip opens, and the Name chip's inline edit.
POP_SEL = ".npx-pop, .as-pop, .as-keys, .npx-pchip[data-editing]"
#: (width, height): the three widths, and a short laptop window.
SIZES = [(1440, 900), (1024, 900), (390, 844), (1440, 760)]

_PROBLEMS = r"""
function __popProblems(el) {
  const out = [];
  const r = el.getBoundingClientRect();
  if (!el.isConnected || !r.width || !r.height) return out;
  const st = getComputedStyle(el);
  if (st.display === 'none' || st.visibility === 'hidden') return out;
  const name = (el.getAttribute('data-testid') || (typeof el.className === 'string' ? el.className : el.tagName)).slice(0, 40);
  const R = (x) => Math.round(x);
  let top = 0, left = 0, bottom = window.innerHeight, right = document.documentElement.clientWidth || window.innerWidth;
  for (let a = el.parentElement; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
    const s = getComputedStyle(a);
    if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
    const b = a.getBoundingClientRect();
    const t = b.top + a.clientTop, l = b.left + a.clientLeft;
    top = Math.max(top, t); left = Math.max(left, l);
    bottom = Math.min(bottom, t + a.clientHeight); right = Math.min(right, l + a.clientWidth);
  }
  const sheet = el.closest('.npx-sheet, .inf-sheet');
  if (sheet) {
    const S = sheet.getBoundingClientRect();
    top = Math.max(top, S.top); left = Math.max(left, S.left); bottom = Math.min(bottom, S.bottom); right = Math.min(right, S.right);
  }
  const tol = 1.5;
  if (r.top < top - tol || r.bottom > bottom + tol || r.left < left - tol || r.right > right + tol)
    out.push(`${name} [${R(r.left)},${R(r.top)} .. ${R(r.right)},${R(r.bottom)}] is not whole in what can be seen [${R(left)},${R(top)} .. ${R(right)},${R(bottom)}] at ${window.innerWidth}x${window.innerHeight}`);
  for (const f of document.querySelectorAll('.npx-foot, .as-foot')) {
    if (f.contains(el)) continue;
    const F = f.getBoundingClientRect();
    if (F.height && F.width && r.bottom > F.top + tol && r.top < F.bottom - tol && r.right > F.left && r.left < F.right)
      out.push(`${name} runs under the footer (popover bottom ${R(r.bottom)}, footer top ${R(F.top)}) at ${window.innerWidth}x${window.innerHeight}`);
  }
  return out;
}
"""

#: On every page: each popover that opens is measured once settled (its open animation, the
#: scroll that reveals it), and what is wrong is kept in window.__popBad.
INIT = _PROBLEMS + r"""
(() => {
  if (window.__popGuard) return;
  window.__popGuard = true;
  window.__popBad = [];
  const seen = new WeakSet();
  const settle = (el) => setTimeout(() => requestAnimationFrame(() => {
    if (!el.isConnected) return;
    for (const p of __popProblems(el)) if (window.__popBad.length < 20) window.__popBad.push(p);
  }), 450);
  const scan = () => {
    for (const el of document.querySelectorAll(%r)) { if (!seen.has(el)) { seen.add(el); settle(el); } }
  };
  const start = () => { new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true }); scan(); };
  if (document.documentElement) start(); else document.addEventListener('DOMContentLoaded', start);
})();
""" % POP_SEL

_NOW = "() => {" + _PROBLEMS + r"""
  const out = [];
  for (const el of document.querySelectorAll(%r)) out.push(...__popProblems(el));
  return out;
}""" % POP_SEL


def install(ctx: Any) -> None:
    """Watch every popover on every page of this browser context."""
    ctx.add_init_script(INIT)


def recorded(page: Any) -> list[str]:
    """What the watcher found on this page so far (popovers that opened not whole in view)."""
    try:
        return list(page.evaluate("() => (window.__popBad || []).slice()"))
    except Exception:  # noqa: BLE001 — mid-navigation: the new page starts clean
        return []


def assert_whole(page: Any, where: str) -> None:
    """Every popover open now is whole in view (after its animation and reveal settle)."""
    page.wait_for_timeout(450)
    bad = page.evaluate(_NOW)
    assert not bad, f"{where}: " + "\n  ".join(bad)


def context_kw(width: int, height: int) -> dict[str, Any]:
    kw: dict[str, Any] = dict(viewport={"width": width, "height": height}, color_scheme="dark")
    if width < 600:
        kw.update(is_mobile=True, has_touch=True, device_scale_factor=2)
    return kw
