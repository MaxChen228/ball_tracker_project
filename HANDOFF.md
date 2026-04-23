# Viewer / Dashboard 三管道獨立 — 交接文檔

**任務主軸**：把 `live` / `ios_post` / `server_post` 三個 detection pipeline 在
viewer、events 列表、dashboard 三處都做到**完全獨立顯示、互不影響**。起因是
`s_8bfec7fe`（live-only 單機 session）時使用者發現：

- viewer 的 `show` 按鈕只有 `svr` 跟 `iOS`，沒有 `live`
- 按 `svr` 卻把 live rays 也一起控制了
- events 列 / dashboard 把三種管道混在一起，live-only session 看起來像失敗
- 底部 strip 單機時只畫 1 軌，看不出另一台 cam 缺資料
- 視角沒把 Cam A diamond 包進來 → 畫面看起來像空的

---

## 狀態速覽

| Commit | Hash | 內容 |
|---|---|---|
| 1 | `9a741f6` | Split viewer into three fully-independent detection pipelines |
| 2 | `1c57a68` | Render viewer camera diamond + axis triad dynamically |
| 3 | **未 commit**（本交接一起打包） | Expose per-pipeline frame counts in events + dashboard |

測試狀態（在 server/ 下 `uv run pytest -q`）：**251 passed**（最後一次跑）

剩餘工作見下方「Pending」。

---

## 程式走讀 — 已完成部分

### Commit 1 — viewer 三管道獨立 (`9a741f6`)

**核心觀念**：三個 DetectionPath（對應 `schemas.DetectionPath` enum）是三條獨立
分析管道：

- `live` = iOS 端 live 偵測，透過 WS streaming（`pitch.frames_live`）
- `ios_post` = iOS 端 post-pass payload（`pitch.frames_on_device`）
- `server_post` = server 解碼 MOV 後跑 detection（`pitch.frames`）

之前的 bug：
1. `routes/viewer.py::_videos_for_session` 用 `pitch.frames_live` 當
   `pitch.frames` 空時的 fallback，讓 live-only session 的 frames 跑到 server
   strip 上。
2. `viewer_page.py::sourceVisibilityKey` 把 `live` → `server` 做 alias，於是
   UI 只有兩顆 pill，`live` 的可見性綁在 `server` 開關上。
3. `render_scene.py` subtitle 把所有 rays 混成一個 `N rays` 數字。

**改動**：

- `server/routes/viewer.py`
  - `_videos_for_session` 的 `frames_info` 改回三個獨立 stream dict；不做
    fallback、不做合併。每 cam row 的 payload 就是
    `{live: {…}, ios_post: {…}, server_post: {…}}`。

- `server/viewer_page.py`（大改）
  - `PATHS = ["live", "ios_post", "server_post"]` 常數。`PATH_LABEL` 給 UI 用。
  - `sourceToPath(source)`：reconstruct.py 的 `r.source` 字串仍是舊的
    `server` / `on_device` / `live`；這個函式是唯一一處翻譯。
  - `PATH_DASH` / `PATH_OPACITY` / `PATH_MARKER_SYMBOL` — 三路視覺語彙。
  - `framesByPath[path][cam] = {t_rel_s, detected, px, py}` 取代舊的
    `framesByCam` / `framesByCamOnDevice`。
  - `camsWithFramesByPath[path]` — 某 path 上有資料的 cam 列表。
  - `HAS_PATH` / `HAS_TRAJ_PATH` — 是否要顯示某 pill（全域判定，見 Pending B）。
  - `layerVisibility = {traj, camA, camB} × {live, ios_post, server_post}`，
    localStorage key 升到 `_v2`（schema 換了，無法 migrate）。
  - `buildDynamicTraces`：ray / ground_traces / 3D trajectories 全部按 path 獨立
    處理，顏色由 `colorForCamPath(cam, path)` 決定。
  - `drawVirtCanvas` 按 `PATH_ORDER = [live, ios_post, server_post]` 順序疊三個
    detection dot。
  - `jumpDetection(dir)` 只跳**可見** pipeline 的 detected frame。
  - `renderFrameLabel` 每 path 一段 `live A:12 ✓ B:— · post …`。
  - HTML 三顆 pill per layer + 三條獨立 strip-row + 多路徑 disclaimer。

- `server/render_scene.py`
  - subtitle 改成 `1 cam · 67 live / 12 post / 8 svr · 3 svr + 2 post 3D`。

- `server/test_viewer.py` + `server/test_server.py`
  - 更新 dual-mode E2E 斷言為新的 `data-path="…"` / 三個 canvas id。

### Commit 2 — camera diamond + 軸動態化 (`1c57a68`)

**bug**：render_scene.py 的 camera trace 帶 `meta.trace_kind="camera"`，被
`ViewerPageContext` 的 STATIC 過濾掉了，但 `buildDynamicTraces` 沒把它們加
回來。結果 viewer 3D 完全看不到 camera，而且 Plotly autoscale 只看 STATIC
（plate + world axes），camera 的 z=1.7m 根本在視角外 → rays 看起來像畫在
空氣中。

