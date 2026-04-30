# CLAUDE.md

Guidance for Claude Code (claude.ai/code) on this repository. **This file
is the single source of agent-facing knowledge** — no auto-memory, no
hidden private notes. If something cross-session is worth remembering,
it lives here or in [`docs/`](docs/).

## Read the docs first

Canonical reference for this codebase lives under [`docs/`](docs/). **Before
answering any question or editing any code, read the relevant doc(s).**
Outdated mental models from earlier sessions are a top source of broken
diffs here — the docs are the single source of truth.

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
doc **in the same commit**. Stale docs are worse than no docs. Mapping of
code → doc is in [`docs/README.md`](docs/README.md#updating).

If you discover a doc is already stale before changing code, fix the doc
first (separate commit), then do the code change against an accurate
baseline.

## No auto-memory in this project

- **Do not write to** `~/.claude/projects/<hash>/memory/`. The auto-memory
  system is disabled for this repo.
- All cross-session knowledge goes here (CLAUDE.md) or in `docs/`.
- Old memory has been archived at
  `~/.claude/projects/-Users-chenliangyu-Desktop-active-ball-tracker-project/memory.archived-2026-04-29/` —
  do not read, do not reference, do not restore. Content was migrated
  into this file on 2026-04-29.

---

# Critical agent rules

## Project scope — personal LAN tool

ball_tracker_project 是個人雙 iPhone 立體追蹤工具，跑在 LAN 內。

- **不需要** API auth / token、`/reset`/`/sessions/arm`/`/pitch` 不加保護
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

- 使用者在做研究 / 量化比較不同 pipeline。fallback 會讓使用者以為看到的
  是 X 的結果實際是 Y，污染所有判斷
- 預設行為要 explicit、單一來源；缺資料就明顯地空 / 報錯，不要悄悄換
- 真需要多來源 → 加 toggle 讓使用者主動選

例外（保留 backcompat 的情境）：明確要 ship 給外部 user、或正式 production
資料庫不能丟。實驗階段不算。

## Git / PR workflow

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

## Subagent — 一律 Opus

呼叫 `Agent` tool（不論 subagent_type 是 Explore / Plan /
general-purpose / claude-code-guide / 自訂 worker）**一律加** `model:
"opus"`。

- Haiku agent 摘要曾編造內容（把「cartesian 全洩漏」當結論回報，但實際
  server 端 `gap_threshold_m` / `cost_threshold` 在 emit 前已 reject 大量
  組合），導致主 agent 誤判
- 即使輕量 Explore quick 也用 Opus，不為省 token 換 Haiku
- 收到 subagent 摘要後仍要對關鍵結論 file:line 自行驗證
- 例外：使用者明說「用 Haiku/Sonnet 跑這個」

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
2. `git grep "case \"" ball_tracker/CameraViewController.swift` 看 iOS 接受哪些
3. 對照 server 還在 dispatch 但沒 WS push 的 command（找
   `state.commands_for_devices()` 的 caller）
4. 凡是「server 改變狀態 → device 應該動作」的端點，確認後面有
   `await device_ws.broadcast(...)` 或 `device_ws.send`

**症狀識別**：log 出現 `reports_received=[]` 加 source=A/B 完全沒 log，
幾乎一定是 iOS 沒收到 trigger，**不是** detection 失敗。

---

# Project state（current as of 2026-04-29）

## Architecture — Phase 1-5 shipped

2026-04-20 連續 ship Phase 1-5（PR #54-#60，merge 到 main）後 durable
架構：

**Calibration / sync / runtime tunables / preview 全部走 dashboard**：

- iOS 不再送 intrinsics / homography — `data/calibrations/<cam>.json`
  是權威；無 calibration → server 422
- HSV / chirp threshold / heartbeat interval 從 dashboard slider WS push
  到 iOS
- 時間校正（chirp 同步 + mutual sync）只走 dashboard，iOS 端 nav button
  已刪
- Live preview pipeline：iOS → server JPEG push → dashboard MJPEG 串流
- Auto-cal 重跑時若已有 calibration **保留既有 intrinsics 只更新
  homography**
- `parkCameraInStandby` 已移除；standby 永遠保持 capture session 60fps
  running

**保留的 iOS UI**：

- `CameraViewController` + monitor overlay
- 無 in-app calibration / settings VC（`AutoCalibrationViewController` /
  `IntrinsicsStore` / `Settings*Controller` 已全部刪除，Phase 6 清理已
  落地）；`AppSettingsStore` 從 `UserDefaults` 讀 `server_ip` /
  `camera_role` 作 bootstrap

**未做（不是 bug，是還沒排）**：

- N-view triangulation — 等第三台 iPhone

**WS 取代 heartbeat 已 ship**（PR `a66d5db`）：iOS 純 WS heartbeat，HTTP
`/heartbeat` 退役。

任何 operator-facing 新功能**預設加在 dashboard**，不要回頭加在 iOS UI；
iOS 端只剩 capture / detect / WS transport 三件事。

## 偵測路徑：live + server_post

兩條偵測路徑並存：

- `live`：iOS 端 HSV+CC+shape gate，WS 串流 frame，**永遠開**，每場
  armed session 必跑
- `server_post`：MOV 全錄上傳 + server HSV 偵測，每次都錄 MOV 但偵測**只在
  operator 從 events 列點 Run server** 才跑（`POST
  /sessions/{sid}/run_server_post`）

**已棄用術語**：「模式一 / 模式二」— 後者（`ios_post`）已刪，`live`
完全取代。使用者提「模式一/二」要主動釐清 — 多半是 live vs server_post。

「iOS-yes / server-no」delta **不要先動 HSV**，先看
`server/dry_run_live_vs_server.py` 量化、判斷是 BT.709 偏移還是 DCT 損失。

同幀比對時 **iOS 更接近 ground truth**（上游無損）。detection rate 跨
時間窗不可直接比（iOS live 從 arm 串到 disarm vs server_post 整 MOV）。
Timestamp 對齊精度約 ±1.67ms（MOV time_base 1/600），8ms pairing window
內可配對。

## iOS↔server alignment scorecard（PR #93）

User goal：iOS live = production，server detection = oracle / debug。
**三層**對齊：演算法 / 像素 / payload。

### ✅ Algorithm — closed

- iOS ROI tracking 拿掉（commit 407fc01），與 server 同走 stateless
  full-frame
- server MOG2+morph 拿掉（Phase-A），與 iOS 同走純
  HSV+CC+shape gate
- silent fallback `or frame` 修掉（PR #93），buffer 異常 raise 不靜默替換
- candidate selector 共用（iOS 送 candidates，server `_resolve_candidates`
  統一排序）

### ✅ Pixel — closed (operationally invisible)

iOS NV12→BGR via `COLOR_YUV2BGR_NV12` hardcode **BT.601**；server H.264
解碼 stream tag 為 **BT.709**（`colorspace=1`/`color_range=1`，每 MOV
驗）。2026-04-30 用 `server/chroma_alignment_check.py` 量化收尾：

**Synthetic（純矩陣數學）**：

- deep_blue 投影：Δh=0、Δs=0、Δv=-8 — **藍球 hue 完全不受矩陣選擇影響**，
  只 V 略低
- tennis_yellow_green：Δh=+1、Δs=+5、Δv=+11
- red_safety（飽和紅）：Δh=-3、Δs=+3、Δv=-10
- 結論：實際使用色 hue 偏移 ≤ 3 OpenCV units（不是當初估的 ~3-4）

**Empirical（真 session 球 ROI）s_55731532（網球）100 frames，
21×21 ROI**：

- Δh: mean +2.03、p50 +2、p95 |Δ|=3、max 11
- Δs: mean -4.98、p50 -5
- Δv: mean +5、p50 +5
- **Tennis preset gate-mask agreement: per-frame Jaccard mean=0.974
  p50=0.994 p95=1.000**
- 切到 BT.709 → 1.9% 現 in-gate 像素掉出、0.2% out-of-gate 像素加入
  → **detection 行為實質不變**

**結論**：6-8° 偏移估算過於保守。真實 ~4° (2 OpenCV units)，且
blue_ball/tennis preset 都有足夠 margin 吸收。**不修也沒事**，留著
BT.601 (iOS) + BT.709 (server) 不對齊是 acceptable。

**何時回頭看這條**：

- 引入新色 preset，preset 寬度 < 6 OpenCV units 時要重跑 chroma_alignment_check
  （e.g. 螢光黃單一 hue 範圍 25-30 太窄）
- iOS 換新 chip 或 OS major upgrade，capture pipeline NV12 細節可能變
- libswscale 升級可能讓 server-side 變 BT.601 → 立刻 100% 重疊但要驗

### How to apply（pixel layer）

- 只動 HSV preset 不動 capture stack：跑
  `uv run python server/chroma_alignment_check.py --synthetic` 看新色
  swatch Δh/Δs/Δv，預期 ≤ 3 OpenCV units
- 真機驗證：`uv run python server/chroma_alignment_check.py --session <sid>
  --preset <name>` 看 Jaccard，目標 mean ≥ 0.95
- 跨裝置 / 鏡頭切換時 hue 偏移突增：第一步重跑這支 tool，第二步看
  MOV stream tag (`av.open(...).streams.video[0].codec_context.colorspace`)
  確認沒有第三種 colorspace 偷偷出現

### ✅ Payload / wire — closed

- aspect/fill ship 進 WS frame payload（commit abfa422）
- `frames_live` candidate `{px,py,area,area_score,aspect,fill}`；server
  `_resolve_candidates` 蓋 cost
- HSV/shape_gate/selector_tuning frozen 進 PitchPayload + SessionResult
  （PR #93）— reprocess 用 frozen snapshot 重現原始 detection
- iOS 對 server-required WS 欄位拒 schema 漂（atomic-drop guard at handler
  head）
- WS settings push 12 欄位文件化（[docs/protocols.md](docs/protocols.md)，
  PR #93）

### How to apply

- 看到「live vs server_post 對不上」→ 不要先猜 HSV，先跑
  `server/dry_run_live_vs_server.py --session <sid>` 看 centroid Δ 量化
- 要 reproduce 舊 session detection → reprocess 預設行為已是用 disk 當下
  config（commit `0b300a4`，2026-04-29）。要凍結快照重現舊 detection 加
  `--use-frozen-snapshot`（讀 `pitch.*_used`）
- 改 wire schema → 同 commit 改 `docs/protocols.md` + iOS 端
  `CameraCommandRouter` guard
- 動 `state.py` 內共用 state → 用 public accessor（如
  `state.live_session_frozen_config(sid)`），不要戳 `state._xxx`

### 待辦（PR #93 NIT 清單）

詳見 PR #93 description「Outstanding NITs」段；主要漏掉的：

- 三條 silent-fallback regression test 補上
- `routes/pitch.py` 兩處 `state._processing` / `state._time_fn` 私屬性戳
- iOS LiveFrameDispatcherTests fixture aspect/fill 補

---

# Tuning baselines

## HSV / fill / shape gate（深藍球實測值）

現場用**深藍色硬球**（不是黃綠網球）。dashboard preset 是
`tennis` / `blue_ball`（**不是** `baseball`）。

### HSV 範圍（OpenCV 0-179 hue space）

`data/hsv_range.json` 預設 **h 105-112 / s 140-255 / v 40-255**。

- 2026-04-29 收緊：h 從 100-130 縮到 105-112 過濾背景藍
- **v_min 必須 ≥ 40**：球下半進陰影 V 會掉到 80 以下；v_min 抬高會讓近相機
  的球只剩高光環、mask 變扁、aspect gate 砍掉（s_cc0dcaa5 reprocess 對比為證）
- preset 註冊單一 source：`server/presets.py::PRESETS`。
  `render_dashboard_session.py` / `routes/settings.py` 都從這裡 import；
  `test_control.py` 只持有字串 assertion，不是 source of truth
- `detection.py` docstring 寫「default 是黃綠網球」是歷史 fallback，不要
  改

### Fill ratio 實測（不是理論值）

combined mask `hsv_mask AND fg_mask` 下實測 fill **0.63-0.70 中位 0.68**
（s_fcf73afa + s_03d533c4 26 個 fill_fail 幀）。理論完美圓 fill = π/4 ≈
0.785，但球側陰影 / 縫線 / HSV 邊緣失敗會挖掉 10-15%。

- `_MIN_FILL` 當前 **0.55**，實測下緣 0.63，留有 margin
- **不要**靠 morphology CLOSE 把 fill 推高到 0.7+ — 那是用複雜度掩蓋校準
  問題

### Shape gate

240 fps 下飛行中的球近乎完美圓（最多輕微橢圓含邊界彎曲）。

- 目前 code `_MIN_ASPECT = 0.70`（`server/detection.py:106`）；實測
  ≥ 0.75 也合理但要先量化才 bump（backlog）
- 若改用 `4πA/P²` circularity，起點 ≥ 0.8
- 不要為「吞運動模糊」放寬 — 模糊不是這場遊戲的問題
- 不要引入 HoughCircles

### How to apply

- 動 detection 門檻時以這份實測分布為基準，不要用理論值
- 偵測問題的真正痛點通常是 **iOS 端 candidate 給太少**（cam B 觀察到 92%
  frame 帶 0 candidate），不是 HSV 不對
- 改完 HSV 要重算舊 session 跑 `server/reprocess_sessions.py`
- 使用者**不會**主動換球色 — 不要建議「試試 tennis preset」

## Residual filter floor ~20cm

viewer 的 residual filter（ray-midpoint gap 過濾）在 ~20 cm 以下邊際效益
遞減，再往下開始砍掉真實軌跡點。

- server_post residual 中位 3-5 cm，但飛行中段因快門糊化 / 邊緣像素抖動
  自然有 10-30 cm 的點，是真實點不是雜訊
- 20 cm 砍完離群已乾淨，再緊就傷己
- 預設 / 建議下限保持 ~20 cm；要更嚴的離群檢測走 fit-residual（先
  ballistic LSQ → 砍 >3σ → 重 fit），不要把 residual cap 壓更低

---

# LAN deployment facts（2026-04）

- **Server**: 跑使用者 Mac，LAN IP `192.168.50.106`（2026-04-24 確認）。
  iPhone Settings → Server IP 設這個。使用者習慣用 terminal 跑
  `uv run uvicorn main:app --host 0.0.0.0 --port 8765`，stdout log 直接
  顯示在他 terminal
- **Device registry**: **Cam A + Cam B 雙機都在線**（2026-04-22 起）。
  mutual sync / stereo triangulation 是常態路徑
- **Chirp 播放**: **Mac 本機**（server `/chirp.wav` 直接 Mac 喇叭）。
  兩台 iPhone 接收聲音強度不對稱（距離 Mac 不同）→ sync 調參時要考慮非
  對稱 SNR
- **網路**: LAN 有偶發性斷線（heartbeat timeouts，指數退避 2s 升至最長
  32s）。iOS `PayloadUploadQueue` retry policy 會在網路恢復後自動補傳
  積壓的 pitch upload
- **device log 無法從 Mac CLI 拉**：Xcode 26 沒提供遠端 iPhone log 的 CLI。
  可行：(a) Console.app 看連接的 device、(b) code 加 `os.Logger` 讓使用者
  複製貼上

**How to apply**：

- 「events 沒顯示」→ 先 curl `/status` 看 `uploads_received` — 空陣列 ≠
  iOS bug，可能是網路斷線 retry 中
- sync 調參假設兩台 SNR 不對稱
- 加 iOS-side debug 優先用 `log.info/warning/error`（使用者能自己
  Console.app 看）

---

# Technical references

## OpenCV Hue 是 0-179（不是 0-360）

`cv2.cvtColor(..., COLOR_BGR2HSV)` 產出 uint8，Hue 只能到 255 但實際用
0-179（每格 2°）。S / V 是標準 0-255。

| 標準 0-360° | OpenCV 0-179 |
|---|---|
| 藍 210-250° | **105-125**（deep-blue） |
| 黃綠 50-110° | **25-55**（yellow-green tennis） |

**踩到的點**：dashboard DETECTION · HSV 卡片輸入 210 會被擋下「值必須
小於或等於 179」— 是 OpenCV 本身約束，不是 UI bug。`/detection/hsv`
端點 `State.set_hsv_range` 會 clip 到 `[0, 179]` for Hue / `[0, 255]`
for S/V。使用者講色相數字時先確認是 OpenCV 還是標準 0-360；UI / 圖片
編輯器的 0-360 要 ÷2 再存。

## iPhone camera / FOV

完整 dump 在 [docs/iphone_camera_formats.md](docs/iphone_camera_formats.md)。
關鍵：

- **240fps 只 2 個格式**：format[22] 1280x720 binned、format[39]
  1920x1080 binned。兩者都 binned（sensor 2×2 pixel binning）— 240fps
  不可能拿到 non-binned 銳利畫面
- **FOV 實測 73.828°**（horizontalFovRadians ≈ **1.2885 rad**）。1920x1080
  所有 16:9 格式 FOV 一致（除 format[36]/[37] 120fps 望遠裁切版 41.173°）
- 舊 65° fallback 已修：`server/calibration_auto.py:51`
  `_IPHONE_MAIN_CAM_HFOV_RAD = 1.2885`。歷史 65° 預設曾讓 fx 高估 14%
  （1920px 下 fx=1507 vs 真值 1278），recovered 深度低估 10-15%
- 寫新 fallback / default FOV → 用 `1.2885`，不要再 hardcode `1.1345`

## reprocess_sessions.py — HSV 改完重算舊 session

`server/reprocess_sessions.py` 是 offline 工具。對已存在的
`data/pitches/*.json` + 配對 `data/videos/session_<sid>_<cam>.mov` 重跑
`detect_pitch`，覆寫 pitch JSON 的 `frames_server_post`，A+B 齊就重新
三角化 + fit，覆寫 `data/results/session_<sid>.json`。`frames_live`
保留不動（iOS WS 串流的即時偵測，HSV 改動影響不了）。

```bash
cd server
uv run python reprocess_sessions.py --since today               # 今天
uv run python reprocess_sessions.py --since 2026-04-20
uv run python reprocess_sessions.py --session s_c8d36fe2 s_xxx  # 指定
uv run python reprocess_sessions.py --all                       # 全史
uv run python reprocess_sessions.py --since today --dry-run     # 算但不寫
```

預設 flip 為「用 disk 當下 config」（commit `0b300a4`，2026-04-29）。
**跑之前建議停 server** 避免檔案競爭。
