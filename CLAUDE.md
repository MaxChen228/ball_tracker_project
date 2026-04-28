# CLAUDE.md

Guidance for Claude Code (claude.ai/code) on this repository.

## Read the docs first

Canonical reference for this codebase lives under [`docs/`](docs/). **Before
answering any question or editing any code, read the relevant doc(s).**
Outdated mental models from earlier sessions are a top source of broken
diffs here — the docs are the single source of truth, this file is just
a pointer.

| Question / change scope | Doc |
|---|---|
| First time this session — what is this thing? | [docs/architecture.md](docs/architecture.md) |
| Touching `ball_tracker/*.swift` / `*.mm` | [docs/ios.md](docs/ios.md) |
| Touching `server/*.py` | [docs/server.md](docs/server.md) |
| Wire format / `/pitch` payload / WS messages / coordinate frames | [docs/protocols.md](docs/protocols.md) |
| Running, calibrating, debugging a degraded session | [docs/operations.md](docs/operations.md) |
| 240 fps capture format on a specific iPhone model | [docs/iphone_camera_formats.md](docs/iphone_camera_formats.md) |

[`docs/README.md`](docs/README.md) has the full index.

## Update docs when you change behaviour

When a code change invalidates something documented in `docs/`, update the
doc **in the same commit**. Stale docs are worse than no docs. The
mapping of code → doc is in [`docs/README.md`](docs/README.md#updating).

If you discover a doc is already stale before you start changing code,
fix the doc first (separate commit), then do the code change against an
accurate baseline.

## Critical agent rules

- **Do NOT run iOS tests via xcodebuild** (`xcodebuild ... test`). Build-only
  is fine (`xcodebuild ... build`). Operator runs tests in Xcode (`⌘U`).
- Memory system at `~/.claude/projects/-Users-chenliangyu-Desktop-active-ball-tracker-project/memory/`
  carries cross-session context (user preferences, project state). It's
  loaded automatically — read `MEMORY.md` for the index.
