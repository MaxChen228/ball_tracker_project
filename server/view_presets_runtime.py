"""Shared 3D camera-preset UI for dashboard and viewer.

Both pages show the same Plotly 3D scene. The 5 fixed views (ISO, CATCH,
SIDE, TOP, PITCHER) and the active-pill drag-clear behaviour must match
on both surfaces — pre-extraction the dashboard had no presets at all
and the viewer had its own copy in `static/viewer/75_view_presets.js`.

Inject ``VIEW_PRESETS_RUNTIME_JS`` as its own ``<script>`` block BEFORE
each page's main script. It exposes ``window.BallTrackerViewPresets``
with:

  - ``PRESETS``: the camera dict table (eye/up/center per view name),
    locked to the strike-zone centroid so the box stays mid-frame.
  - ``bind(sceneDiv, toolbarEl)``: wires `.view-preset[data-view=...]`
    buttons inside `toolbarEl` to ``Plotly.relayout`` on `sceneDiv`,
    plus a `plotly_relayouting` listener that clears the active pill on
    the first user drag.

CATCH and PITCHER raise eye.z above the strike-zone centroid so the
visual frustum tilts down toward home plate (z=0) — without this, the
sight line is exactly horizontal and the plate falls below the bottom
of the rendered viewport at default Plotly FOV.
"""
from __future__ import annotations

VIEW_PRESETS_RUNTIME_JS: str = r"""
(function () {
  if (window.BallTrackerViewPresets) return;
  const NS = {};

  // Strike zone centroid (mirror of render_scene_theme.py + viewer
  // 75_view_presets.js): X = plate centre, Y = (0 + 0.432) / 2,
  // Z = (0.46 + 1.06) / 2.
  const SZC = { x: 0, y: 0.216, z: 0.76 };

  // All presets use perspective. Tried orthographic for the four
  // orthogonal views — Plotly's ortho + aspectmode='data' interaction
  // shrank the scene to a tiny corner of the canvas (couldn't reproduce
  // a clean ortho fit without hand-coded aspectratio). Perspective with
  // tight eye magnitudes (2.2 m for orthogonal views, 1.6 m for ISO)
  // gives a serviceable approximation of a flat projection at this
  // working scale (~3 m bbox). CATCH / PITCHER eye.z lifted +0.4 m
  // above SZC.z so the sight line tilts ~10° down toward home plate
  // (z=0); without this the plate falls below the default vertical FOV
  // and reads as "catcher's view doesn't show the plate". TOP looks
  // straight down with up=+Y so pitcher reads "north".
  const PRESETS = {
    iso:     { eye: {x: SZC.x + 1.6, y: SZC.y + 1.6, z: SZC.z + 0.8}, up: {x: 0, y: 0, z: 1}, center: SZC },
    catch:   { eye: {x: SZC.x,        y: SZC.y - 2.2, z: SZC.z + 0.4}, up: {x: 0, y: 0, z: 1}, center: SZC },
    side:    { eye: {x: SZC.x - 2.2, y: SZC.y,        z: SZC.z       }, up: {x: 0, y: 0, z: 1}, center: SZC },
    top:     { eye: {x: SZC.x,        y: SZC.y,        z: SZC.z + 2.5}, up: {x: 0, y: 1, z: 0}, center: SZC },
    pitcher: { eye: {x: SZC.x,        y: SZC.y + 2.2, z: SZC.z + 0.4}, up: {x: 0, y: 0, z: 1}, center: SZC },
  };
  NS.PRESETS = PRESETS;

  NS.bind = function (sceneDiv, toolbarEl) {
    if (!sceneDiv || !toolbarEl) return;
    const btns = Array.from(toolbarEl.querySelectorAll(".view-preset[data-view]"));
    if (!btns.length) return;

    let suppressClear = false;
    function setActive(name) {
      for (const btn of btns) btn.classList.toggle("active", btn.dataset.view === name);
    }
    function clearActive() {
      for (const btn of btns) btn.classList.remove("active");
    }

    for (const btn of btns) {
      btn.addEventListener("click", () => {
        const preset = PRESETS[btn.dataset.view];
        if (!preset) return;
        suppressClear = true;
        // Deep clone so Plotly can't mutate our table.
        const cam = JSON.parse(JSON.stringify(preset));
        Plotly.relayout(sceneDiv, { "scene.camera": cam }).finally(() => {
          setActive(btn.dataset.view);
          setTimeout(() => { suppressClear = false; }, 0);
        });
      });
    }

    // Plotly only attaches `.on` after the first react/newPlot, which
    // can land after this binding runs. Retry per-frame until ready,
    // then hook plotly_relayouting to clear active pill on user drag.
    function hookRelayouting() {
      if (typeof sceneDiv.on !== "function") {
        requestAnimationFrame(hookRelayouting);
        return;
      }
      sceneDiv.on("plotly_relayouting", () => {
        if (!suppressClear) clearActive();
      });
    }
    hookRelayouting();
  };

  window.BallTrackerViewPresets = NS;
})();
"""


def view_presets_toolbar_html(*, default_view: str = "iso") -> str:
    """Return the 5-button toolbar markup. `default_view` controls which
    button starts with `.active` — viewer defaults to ISO; dashboard too."""
    buttons = [
        ("iso", "ISO", "Isometric overview (default)"),
        ("catch", "CATCH", "Catcher's view — strike zone front-on (X/Z plane)"),
        ("side", "SIDE", "1B-side view — trajectory arc (Y/Z plane)"),
        ("top", "TOP", "Top-down — horizontal break (X/Y plane)"),
        ("pitcher", "PITCHER", "Pitcher's view — looking back at catcher"),
    ]
    parts = ['<div class="scene-views" role="toolbar" aria-label="Camera presets">']
    for key, label, title in buttons:
        cls = "view-preset" + (" active" if key == default_view else "")
        parts.append(
            f'<button class="{cls}" type="button" data-view="{key}" '
            f'title="{title}">{label}</button>'
        )
    parts.append("</div>")
    return "".join(parts)


def assert_view_presets_present(html: str) -> None:
    """Sanity-check that a rendered page injected the runtime."""
    if "BallTrackerViewPresets" not in html:
        raise AssertionError("rendered page missing view-presets runtime injection")
