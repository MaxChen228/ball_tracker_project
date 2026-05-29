# CLAUDE.md

Agent rules for this repo. **All cross-session knowledge lives in
[`docs/`](docs/) or here** — no auto-memory, no hidden private notes.

## Read the docs first

Canonical reference lives under [`docs/`](docs/). **Before answering or
editing, read the relevant doc(s).** [`docs/README.md`](docs/README.md)
is the routing table (semantic intent → path, with **(SoT)** markers).

Quick pointer (full table is in `docs/README.md`):

| Intent | Doc |
|---|---|
| First time this session | [docs/architecture.md](docs/architecture.md) |
| Swift / `.mm` change | [docs/ios.md](docs/ios.md) |
| `server/*.py` change | [docs/server.md](docs/server.md) |
| Wire schema / endpoints | [docs/reference/protocols.md](docs/reference/protocols.md) **(SoT)** |
| Algorithm registry / bucket keys | [docs/reference/algorithms.md](docs/reference/algorithms.md) **(SoT)** |
| HSV / fill / aspect numbers | [docs/reference/tuning-baselines.md](docs/reference/tuning-baselines.md) **(SoT)** |
| OpenCV hue convention / BT.601-709 gap | [docs/reference/hue-and-color.md](docs/reference/hue-and-color.md) **(SoT)** |
| 240 fps formats / iPhone FOV constant | [docs/reference/iphone-camera-formats.md](docs/reference/iphone-camera-formats.md) **(SoT)** |
| Run / calibrate / reprocess / debug a session | [docs/operations.md](docs/operations.md) |
| Current LAN IP / device topology | [docs/snapshot/lan-deployment.md](docs/snapshot/lan-deployment.md) ⚠ stale-prone |

## Update docs when behaviour changes

