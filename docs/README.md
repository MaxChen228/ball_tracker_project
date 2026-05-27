# docs/

Canonical reference for ball_tracker. **Read the relevant doc(s) before
editing code or answering a question.** `CLAUDE.md` at the repo root is a
slim agent-rules pointer — the content lives here.

## Doc routing — semantic intent → path

Not sure where to look? Match your intent to the row. **(SoT)** marks the
authoritative file when two docs disagree.

| You're doing… | Read |
|---|---|
| First time this session — what is this thing? | [architecture.md](architecture.md) |
| Touching `ball_tracker/*.swift` / `*.mm` | [ios.md](ios.md) |
| Touching `server/*.py` (module index, entry points) | [server.md](server.md) |
| Running, calibrating, debugging a session; tuning workflow | [operations.md](operations.md) |
| Wire format / `/pitch` payload / WS messages / coordinate frames | [reference/protocols.md](reference/protocols.md) **(SoT)** |
| Algorithm registry, runnable IDs, cost-threshold ownership, bucket-key convention | [reference/algorithms.md](reference/algorithms.md) **(SoT)** |
| Empirical HSV / fill / aspect / residual baselines | [reference/tuning-baselines.md](reference/tuning-baselines.md) **(SoT)** |
| OpenCV hue convention, BT.601/709 colour-matrix gap, "should I align?" | [reference/hue-and-color.md](reference/hue-and-color.md) **(SoT)** |
| 240 fps capture format on a specific iPhone model; FOV constant | [reference/iphone-camera-formats.md](reference/iphone-camera-formats.md) **(SoT)** |
| Current LAN IP, device registry, chirp source | [snapshot/lan-deployment.md](snapshot/lan-deployment.md) ⚠ stale-prone |

## Buckets

```
docs/
├─ architecture.md         # System overview, dashboard control plane, detection paths
├─ ios.md                  # iOS state machine, capture, audio, modules
├─ server.md               # Server module index (schemas / detection / pipeline / routes)
├─ operations.md           # How to run / calibrate / tune / reprocess / degrade
├─ reference/              # SoT lookup tables — wire shapes, registries, baselines
│   ├─ protocols.md
│   ├─ algorithms.md
│   ├─ tuning-baselines.md
│   ├─ hue-and-color.md
│   └─ iphone-camera-formats.md
├─ snapshot/               # Point-in-time facts; stale-prone, re-verify before acting
│   └─ lan-deployment.md
└─ archive/                # Completed / abandoned planning docs (do not read for current behaviour)
```

**reference/** = SoT lookup — conflict with anything else, this wins.
**snapshot/** = time-stamped facts that decay. Each file states what to
re-verify and when.
**archive/** = past intent, not present state. Read for git-blame
archaeology only.

## Updating — change behaviour, change the doc

When code changes invalidate something documented here, update the
corresponding doc **in the same commit**. Stale docs are worse than no
docs — they actively mislead.

Mapping of code change → doc to touch:

| Code change | Doc(s) to update |
|---|---|
| New / removed FastAPI route | [server.md](server.md) + [reference/protocols.md](reference/protocols.md) |
| Wire field added to `FramePayload` / `PitchPayload` / WS frame | [reference/protocols.md](reference/protocols.md) + iOS-side `CameraCommandRouter` guard |
| New / removed entry in `server/algorithms/__init__.py` | [reference/algorithms.md](reference/algorithms.md) + [server.md](server.md) algorithm row + [reference/protocols.md](reference/protocols.md) error matrix |
| Change to `_MIN_FILL` / `_MIN_ASPECT` / selector weights / preset HSV | [reference/tuning-baselines.md](reference/tuning-baselines.md) |
| `_IPHONE_MAIN_CAM_HFOV_RAD` or any iPhone capture-format assumption | [reference/iphone-camera-formats.md](reference/iphone-camera-formats.md) |
| Preset / dual-active preset schema change | [server.md](server.md) + [operations.md](operations.md) + [reference/protocols.md](reference/protocols.md) |
| Detection algorithm internal change (no API delta) | [server.md](server.md) (and `ball_tracker/BallDetector.mm` header for lockstep) |
| New / removed iOS capture state | [ios.md](ios.md) |
| Dashboard UI restructure | [architecture.md](architecture.md) (control plane section) |
| Calibration / sync flow change | [operations.md](operations.md) |
| LAN IP / device / chirp source change | [snapshot/lan-deployment.md](snapshot/lan-deployment.md) + bump "Last verified" date |

If you find a doc already stale before changing code, **fix the doc first
in a separate commit**, then do the code change against an accurate
baseline.

## Archive

`archive/` holds completed / abandoned planning docs. Don't read them
to learn current behaviour — they describe past intent, not present
state.
