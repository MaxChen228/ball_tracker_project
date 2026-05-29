# Algorithm registry — SoT

This is the **single source of truth** for the detection-algorithm registry,
runnable IDs, params schema entry points, cost-threshold ownership, and the
live/server_post bucket-key convention. Conflict with other docs → this file
wins.

Source: [`server/algorithms/__init__.py`](../../server/algorithms/__init__.py).

## Entries

Module-level dict of `AlgorithmEntry` records keyed by `algorithm_id`:

| `algorithm_id`      | Runnable | Role                                                                    |
|---------------------|----------|-------------------------------------------------------------------------|
| `v11_hsv_cc`        | ✅       | HSV + connectedComponents + shape gate (driven by `detection.py`).      |
| `ios_capture_time`  | ❌       | Sentinel for the live-bucket dict key. Constant `IOS_CAPTURE_TIME_ALGORITHM_ID`. |

Each runnable entry carries:

- `algorithm_id`
- human label
- params JSON schema (consumed by dashboard Apply form generator + `POST /sessions/{sid}/runs/{algorithm_id}` validation)
- `cost_threshold` (algorithm-owned pairing-cost gate; see below)
- the run function

## Unified entry point

`run_detection(algorithm_id, mov_path, video_start_pts_s, params, …)` —
routes detection through the chosen algorithm without callers caring about
the underlying module.

## ID validation

- `validate_id(slug)` — accepts any registered id (including non-runnable
  `ios_capture_time`). Used wherever the id is a dict key.
- `validate_runnable_id(slug)` — rejects non-runnable ids with 422. Used by
  `POST /sessions/{sid}/runs/{algorithm_id}` and `POST /presets`.

## Boot-time drift guards

`_check_schemas_constant_drift` / `_check_legacy_bucket_in_registry` raise
loudly on import if `schemas.py` constants or persisted JSONs reference an
unregistered algorithm id. This catches a stale data dir or half-deleted
entry before serving traffic.

## Cost threshold — algorithm-owned

`cost_threshold_for_algorithm(algorithm_id)` returns the per-algorithm
pairing-cost gate. **`pairing.py` does NOT apply it** — pairing emits the
full triangulated set, filtered only by the absolute emit ceilings
(`_EMIT_COST_CEILING` / `_EMIT_GAP_CEILING_M`, disk/memory protection, not
operator-tunable). The per-algorithm `cost_threshold` gate is applied
**downstream** in `session_results._passes_stamped_filter`
(`server/session_results.py:636`, resolved per path via
`cost_threshold_for_algorithm`), alongside the operator's
`gap_threshold_m`, before the segmenter consumes the points. The legacy
operator-stamped `SessionResult.cost_threshold` is gone (b15a611 +
bc0e92e); `POST /sessions/{sid}/recompute` accepts only
`{gap_threshold_m: float}` now.

## Bucket-key convention

`PitchPayload.frames_by_algorithm` and `…config_used_by_algorithm` are
dicts keyed by `algorithm_id`:

- **Live path** → key is the sentinel `ios_capture_time`.
- **server_post path** → key is the runnable detector id (currently
  `v11_hsv_cc`; future detectors register through this same dict).

`active_server_post_algorithm_id` points at the bucket the viewer projects.
Re-running a different algorithm flips the pointer; prior buckets stay in
the dict (multi-algorithm history at the storage layer).

## Routes

- `GET /algorithms` → sorted list of registered **runnable** entries
  (algorithm_id, label, params schema, cost_threshold).
- `GET /algorithms/{algorithm_id}` → single entry. **404 on any
  unknown id INCLUDING non-runnable `ios_capture_time`**, so dashboard
  form generators can rely on it for "is this a runnable target?".

The dashboard's preset Apply form fetches `/algorithms` once on open and
`/algorithms/{algorithm_id}` per algorithm switch to rebuild the params
editor.

## Preset binding

Every named preset binds to exactly one `algorithm_id`. Presets for
different algorithms are NOT interchangeable. The dashboard Apply form
POSTs `{name, label, algorithm_id, params}` — `params` is validated
against the algorithm's `params_schema`. See
[../operations.md](../operations.md) for the preset library workflow.

## How to apply (when changing this surface)

- Adding a new algorithm → register here, update this table, update
  [protocols.md](protocols.md) (`GET /algorithms` schema sample,
  `POST /sessions/{sid}/runs/{algorithm_id}` error matrix), update
  [../server.md](../server.md) entry, and add a preset seed in
  `server/presets.py::_BUILTIN_SEEDS` only if the new algorithm
  should ship with a default preset.
- Changing a `params_schema` → bump validation in
  `routes/sessions.py::run_server_post_for_algorithm` if needed;
  existing presets stamped with the old schema will fail load —
  acceptable in experimental phase (no migration shim).
- Changing `cost_threshold_for_algorithm` → no client-visible API
  change, but [tuning-baselines.md](tuning-baselines.md) should note
  the new value if it differs materially from v11_hsv_cc's default.
