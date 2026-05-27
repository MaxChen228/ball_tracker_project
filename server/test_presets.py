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
    `params_schema`. A preset missing a required nested field surfaces
    Pydantic's structured error list at 422 — dashboard form generator
    can highlight the offending dotted path."""
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


def test_create_preset_v11_rejects_extra_param_field(tmp_path, monkeypatch):
    """V11Params is `extra='forbid'`. A rogue top-level key inside
    `params` (operator typo, stale frontend, future-version field
    pasted backwards) must 422 — silent drop would let the operator
    think a value was saved when it wasn't."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    bad = {
        **_BODY_VALID,
        "name": "v11_extra",
        "params": {**_BODY_VALID["params"], "rogue_field": 42},
    }
    r = client.post("/presets", json=bad)
    assert r.status_code == 422, r.text


def test_create_preset_malformed_algorithm_id_returns_400(tmp_path, monkeypatch):
    """An algorithm_id that doesn't match the slug regex (capitals,
    dashes, too long) is structurally invalid — 400, distinct from
    the 422 reserved for "well-formed but unknown / non-runnable".
    Without the split, a typo with capitals would 422 and the
    dashboard couldn't distinguish "fix the case" from "this id was
    retired"."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for bad_id in ("V11_HSV_CC", "v11-hsv-cc", "x" * 33, ""):
        body = {**_BODY_VALID, "name": "bad_fmt", "algorithm_id": bad_id}
        r = client.post("/presets", json=body)
        # Empty string falls through to "missing field" 400 first,
        # which is also 400 — both branches are correct here.
        assert r.status_code == 400, (bad_id, r.text)


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


def test_set_active_requires_target(tmp_path, monkeypatch):
    """`target` is required (no default). Operator must explicitly
    choose live vs server_post — silent default to "live" would tempt
    the dashboard into not thinking about which slot it's driving."""
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


def test_set_active_server_post_accepts_v11_preset(tmp_path, monkeypatch):
    """server_post slot accepts any registered algorithm — currently
    only v11. POST to switch to a different v11 preset must NOT touch
    live `DetectionConfig` and must NOT broadcast WS settings (iOS
    doesn't run the server_post algorithm). State is persisted to a
    sidecar file so a restart keeps the operator's choice."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    live_before = main.state.detection_config()
    r = client.post(
        "/presets/active",
        json={"name": "blue_ball", "target": "server_post"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "ok": True, "active": "blue_ball", "target": "server_post",
    }
    assert main.state.active_server_post_preset_name() == "blue_ball"
    # Live unchanged.
    live_after = main.state.detection_config()
    assert live_after.preset == live_before.preset
    assert live_after.algorithm_id == live_before.algorithm_id
    # Sidecar persisted.
    sidecar = tmp_path / "active_server_post_preset.json"
    assert sidecar.exists()
    import json
    assert json.loads(sidecar.read_text()) == {"name": "blue_ball"}


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
    before the Run server button works. Boot also writes the sidecar
    explicitly (see `test_active_server_post_first_boot_writes_sidecar_explicitly`)."""
    main = _fresh_main(tmp_path, monkeypatch)
    live = main.state.detection_config().preset
    assert main.state.active_server_post_preset_name() == live


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
        json={"name": "blue_ball", "target": "server_post"},
    )
    # Simulate restart.
    fresh = main.State(data_dir=tmp_path)
    assert fresh.active_server_post_preset_name() == "blue_ball"


def test_active_server_post_first_boot_writes_sidecar_explicitly(
    tmp_path, monkeypatch,
):
    """First boot has no sidecar — `_load_active_server_post_preset_or_default`
    MUST explicitly write one pointing at the live preset, not just
    silently default in memory. Otherwise the operator's first
    perceived 'choice' is a fallback no one persisted, and a hand-edit
    pre-restart is invisible."""
    main = _fresh_main(tmp_path, monkeypatch)
    sidecar = tmp_path / "active_server_post_preset.json"
    assert sidecar.exists(), "boot must seed the sidecar explicitly"
    import json
    body = json.loads(sidecar.read_text())
    assert body == {"name": main.state.detection_config().preset}


def test_corrupt_active_server_post_sidecar_raises_at_boot(
    tmp_path, monkeypatch,
):
    """A hand-edited corrupt sidecar must surface at boot with a
    typed message naming the path — no silent fallback to the live
    preset that would mask the operator's broken edit."""
    sidecar = tmp_path / "active_server_post_preset.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        _fresh_main(tmp_path, monkeypatch)