**改動**（都在 `server/viewer_page.py`）：

- `ViewerPageContext` 新增 `scene_theme_json` 欄位，從 `render_scene_theme`
  匯出 `_CAMERA_AXIS_LEN_M`、`_CAMERA_FORWARD_ARROW_M`、`_DEV`（右軸色）、
  `_INK_40`（上軸色）。
- `camMarkerTracesFor(c)` 產出 4 條 scatter3d trace：diamond + forward / right
  / up。forward 用該 cam 的色，right/up 用固定色。
- `cameraIsAnyPathVisible(camera_id)` — 三 pipeline pill 任一開就顯示相機。
- `buildDynamicTraces` 的最前面 for-loop 推進 cameras，確保 Plotly autoscale 會
  把 camera 座標一起納入 bounding box。
- 新 test `test_viewer_renders_camera_marker_dynamically_following_pipeline_pills`
  驗證 JS 含這兩個 function，且 STATIC 不含 `trace_kind="camera"` 的 trace。

### Commit 3 (WIP, 尚未單獨 commit) — events + dashboard 三欄

**bug**：state.py `events()` 只回 `n_ball_frames`（= server_post 的 count）跟
`n_ball_frames_on_device`（= ios_post）。live 沒地方放。
`path_status` 判定綁在 `result.paths_completed`，live-only 單機 session 因
無法 triangulate 所以永遠不會 `paths_completed`，UI 顯示 `-` 而不是 `done`。

**改動**：

- `server/state.py::events()`
  - 新增 `n_ball_frames_by_path: {live: {cam:count}, ios_post: {…}, server_post: {…}}`。
    三個 key 永遠存在，count=0 也保留；UI 才能畫三欄而不必猜。
  - 保留 `n_ball_frames` / `n_ball_frames_on_device` 當 alias（給舊 consumer），
    別刪，`test_events_endpoint_lists_sessions_latest_first` 有依賴。
  - `_path_status(path)` 新邏輯：
    1. `result.paths_completed` 有 → `done`
    2. 有任何 cam 的 detected 幀 > 0 → `done`（**這條是解 live-only 的關鍵**）
    3. `result.abort_reasons` 有對應 key → `error`
    4. 否則 `-`
  - snapshot tuple 從 7 元素變 6 元素（合併 on_device frame count 進統一 dict）。

- `server/render_dashboard_events.py::_render_events_body`
  - Chip 改成 `<span class="path-chip on|err|">L<span class="pc">67</span></span>`
    含 hover title 說明該 pipeline 是什麼 + 每 cam frame 數。
  - 三個 pipeline label 常數：`Live — …` / `POST — …` / `SVR — …`。

- `server/render_dashboard_style.py`
  - 新增 `.path-chip.err`（用 `--dev` 紅色）。
  - 新增 `.path-chip .pc`（count 後綴，字號降、左邊 divider）。

- `server/static/dashboard_client.js::renderEvents`
  - JS 端邏輯跟 `render_dashboard_events.py::_path_chip` lockstep（dashboard 有
    SSR + 5 秒 JS 刷新兩條路徑，兩邊必須產出同樣 DOM，否則刷新會閃）。

- `server/test_viewer.py`
  - 更新 `test_events_endpoint_lists_sessions_latest_first` 對 `n_ball_frames_by_path`
    + `path_status` 的斷言。
  - 新 test `test_events_path_status_marks_live_done_on_frame_existence_not_triangulation`
    ：鎖住「live-only 單機 session → path_status.live == done」的語意，並檢查
    dashboard HTML 有 `path-chip on` + `pc`=2 的 chip 輸出。

---

## Pending — 接手後要做的

### A. Strip 改雙軌（A/B）— 使用者最後一則明確要求，未實作

**使用者需求**：每條 pipeline 的 strip 應該 reserve A 跟 B **兩個子軌道**，
即使某台 cam 沒資料也畫空軌。以 `s_8bfec7fe` 為例：只有 Cam A，所以 LIVE strip
內 A 軌有 67 個 detection 方塊、B 軌整條空白。總共最多 3 strip × 2 軌 = 6 子軌。

**現況的問題**：
`server/viewer_page.py::drawStripInto` 目前接 `cams` 參數，用
`camsWithFramesByPath[path]`（= 該 path 有資料的 cam 列表）決定畫幾行。單機
session 只畫 1 行。

**實作提示**：

1. HTML（三處 strip-row 內）：`<canvas height="18">` 改 `height="28"`。
2. `render_dashboard_style.py` 的 CSS（其實是 `_viewer_css` 裡）找
   `.scrubber-wrap canvas { height: 18px; }`，給 strip-row 下的 canvas 一條
   更具體的 rule，height = 28px。可以考慮加一個 `.strip-sublabels` 子元件
   在 strip-label 跟 canvas 之間顯示垂直堆疊的 "A" / "B" 小字標，字號 8px。
