(() => {
  const DATA = JSON.parse(document.getElementById("viewer-data").textContent);
  const SCENE = DATA.scene;
  // Strike-zone visibility — shared with dashboard via
  // window.BallTrackerOverlays (server/overlays_ui.py). One key, one
  // implementation, two surfaces.
  const _OVL = window.BallTrackerOverlays;
  const strikeZoneVisible = _OVL.strikeZoneVisible;
  const setStrikeZoneVisible = _OVL.setStrikeZoneVisible;
  const CAM_COLOR = DATA.camera_colors || {};
  const FALLBACK = DATA.fallback_color;
  const ACCENT = DATA.accent_color;
  // Two detection pipelines. Their string IDs match
  // server/schemas.py::DetectionPath so we never have to translate.
  const PATHS = ["live", "server_post"];
  const PATH_LABEL = { live: "live", server_post: "svr" };
  // reconstruct.py still tags rays with the older source strings; map here
  // once so the rest of the JS speaks in DetectionPath IDs exclusively.
  function sourceToPath(source) {
    if (source === "live") return "live";
    return "server_post";
  }
  // Per-path hue (source = colour), A/B shade within each hue (camera =
  // lightness). Replaces the earlier solid/dash/dot distinction — users
  // read hue faster than dash patterns in a dense 3D scene.
  const PATH_COLORS = {
    live:        { A: "#B8451F", B: "#E08B5F" },
    server_post: { A: "#4A6B8C", B: "#89A5BD" },
  };
  function colorForCamPath(cam, path) {
    const bucket = PATH_COLORS[path];
    if (bucket && bucket[cam]) return bucket[cam];
    return CAM_COLOR[cam] || FALLBACK;
  }
  const PATH_DASH = { live: "solid", server_post: "solid" };
  const PATH_OPACITY = { live: 0.55, server_post: 0.55 };
  const PATH_MARKER_SYMBOL = { live: "circle", server_post: "circle" };
  const SCENE_THEME = DATA.scene_theme || {
    cam_axis_len_m: 0.25, cam_fwd_len_m: 0.5,
    axis_color_right: "#C0392B", axis_color_up: "#2A2520",
  };
  const VIDEO_META = DATA.videos || [];
  const HAS_TRIANGULATED = DATA.has_triangulated;
  // SegmentRecord[] — persisted by session_results.stamp_segments_on_result
  // at session build / recompute. `segments` is the legacy single-path
  // surface; `segments_by_path` is the viewer's real source of truth so
  // PATH can switch fit + point semantics together. Both are mutable so
  // SSE / recompute patch-in-place can refresh without reloading.
  let SEGMENTS = Array.isArray(DATA.segments) ? DATA.segments : [];
  let SEGMENTS_BY_PATH = DATA.segments_by_path || {};
  const sceneDiv = document.getElementById("scene");
  const playBtn = document.getElementById("play-btn");
  const scrubber = document.getElementById("scrubber");
  const frameInput = document.getElementById("frame-input");
  const frameTotal = document.getElementById("frame-total");
  const frameSub = document.getElementById("frame-sub");
  const framePrimary = document.getElementById("frame-primary");
  const modeAll = document.getElementById("mode-all");
  const modePlayback = document.getElementById("mode-playback");
  const stepFirstBtn = document.getElementById("step-first");
  const stepBackBtn = document.getElementById("step-back");
  const stepFwdBtn = document.getElementById("step-fwd");
  const stepLastBtn = document.getElementById("step-last");
  const speedGroup = document.getElementById("speed-group");
  const hintBtn = document.getElementById("hint-btn");
  const hintOverlay = document.getElementById("hint-overlay");
  const vids = Array.from(document.querySelectorAll("video[data-cam]"));
  const offsetByCam = Object.fromEntries(VIDEO_META.map(v => [v.camera_id, v.t_rel_offset_s]));
  const fpsByCam = Object.fromEntries(VIDEO_META.map(v => [v.camera_id, v.fps]));
