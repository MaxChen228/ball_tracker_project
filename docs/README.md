# docs/

Canonical reference for ball_tracker. **Read these before editing code.**
`CLAUDE.md` / `AGENTS.md` at the repo root are slim pointers — the
content lives here.

## When to read which

| You're touching… | Read |
|---|---|
| Anything, first time in a session | [architecture.md](architecture.md) |
| Swift / iOS app (state machine, capture, audio, modules) | [ios.md](ios.md) |
| `server/*.py` (schemas, detection, pipeline, state, routes) | [server.md](server.md) |
| Wire format, WS messages, coordinate frames, `/pitch` payload | [protocols.md](protocols.md) |
| Running the server, calibrating the rig, debugging a degraded session | [operations.md](operations.md) |
| 240 fps capture format selection on iPhones | [iphone_camera_formats.md](iphone_camera_formats.md) |

## Updating

These docs MUST stay aligned with the code. When you change behaviour
that's documented here, update the corresponding doc in the SAME
commit. Stale docs are worse than no docs — they actively mislead.

Specifically:
- New / removed FastAPI route → `server.md` + `protocols.md`
- New / removed iOS capture state → `ios.md`
- Wire field added to `FramePayload` / `PitchPayload` / WS frame → `protocols.md`
- Detection algorithm change → `server.md` (and check `ball_tracker/BallDetector.mm` header)
- Dashboard UI restructure → `architecture.md` (control plane section)
- Calibration / sync flow change → `operations.md`

## Archive

`archive/` holds completed / abandoned planning docs. Don't read them
to learn current behaviour — they describe past intent, not present
state. They're kept for git-blame archaeology and nothing else.