3. JS `drawStripInto(canvas, strips, path)`（移除 `cams` 參數）：
   ```js
   const ALL_CAMS = ["A", "B"];
   const rows = ALL_CAMS.length;
   const rowH = Math.floor(H / rows);
   for (let ci = 0; ci < rows; ++ci) {
     const cam = ALL_CAMS[ci];
     const strip = strips[cam];            // 可能 undefined
     const y = ci * rowH;
     ctx.fillStyle = STRIP_EMPTY;
     ctx.fillRect(0, y, W, rowH);
     if (!strip) continue;                 // 沒資料的 cam 留空
     const muted = !isLayerVisible(`cam${cam}`, path);
     const detColor = muted ? STRIP_MUTED : colorForCamPath(cam, path);
     for (let x = 0; x < W; ++x) { ... }
   }
   ```
4. `renderDetectionStrip` 對應簡化，不再 pass `camsWithFramesByPath[path]`。
5. `resizeDetectionCanvas` 不動（只 resize 可見 strip）。
6. 可能要更新 `test_server.py::test_dashboard_drives_dual_mode_end_to_end` 或
   `test_viewer.py` 某個斷言若後者檢查 canvas 高度；目前沒看到。

### B. per-camera `HAS_PATH`（建議、非使用者要求）

**現況**：`HAS_PATH.live` 是全域判定 —— 只要 scene 裡有**任何** cam 的 live
ray，就 true。camA 跟 camB 的 `live` pill 都會顯示。
`s_8bfec7fe` 只有 camA 有 live，但 camB 的 live pill 也還是亮著、點了沒效果。

**修法**：`HAS_PATH_PER_CAM[camKey][path]`，`paintLayerPills` 讀這個；當某
cam 的某 path 沒資料，隱藏該 pill。

### C. 已知 flaky test（確認後再動）

`server/test_server.py::test_live_websocket_stream_pairs_frames_and_emits_events`
在完整 suite 中曾出現 `path_completed` WS event 缺 `cam="A"` 欄位而斷言失敗，
但單獨跑 pass；最後一次完整 suite 跑是 251/251 全綠。懷疑跟既有（pre-existing
dirty 的）`live_pairing.py` / `state.py` 變動有關，不在本次範疇。若 CI 失敗可
先單獨 rerun 驗證是否 flaky；如為真 flaky 再追。

---

## 本次 commit 打包說明

因為 working tree 裡除了我本輪的 5 個檔案外，還有**使用者前置 session 留下
的 dirty 檔案**（我沒碰）：

- `ball_tracker/CameraRecordingWorkflow.swift`
- `server/live_pairing.py`
- `server/main.py`
- `server/reconstruct.py`
- `server/state.py`（前面幾 hunks — `_live_frames_for_camera_locked`、
  `persist_live_frames`、`_record` 的 live-frame 合併邏輯。events() 那段是本輪）
- `server/test_control.py`

為了讓「轉機器 + pull」能一次拿到完整工作狀態，這次交接 commit **會同時
bundle**：

1. 本輪 Commit 3 的 events/dashboard 三欄改動
2. 以上既有 dirty 檔案（原封不動）
3. 這份 HANDOFF.md

新機器拿到後若要把 pre-existing dirty 還原成 dirty（讓 Commit 3 保持為一個
獨立 commit），執行：

```bash
git reset --soft HEAD~1
git reset HEAD -- ball_tracker/CameraRecordingWorkflow.swift server/live_pairing.py \
  server/main.py server/reconstruct.py server/test_control.py
git checkout HEAD -- <any file you want to drop>
# 或者保留整個 bundle commit 不拆，繼續往下做
```

---

## 轉機器步驟

**來源機（這台）**：

```bash
cd /Users/chenliangyu/Desktop/ball_tracker_project
git log --oneline -5          # 確認 HEAD 是剛打包的 commit
git push origin main
```

**目標機（新電腦）**：

```bash
cd <worktree>
git fetch origin
git pull --ff-only origin main
cat HANDOFF.md                # ← 讀這份
cd server && uv run pytest -q  # 預期 251 passed
```

---

## 快速 sanity check（新機器接手後先跑這幾個）

```bash
cd server
uv run pytest test_viewer.py -q
uv run pytest test_viewer.py::test_events_path_status_marks_live_done_on_frame_existence_not_triangulation -q
uv run pytest test_viewer.py::test_viewer_renders_camera_marker_dynamically_following_pipeline_pills -q

# 啟 server 肉眼確認：
uv run uvicorn main:app --host 0.0.0.0 --port 8765
# 開 http://localhost:8765/viewer/s_8bfec7fe
# 應該看到：subtitle "1 cam · 67 live"、Cam A diamond 有出現、底部 LIVE strip
# 唯一一條；pill 只有 RAYS A/B · live。
# 開 http://localhost:8765/
# 應該看到：s_8bfec7fe row 有 "L 67" chip (on)、"I" (-)、"S" (-)
```