Code change that invalidates a doc → update the doc **in the same
commit**. Stale docs are worse than no docs. Code→doc mapping table is
in [`docs/README.md`](docs/README.md#updating--change-behaviour-change-the-doc).

If a doc is already stale before your code change, fix the doc first
(separate commit), then do the code change against an accurate
baseline.

## No auto-memory in this project

- **Do not write to** `~/.claude/projects/<hash>/memory/`. Auto-memory
  is disabled for this repo.
- All cross-session knowledge → CLAUDE.md (agent rules) or `docs/`
  (everything else).
- Old memory archived at
  `~/.claude/projects/-Users-chenliangyu-Desktop-active-ball-tracker-project/memory.archived-2026-04-29/`.
  Do not read, reference, or restore. Content was migrated into this
  repo on 2026-04-29.

---

# Critical agent rules

## Project scope — personal LAN tool

ball_tracker_project 是個人雙 iPhone 立體追蹤工具，跑在 LAN 內。

- **不需要** API auth / token、`/reset` / `/sessions/arm` / `/pitch` 不加保護
- **不需要** rate limiting / DDoS 防護 / Prometheus / 多用戶並發
- 做技術債分析或 batch 規劃時，把「auth / metrics / observability platform」
  類議題**直接刪除**，不列入候選；資源集中在 race / data loss / UX /
  偵測準確度。

## Experimental phase — 禁止向後相容 / silent fallback

iOS = server lockstep 部署，沒有多版本 client、沒有歷史資料保鮮需求。

- 新增欄位 → **直接 required**，不給 default、不寫 `Optional`
- 改路徑 → 砍舊路徑，不留 `if old_path: ...` 分支
- WS / payload schema 變更 → 拒絕「沒有新欄位」的訊息（讓它報錯）
- 不寫「舊資料 reload 時 fallback 為 X」的 migration shim
- guard / null check 只保留 system-boundary 用，內部 invariant 一律 assert

**禁止 silent fallback**（`a or b`、`a if a else b`、三元 fallback）。
使用者在做研究 / 量化比較不同 pipeline；fallback 會讓他以為看到的是 X 結果
實際是 Y，污染所有判斷。預設行為要 explicit、單一來源；缺資料就明顯地空 /
報錯，不要悄悄換。真需要多來源 → 加 toggle 讓使用者主動選。

例外（保留 backcompat 的情境）：明確要 ship 給外部 user、或正式 production
資料庫不能丟。實驗階段不算。

## Git / PR workflow

**pre-push gate（雙人協作，clone 後跑一次）**：
`git config core.hooksPath scripts/hooks`。`scripts/hooks/pre-push`
依本次 push 改動智慧跑：`server/*.py` 改 → pytest；`*.swift` 改 →
`xcodebuild build-for-testing`（編譯 test target，補 simulator `build`
漏編 test 的洞）。繞過：`git push --no-verify`（全跳）或
`BALL_TRACKER_SKIP_IOS=1`（只跳慢的 iOS build）。目的：`main` 永遠保持
綠，不讓紅 CI 污染另一位協作者的基底。

**merge PR 後立刻 pull main**：`gh pr merge` 成功後同一輪工具呼叫鏈加
`git checkout main && git pull origin main`，不管 squash/merge/rebase。
不 pull 會基於過時 main 做後續判斷。

**背景 worker agent 必須跑到 PR**：用 `Agent` tool（`run_in_background:
true`, `isolation: "worktree"`）派 batch worker 時，prompt 結尾**必須**
加這段警告：

> 重要警告：你必須完整跑完到 `gh pr create` 並回報 `PR: <url>`。
> 如果你寫了改動但沒 commit/push/PR，worktree 會被自動清掉、所有
> 工作消失。請務必嚴格遵守工作流程的最後 commit + push + PR 步驟，
> 不要在中間停下等使用者確認。遇到 blocker 自己診斷自己修。

不加這段歷史上發生過：worker 寫完 code 停在 commit 前 → worktree
auto-cleanup → code 全消失。

## Subagent — 模型選擇

呼叫 `Agent` tool 時依任務複雜度選 model：

- **預設 Sonnet**：一般 Explore / Plan / worker 任務
- **複雜任務必用 Opus**：跨檔重構、深度 audit、需要強推理 / 長 context
  整合的任務
- **Haiku 可用但不可信**：Haiku 摘要曾編造內容（把「cartesian 全洩漏」
  當結論回報，但實際 server 端 `gap_threshold_m` / `cost_threshold` 在
  emit 前已 reject 大量組合），導致主 agent 誤判。輕量 lookup 可用，但
  關鍵結論一律 file:line 自行驗證
- 收到任何 subagent 摘要都要對關鍵結論自行驗證

## pytest 一律 background 跑

`cd server && uv run pytest` 跑完整套要 30+ 秒。任何 pytest 指令（含單一
test、`-q`、`-x`）一律加 `run_in_background: true`，不要前景等。完成後用
`tail` 或讀 output_file 取結果。

## iOS — 禁 xcodebuild test

- **可以**：`xcodebuild -project ball_tracker.xcodeproj -scheme ball_tracker
  -sdk iphonesimulator -destination 'platform=iOS Simulator,name=iPhone
  17,OS=26.4' build`
- **禁止**：`xcodebuild ... test`、`-only-testing:`、`swift test`
- 跑一次完整 iOS test suite 要好幾分鐘；使用者自己在 Xcode 用 ⌘U
- refactor 讓某 test file 編譯失敗 → 修到「能編譯」即可，不要跑該 test
  確認邏輯
- 例外：使用者明確要求「跑這個測試確認 X」

## Debug 順序

**Web UI 卡住先看 DevTools Console**。Dashboard / 網頁 UI「按鈕沒反應」
「狀態不更新」「需 reload 才動」 → **第一步永遠**請使用者開 DevTools
Console 截圖（F12 → Console），不要直接跳進 backend race / connection
pool / cache header / SSE plumbing。

- 一個 top-level JS ReferenceError（例如未替換的 `{PLATE_WORLD_JS}`
  template placeholder）會讓 IIFE abort，所有後續 `setInterval` /
  `addEventListener` / SSE 不會發生，但前面的 click handler 還是 work —
  症狀跟「後端 race」一模一樣。2026-04-22 為此推了 4 個錯誤方向 commit
- `node --check` 可 offline 驗證 JS 語法，但 top-level reference errors
  只能 runtime 觀察
- HTML 模板注入 inline JS → render 後餵 `fastapi TestClient` + regex
  抓 `<script>`，Python 驗證 placeholder 是否殘留是便宜保險

## WS-only 後 — command 派送稽核 checklist

PR `a66d5db` 退役 HTTP `/heartbeat`，改純 WS 後曾漏掉 `/sync/start` 的
`sync_run` push → 每次按 RUN MUTUAL SYNC 都靜默 timeout、`reports_received=[]`。

任何 retirement / transport 切換**必須跑這 4 步**：

1. `git grep '"type":\s*"' server/` 列所有現有 WS message types
2. `git grep "case \"" ball_tracker/CameraCommandRouter.swift` 看 iOS 接受哪些
3. 對照 server 還在 dispatch 但沒 WS push 的 command（找
   `state.commands_for_devices()` 的 caller）
4. 凡是「server 改變狀態 → device 應該動作」的端點，確認後面有
   `await device_ws.broadcast(...)` 或 `device_ws.send`
5. unknown WS mtype 現在會 raise ValueError + close socket — regression
   test 在 `server/test_device_ws_unknown_mtype.py` 鎖住。

**症狀識別**：log 出現 `reports_received=[]` 加 source=A/B 完全沒 log，
幾乎一定是 iOS 沒收到 trigger，**不是** detection 失敗。
