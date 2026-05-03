"""Tests for the preset CRUD endpoints (`routes/presets.py`) and the
dangling-preset UI branch in the dashboard renderer.

Phase 2 of the preset library refactor: phase 1 made the registry
disk-backed; this phase exposes operator-facing CRUD and surfaces the
"deleted reference" identity state in the dashboard.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fresh_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    return main


# ----- list / get ----------------------------------------------------


def test_list_presets_returns_seeded_builtins(tmp_path, monkeypatch):
    """Fresh boot: every name in `_BUILTIN_SEEDS` is written to disk
    and surfaces in `GET /presets`. Sourced from the seeds dict so a
    new shipped algorithm's seed adds itself without a test edit."""
    import presets as _presets
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/presets")
    assert r.status_code == 200, r.text
    names = sorted(p["name"] for p in r.json()["presets"])
    expected = sorted(_presets._BUILTIN_SEEDS.keys())
    assert names == expected


def test_get_preset_returns_full_record(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/presets/blue_ball")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["name"] == "blue_ball"
    assert p["label"] == "Blue ball"
    # Wire shape is canonical `{algorithm_id, name, label, params}`;
    # `params` is opaque per-algorithm. v11_hsv_cc lays out
    # `{hsv, shape_gate}` inside it. No legacy v11 flat surface.
    assert p["params"]["hsv"]["h_min"] == 105
    assert p["params"]["shape_gate"]["aspect_min"] == pytest.approx(0.75)
    assert "hsv" not in p, "wire must not still emit legacy flat hsv"
    assert "shape_gate" not in p, "wire must not still emit legacy flat shape_gate"
    # Phase-1 algorithm_id discriminator. Today every preset targets
    # the sole registered algorithm; the field exists so future entries
    # with different params shapes can ship in the same directory.
    import algorithms
    assert p["algorithm_id"] == algorithms.DEFAULT_ALGORITHM_ID


def test_list_presets_wire_shape_is_canonical(tmp_path, monkeypatch):
    """`GET /presets` must emit canonical `{algorithm_id, name, label,
    params}` for every entry — never the legacy v11 flat surface
    (`hsv` / `shape_gate` at the top level). The single-record `GET
    /presets/{name}` shape is pinned by `test_get_preset_returns_full_record`;
    this guards the list-endpoint shape so a future serializer split
    can't regress one without the other."""
    import algorithms
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/presets")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "presets" in body and isinstance(body["presets"], list)
    assert body["presets"], "boot must seed at least one preset"
    for entry in body["presets"]:
        assert set(entry.keys()) == {"algorithm_id", "name", "label", "params"}, (
            f"unexpected keys on {entry.get('name')}: {sorted(entry)}"
        )
        algorithms.validate_id(entry["algorithm_id"])
        assert isinstance(entry["params"], dict)
        # v11 entries lay out `{hsv, shape_gate}` inside params; this is
        # the only registered runnable algorithm whose seed surfaces here.
        if entry["algorithm_id"] == algorithms.V11_HSV_CC:
            assert "hsv" in entry["params"]
            assert "shape_gate" in entry["params"]


def test_load_preset_backfills_algorithm_id_into_legacy_file(
    tmp_path, monkeypatch,
):
    """A preset file written before phase-1 lacks `algorithm_id`. First
    runtime read backfills the registry default AND rewrites the file
    canonical so subsequent boots see an explicit field. Drives the
    one-shot read migration on `presets._read_with_migration`."""
    import json
    pdir = tmp_path / "presets"
    pdir.mkdir(parents=True, exist_ok=True)
    legacy_body = {
        # No `algorithm_id` key.
        "name": "legacy_test",
        "label": "Legacy",
        "hsv": {"h_min": 0, "h_max": 1, "s_min": 0, "s_max": 1, "v_min": 0, "v_max": 1},
        "shape_gate": {"aspect_min": 0.5, "fill_min": 0.5},
    }
    (pdir / "legacy_test.json").write_text(json.dumps(legacy_body))

    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/presets/legacy_test")
    assert r.status_code == 200, r.text
    import algorithms
    assert r.json()["algorithm_id"] == algorithms.DEFAULT_ALGORITHM_ID

    # File on disk now carries the explicit field.
    persisted = json.loads((pdir / "legacy_test.json").read_text())
    assert persisted["algorithm_id"] == algorithms.DEFAULT_ALGORITHM_ID


def test_unknown_algorithm_id_in_preset_file_fails_loud(
    tmp_path, monkeypatch,
):
    """A preset file naming an algorithm not in the registry must fail
    at read time — defensive against future-version files copied
    backwards or operator typos."""
    import json
    pdir = tmp_path / "presets"
    pdir.mkdir(parents=True, exist_ok=True)
    bad = {
        "algorithm_id": "v999_not_registered",
        "name": "bad_algo",
        "label": "Bad algo",
        "hsv": {"h_min": 0, "h_max": 1, "s_min": 0, "s_max": 1, "v_min": 0, "v_max": 1},
        "shape_gate": {"aspect_min": 0.5, "fill_min": 0.5},
    }
    (pdir / "bad_algo.json").write_text(json.dumps(bad))

    main = _fresh_main(tmp_path, monkeypatch)
    # Both list_presets (iterates dir) and load_preset (single file)
    # must surface the loud failure — they're independent code paths
    # and could regress separately.
    with pytest.raises(ValueError, match="v999_not_registered"):
        main.state.list_presets()
    with pytest.raises(ValueError, match="v999_not_registered"):
        main.state.load_preset("bad_algo")


def test_get_unknown_preset_returns_404(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/presets/no_such")
    assert r.status_code == 404


# ----- create --------------------------------------------------------


_BODY_VALID = {
    "name": "indoor_overcast",
    "label": "Indoor / overcast",
    "algorithm_id": "v11_hsv_cc",
    "params": {
        "hsv": {"h_min": 100, "h_max": 130, "s_min": 80, "s_max": 255, "v_min": 60, "v_max": 255},
        "shape_gate": {"aspect_min": 0.7, "fill_min": 0.5},
    },
}


_BODY_VALID_HYBRID = {
    "name": "hybrid_indoor",
    "label": "Hybrid indoor",
    "algorithm_id": "hybrid_28d",
    "params": {
        "prod_hsv": {"h_min": 105, "h_max": 112, "s_min": 140, "s_max": 255, "v_min": 40, "v_max": 255},
        "prod_shape": {"aspect_min": 0.75, "fill_min": 0.55},
        "v11_hsv": {"h_min": 103, "h_max": 118, "s_min": 120, "s_max": 255, "v_min": 30, "v_max": 255},
        "v11_shape": {"aspect_min": 0.40, "fill_min": 0.35},
    },
}


def test_create_preset_persists_to_disk(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post("/presets", json=_BODY_VALID)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "indoor_overcast"
    assert (tmp_path / "presets" / "indoor_overcast.json").exists()
    # Round-trip: list now includes it.
    r = client.get("/presets")
    names = sorted(p["name"] for p in r.json()["presets"])
    assert "indoor_overcast" in names


def test_create_preset_round_trip_canonical_shape(tmp_path, monkeypatch):
    """`POST /presets` body and storage are both canonical
    `{name, label, algorithm_id, params}`. Round-tripping through
    `GET /presets/{new_name}` must return the same shape with values
    matching the POST body — no legacy flat-key surface leaks through.
    Pairs with `test_list_presets_wire_shape_is_canonical` to pin
    every public read path of preset records."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post("/presets", json=_BODY_VALID)
    assert r.status_code == 200, r.text
    r = client.get(f"/presets/{_BODY_VALID['name']}")
    assert r.status_code == 200, r.text
    p = r.json()
    assert set(p.keys()) == {"algorithm_id", "name", "label", "params"}
    assert p["algorithm_id"] == _BODY_VALID["algorithm_id"]
    assert p["name"] == _BODY_VALID["name"]
    assert p["label"] == _BODY_VALID["label"]
    assert p["params"]["hsv"]["h_min"] == _BODY_VALID["params"]["hsv"]["h_min"]
    assert p["params"]["hsv"]["h_max"] == _BODY_VALID["params"]["hsv"]["h_max"]
    assert p["params"]["shape_gate"]["aspect_min"] == pytest.approx(
        _BODY_VALID["params"]["shape_gate"]["aspect_min"]
    )
    assert "hsv" not in p
    assert "shape_gate" not in p


def test_create_preset_rejects_duplicate_name(tmp_path, monkeypatch):
    """Built-in `tennis` already exists at boot — create must 409, not
    silently overwrite. Preset filenames are immutable in the new
    model; operator must pick a fresh name to save tweaked values."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {**_BODY_VALID, "name": "tennis"}
    r = client.post("/presets", json=body)
    assert r.status_code == 409, r.text
    assert "immutable" in r.json()["detail"]


def test_create_preset_switches_active_to_new(tmp_path, monkeypatch):
    """`POST /presets` is the dashboard Apply path: save + auto-switch.
    The newly-created preset becomes the active one so the operator
    sees their just-applied values bound by name on the next render."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    # Default boot active is `tennis` — confirm before swap.
    assert main.state.detection_config().preset == "tennis"
    r = client.post("/presets", json=_BODY_VALID)
    assert r.status_code == 200, r.text
    assert main.state.detection_config().preset == "indoor_overcast"


def test_create_preset_rejects_invalid_slug(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for bad in ("With Space", "UPPER", "has-dash", "x" * 33, ""):
        body = {**_BODY_VALID, "name": bad}
        r = client.post("/presets", json=body)
        assert r.status_code == 400, (bad, r.text)


def test_create_preset_rejects_missing_top_level_fields(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for missing in ("name", "label", "algorithm_id", "params"):
        body = {**_BODY_VALID}
        del body[missing]
        r = client.post("/presets", json=body)
        assert r.status_code == 400, (missing, r.text)


def test_create_preset_rejects_unknown_algorithm_id(tmp_path, monkeypatch):
    """`algorithm_id` must name a runnable detector — typo / future
    version copied backwards / non-runnable data source like
    `ios_capture_time` all fail at the system boundary before any disk
    write. 422 (semantically invalid value) so dashboard distinguishes
    from 400 (missing field)."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {**_BODY_VALID, "name": "bad_algo", "algorithm_id": "v999_not_real"}
    r = client.post("/presets", json=body)
    assert r.status_code == 422, r.text
    assert "v999_not_real" in r.json()["detail"]


def test_create_preset_rejects_non_runnable_algorithm_id(tmp_path, monkeypatch):
    """`ios_capture_time` is a valid wire identity but a non-runnable
    data source (no Detector → no params_schema). Reject at write
    time; otherwise the resulting preset would dangle (`POST
    /presets/active` would crash on accessor lookup)."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {**_BODY_VALID, "name": "ios_preset", "algorithm_id": "ios_capture_time"}
    r = client.post("/presets", json=body)
    assert r.status_code == 422, r.text


def test_create_preset_rejects_schema_mismatched_params(tmp_path, monkeypatch):
    """`params` must round-trip through the algorithm's
    `params_schema`. A v11 preset missing `shape_gate` from `params`,
    or carrying an extra field, surfaces Pydantic's structured error
    list at 422 — dashboard form generator can highlight the offending
    dotted path."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    # Missing shape_gate inside params — V11Params requires it.
    bad_missing = {
        **_BODY_VALID,
        "name": "missing_sg",
        "params": {"hsv": _BODY_VALID["params"]["hsv"]},
    }
    r = client.post("/presets", json=bad_missing)
    assert r.status_code == 422, r.text
    # Extra unknown key inside params — V11Params allows extras by
    # default; flip when V11Params gains extra='forbid'. For now,
    # confirm hybrid_28d (which IS extra='forbid') rejects extras.
    bad_extra = {
        **_BODY_VALID_HYBRID,
        "name": "hybrid_extra",
        "params": {**_BODY_VALID_HYBRID["params"], "rogue_field": 42},
    }
    r = client.post("/presets", json=bad_extra)
    assert r.status_code == 422, r.text


def test_create_hybrid_preset_persists_without_touching_live(tmp_path, monkeypatch):
    """POST a hybrid_28d preset → file written; live `DetectionConfig`
    must NOT change (it's still v11_hsv_cc). Phase-3 will introduce a
    dual active state where hybrid can sit in the server_post slot.
    Until then, creating a non-v11 preset is pure persistence."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    before_cfg = main.state.detection_config()
    r = client.post("/presets", json=_BODY_VALID_HYBRID)
    assert r.status_code == 200, r.text
    assert (tmp_path / "presets" / "hybrid_indoor.json").exists()
    after_cfg = main.state.detection_config()
    # Live config untouched — same preset name, same algorithm_id.
    assert after_cfg.preset == before_cfg.preset
    assert after_cfg.algorithm_id == before_cfg.algorithm_id
    # And the new preset reads back canonical.
    r = client.get("/presets/hybrid_indoor")
    p = r.json()
    assert p["algorithm_id"] == "hybrid_28d"
    assert p["params"]["prod_hsv"]["h_min"] == 105
    assert p["params"]["v11_hsv"]["h_max"] == 118


# ----- set active ----------------------------------------------------


def test_set_active_preset_switches_without_writing(tmp_path, monkeypatch):
    """Pure switch: `POST /presets/active` only loads the preset's
    values and binds the live `DetectionConfig` to it. No new file is
    written — the dashboard preset dropdown calls this when the
    operator selects an existing preset."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert main.state.detection_config().preset == "tennis"
    r = client.post(
        "/presets/active", json={"name": "blue_ball", "target": "live"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "active": "blue_ball", "target": "live"}
    assert main.state.detection_config().preset == "blue_ball"
    # Sliders snap to blue_ball's values.
    bb = main.state.load_preset("blue_ball")
    assert main.state.hsv_range() == bb.hsv
    assert main.state.shape_gate() == bb.shape_gate


def test_set_active_preset_rejects_unknown_name(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post(
        "/presets/active", json={"name": "no_such", "target": "live"},
    )
    assert r.status_code == 404, r.text


def test_set_active_preset_rejects_missing_name(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post("/presets/active", json={"target": "live"})
    assert r.status_code == 400, r.text


def test_set_active_live_rejects_non_v11_preset(tmp_path, monkeypatch):
    """`target=live` only accepts v11_hsv_cc (iOS-side detection is
    hardcoded HSV+CC). Activating a hybrid_28d preset on the live slot
    would lie about what's running on iOS — reject 422."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert main.state.preset_exists("hybrid_28d_blue_ball")
    before = main.state.detection_config().preset
    r = client.post(
        "/presets/active",
        json={"name": "hybrid_28d_blue_ball", "target": "live"},
    )
    assert r.status_code == 422, r.text
    assert "hybrid_28d" in r.json()["detail"]
    assert main.state.detection_config().preset == before


def test_set_active_requires_target(tmp_path, monkeypatch):
    """Phase-3: `target` is required (no default). Operator must
    explicitly choose live vs server_post — silent default to "live"
    would tempt the dashboard into not thinking about which slot it's
    driving and would silently no-op a hybrid preset Apply."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post("/presets/active", json={"name": "blue_ball"})
    assert r.status_code == 400, r.text
    assert "target" in r.json()["detail"]


def test_set_active_rejects_invalid_target(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post(
        "/presets/active",
        json={"name": "blue_ball", "target": "iphone"},
    )
    assert r.status_code == 422, r.text
    assert "target" in r.json()["detail"]


def test_set_active_server_post_accepts_hybrid_preset(tmp_path, monkeypatch):
    """`target=server_post` accepts any registered algorithm. Switching
    to hybrid_28d here must NOT touch live `DetectionConfig` and must
    NOT broadcast WS settings (iOS doesn't run the server_post
    algorithm). State is persisted to a sidecar file so a restart
    keeps the operator's choice."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    live_before = main.state.detection_config()
    r = client.post(
        "/presets/active",
        json={"name": "hybrid_28d_blue_ball", "target": "server_post"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "ok": True, "active": "hybrid_28d_blue_ball", "target": "server_post",
    }
    # server_post slot updated.
    assert main.state.active_server_post_preset_name() == "hybrid_28d_blue_ball"
    # Live unchanged.
    live_after = main.state.detection_config()
    assert live_after.preset == live_before.preset
    assert live_after.algorithm_id == live_before.algorithm_id
    # Sidecar persisted.
    sidecar = tmp_path / "active_server_post_preset.json"
    assert sidecar.exists()
    import json
    assert json.loads(sidecar.read_text()) == {"name": "hybrid_28d_blue_ball"}


def test_set_active_server_post_accepts_v11_preset(tmp_path, monkeypatch):
    """server_post slot accepts v11 too — operator is free to run the
    same algorithm in both slots, or use hybrid as oracle while keeping
    v11 live. No algorithm constraint on server_post."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post(
        "/presets/active",
        json={"name": "blue_ball", "target": "server_post"},
    )
    assert r.status_code == 200, r.text
    assert main.state.active_server_post_preset_name() == "blue_ball"


def test_set_active_server_post_unknown_preset_returns_404(
    tmp_path, monkeypatch,
):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post(
        "/presets/active",
        json={"name": "no_such", "target": "server_post"},
    )
    assert r.status_code == 404, r.text


def test_active_server_post_preset_default_matches_live(tmp_path, monkeypatch):
    """Boot default: server_post slot points at the same preset as
    live, so first-run operators don't need an extra "pick one" step
    before the Run server button works. Persistence is empty on first
    boot — sidecar file shouldn't exist yet."""
    main = _fresh_main(tmp_path, monkeypatch)
    live = main.state.detection_config().preset
    assert main.state.active_server_post_preset_name() == live
    sidecar = tmp_path / "active_server_post_preset.json"
    assert not sidecar.exists()


def test_active_server_post_preset_persisted_across_restart(
    tmp_path, monkeypatch,
):
    """A second `State(data_dir=tmp_path)` (simulating restart) must
    read the sidecar back. Drives the `_load_active_server_post_preset_or_default`
    path."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post(
        "/presets/active",
        json={"name": "hybrid_28d_blue_ball", "target": "server_post"},
    )
    # Simulate restart.
    fresh = main.State(data_dir=tmp_path)
    assert fresh.active_server_post_preset_name() == "hybrid_28d_blue_ball"


# ----- delete --------------------------------------------------------


def test_delete_preset_unlinks_file(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.delete("/presets/blue_ball")
    assert r.status_code == 200, r.text
    assert not (tmp_path / "presets" / "blue_ball.json").exists()
    r = client.get("/presets/blue_ball")
    assert r.status_code == 404


def test_delete_unknown_preset_returns_404(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.delete("/presets/no_such")
    assert r.status_code == 404


def test_delete_active_preset_returns_409(tmp_path, monkeypatch):
    """Active preset can never be left dangling at the route layer —
    operator must switch active first via POST /presets/active. The
    state-level `delete_preset` accepts the unlink (used by the
    dangling-reference renderer test) but the HTTP route enforces the
    invariant."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    # Default active is `tennis`.
    assert main.state.detection_config().preset == "tennis"
    r = client.delete("/presets/tennis")
    assert r.status_code == 409, r.text
    assert "currently active" in r.json()["detail"]
    # Switching active first releases the lock.
    r = client.post(
        "/presets/active", json={"name": "blue_ball", "target": "live"},
    )
    assert r.status_code == 200, r.text
    r = client.delete("/presets/tennis")
    assert r.status_code == 200, r.text


# ----- dangling preset reference in dashboard -----------------------


def test_dashboard_shows_deleted_when_bound_preset_removed(tmp_path, monkeypatch):
    """Set live config to `preset=blue_ball` (preset-pure), then delete
    the preset file. Dashboard render must NOT crash and must show the
    `identity-deleted` branch with the dangling slug visible to the
    operator."""
    main = _fresh_main(tmp_path, monkeypatch)
    from detection_config import DetectionConfig

    # Bind to blue_ball cleanly.
    bb = main.state.load_preset("blue_ball")
    main.state.set_detection_config(DetectionConfig(
        hsv=bb.hsv, shape_gate=bb.shape_gate,
        preset="blue_ball", last_applied_at=None,
    ))

    # Operator (or cli) deletes the preset file out from under us.
    main.state.delete_preset("blue_ball")

    client = TestClient(main.app)
    body = client.get("/").text
    # Renderer survived — dashboard still serves.
    assert 'id="hsv-body"' in body
    # Dangling-reference branch surfaced visually.
    assert "identity-deleted" in body
    assert "(preset deleted)" in body
    # No reset-to-preset button (target is gone).
    assert 'data-detection-reset-preset="blue_ball"' not in body


def test_dashboard_renders_save_as_new_and_manage_buttons(tmp_path, monkeypatch):
    """The Detection-config card must surface the `Manage…` button, and
    the Manage modal must SSR a row per preset with Use / Duplicate /
    Delete actions. Save-as-new was consolidated into the Apply button
    (one button, one prompt) so `data-preset-save-as` no longer renders."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = client.get("/").text
    assert 'data-preset-save-as' not in body
    assert 'data-preset-manage' in body
    assert 'id="preset-manage-modal"' in body
    # Each seeded preset surfaces in the modal table with all three
    # row actions.
    for slug in ("tennis", "blue_ball"):
        assert f'data-preset-use="{slug}"' in body
        assert f'data-preset-duplicate="{slug}"' in body
        assert f'data-preset-delete="{slug}"' in body


def test_dashboard_marks_active_preset_in_manage_modal(tmp_path, monkeypatch):
    """The currently-bound preset is decorated with a ★ marker so the
    operator can locate it in the library list."""
    main = _fresh_main(tmp_path, monkeypatch)
    from detection_config import DetectionConfig

    bb = main.state.load_preset("blue_ball")
    main.state.set_detection_config(DetectionConfig(
        hsv=bb.hsv, shape_gate=bb.shape_gate,
        preset="blue_ball", last_applied_at=None,
    ))
    client = TestClient(main.app)
    body = client.get("/").text
    # `presets.list_presets` returns slugs sorted, so blue_ball precedes
    # tennis in the modal table. The current marker must sit before the
    # tennis row; if it appears after, it's been attached to the wrong
    # preset.
    blue_idx = body.index('data-preset-use="blue_ball"')
    tennis_idx = body.index('data-preset-use="tennis"')
    assert blue_idx < tennis_idx
    star = body.find("★ current")
    assert star != -1
    assert star < tennis_idx


def test_dashboard_renders_after_creating_custom_preset(tmp_path, monkeypatch):
    """A user-created preset surfaces in the dashboard's preset-button
    row, with its label HTML-escaped."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {
        "name": "rainy_day",
        "label": "Rainy <day>",  # angle brackets to verify escape
        "algorithm_id": "v11_hsv_cc",
        "params": {
            "hsv": {"h_min": 100, "h_max": 130, "s_min": 80, "s_max": 255, "v_min": 60, "v_max": 255},
            "shape_gate": {"aspect_min": 0.7, "fill_min": 0.5},
        },
    }
    r = client.post("/presets", json=body)
    assert r.status_code == 200, r.text
    page = client.get("/").text
    assert 'data-hsv-preset="rainy_day"' in page
    # Label should be escaped — raw angle bracket must NOT appear in the
    # button text. (HTML attributes elsewhere may carry escaped forms.)
    assert "Rainy &lt;day&gt;" in page
    assert "Rainy <day>" not in page