def test_delete_preset_rejects_active_server_post_slot(
    tmp_path, monkeypatch,
):
    """DELETE on the preset bound to the server_post active slot must
    409 — otherwise the sidecar dangles. The 409 mentions the
    server_post slot specifically so operator knows which slot to
    re-bind."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post(
        "/presets/active",
        json={"name": "blue_ball", "target": "server_post"},
    )
    r = client.delete("/presets/blue_ball")
    assert r.status_code == 409, r.text
    assert "server_post" in r.json()["detail"]


def test_set_active_target_is_case_sensitive(tmp_path, monkeypatch):
    """`target` accepts only lowercase exact matches. `"Live"` and
    `"LIVE"` 422 — same enum strictness the rest of the wire layer
    follows; silent normalisation would mask client typos."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for variant in ("Live", "LIVE", "Server_post"):
        r = client.post(
            "/presets/active",
            json={"name": "blue_ball", "target": variant},
        )
        assert r.status_code == 422, (variant, r.text)


def test_active_server_post_sidecar_shape_pinned(tmp_path, monkeypatch):
    """The sidecar wire shape MUST stay exactly `{"name": "..."}` —
    no extra fields creeping in (e.g. `set_at`, `algorithm_id`, etc.)
    that future readers might come to depend on. Pin independent of
    the round-trip test so a writer-side regression fails here."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post(
        "/presets/active",
        json={"name": "blue_ball", "target": "server_post"},
    )
    import json
    sidecar = tmp_path / "active_server_post_preset.json"
    body = json.loads(sidecar.read_text())
    assert set(body.keys()) == {"name"}
    assert body["name"] == "blue_ball"


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
    # Boot also seeded server_post slot to "tennis" (defaults to live).
    # Move it off so the test can isolate the live-slot 409 path.
    r = client.post(
        "/presets/active",
        json={"name": "blue_ball", "target": "server_post"},
    )
    assert r.status_code == 200, r.text
    r = client.delete("/presets/tennis")
    assert r.status_code == 409, r.text
    assert "currently active" in r.json()["detail"]
    # Switching live active too releases both slot locks.
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

    # Bind live to blue_ball cleanly. Server_post slot defaults to
    # "tennis" at boot, which doesn't collide with the blue_ball
    # delete below, so no extra slot move needed here.
    bb = main.state.load_preset("blue_ball")
    main.state.set_detection_config(DetectionConfig(
        hsv=bb.hsv, shape_gate=bb.shape_gate,
        preset="blue_ball", last_applied_at=None,
    ))

    # Switch live off blue_ball so delete_preset's active-slot guard
    # passes — this test exercises the dangling-reference RENDERER
    # branch, not the delete-while-active reject.
    main.state.set_detection_config(DetectionConfig(
        hsv=bb.hsv, shape_gate=bb.shape_gate,
        preset="tennis", last_applied_at=None,
    ))
    main.state.delete_preset("blue_ball")
    # Re-pin live to dangling "blue_ball" name to drive the renderer
    # branch. This SIMULATES an external `rm data/presets/blue_ball.json`
    # while a session was live-bound — the in-memory pointer survives
    # but the file is gone. `set_detection_config` (correctly) rejects
    # binding to a missing preset under its lock-held existence check,
    # so this test reaches into the private field to reproduce the
    # post-external-rm inconsistent state. The renderer's
    # `identity-deleted` branch is the safety net for exactly this case.
    main.state._detection_config = DetectionConfig(
        hsv=bb.hsv, shape_gate=bb.shape_gate,
        preset="blue_ball", last_applied_at=None,
    )

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


# ----- boot drift guard -----------------------------------------------


def test_builtin_seeds_validate_against_registry():
    """Importing `presets` runs `_validate_builtin_seeds_against_registry`
    at module level. If any seed's params drift from its algorithm's
    `params_schema` (e.g. a new required field is added to the detector
    without updating the seed literal), the import raises `RuntimeError`.
    This test confirms the guard function is present and that the current
    seeds all pass validation against their registered algorithm schemas."""
    import presets
    # Module-level call already ran on import — if seeds were invalid we
    # would have raised before reaching here.
    assert callable(presets._validate_builtin_seeds_against_registry)
    # Re-invoke explicitly to confirm it passes with the current seeds.
    presets._validate_builtin_seeds_against_registry()
