# Live Streaming Architecture — 完整重構計劃

> **Status**: Partially landed — core `live` path is in production (WS frame streaming via `live_pairing.py`, `state.ingest_live_frame`, iOS `LiveFrameDispatcher`, viewer 三管道獨立 UI). Remaining items in this doc (dashboard live trajectory overlay, latency budget auditing, full SSE broadcast) are not yet shipped.
> **Author**: Max0228 + Claude
> **Date**: 2026-04-21 (status updated 2026-04-23)
> **Scope**: iOS ↔ server ↔ dashboard 的偵測 / 配對 / 即時顯示管線重構
> **Target**: 丟球瞬間到 dashboard 看到軌跡點延遲 < 50 ms/point

---

## 1. 動機與現況

### 1.1 目前體驗
`Arm → 丟球 → 錄完 → MOV finalize (100-300ms) → 上傳 (1-5s) → post-pass detect (0.5-2s) → triangulate → Events 列表 5s polling → 看到軌跡`

端到端 3-8 秒才看到結果。即時感為零。

### 1.2 核心誤解澄清（修正前的認知錯誤）
- 「錄影與 HSV 計算已解耦」=== capture queue 不被 detection 拖慢 ≠ detection 能跑滿 240Hz
- 實際上 live detection 因 `detectionInFlight` serial gate 封頂 ~60-80 Hz
- `onDevice` / `cameraOnly` 主路徑都是 post-pass（事後重 decode MOV），不是 live
- `dual` 是「iOS post-pass + server post-pass 互相驗證」，不是 live vs post-pass

### 1.3 目標
1. 丟球過程中，dashboard 3D scene 點點浮現（<50ms per point）
2. 三個偵測路徑可**複選**（live / iOS post-pass / server post-pass），自由組合
3. Dashboard UX 從「批次結果展示」變「連續過程監控」
4. 不破壞現有 MOV 歸檔 / reprocess / viewer scrubber 等資產

---

## 2. 目標架構總覽

### 2.1 三條偵測路徑（正交可複選）

| 路徑 ID | 何時算 | 在哪算 | 幀率 | 延遲/點 | 精度 |
|---|---|---|---|---|---|
| `live` | capture 中 | iOS concurrent worker pool | ~240 Hz | <50 ms | BGRA 直出 |
| `ios_post` | 錄完 | iOS `LocalVideoAnalyzer` | 240 Hz | cycle + 0.5-2s | BGRA 直出 |
| `server_post` | 收到 MOV 後 | Server PyAV + OpenCV | 240 Hz | upload + 1-2s | H.264 decoded BGR |

### 2.2 通訊通道

| 方向 | 通道 | 負載 |
|---|---|---|
| iOS ↔ server | WebSocket `/ws/device/{cam}` | 即時 frames 上行 + arm/disarm/settings 下行 + 雙向 heartbeat |
| iOS → server | HTTP POST `/pitch` (multipart) | MOV + 最終 payload（保留，非即時路徑）|
| iOS → server | HTTP POST `/pitch_analysis` | iOS post-pass frames（保留）|
| server → dashboard | SSE `/stream` | 即時點、frame 計數、session 狀態、calibration 更新 |
| dashboard → server | HTTP POST (維持) | `/sessions/arm`、`/detection/hsv` 等控制命令 |

### 2.3 資料流（live streaming 路徑）

```
iOS captureOutput (240 fps)
  ├─ clipRecorder.append(sample)                  [不變，MOV 繼續錄]
  └─ concurrent detection pool (3 workers)
       └─ 每幀算完 → WebSocket.send(FramePayload)
                          ↓
         Server WS handler → state.ingest_live_frame(cam, sid, frame)
                          ↓
         _try_pair_and_triangulate(sid, cam, ts)
                          ↓
         若 A/B 都有 ts±8ms 的幀 → triangulate → append to live results
                          ↓
         SSE broadcast { type: "point", sid, x, y, z, t_rel }
                          ↓
         Dashboard EventSource → Plotly.extendTraces → 新點現身
```

### 2.4 session 生命週期

```
arm (POST /sessions/arm, paths={live, ios_post})
  → server mints sid, stores paths, broadcasts SSE "session_armed"
  → WS down to each cam: { "cmd": "arm", "sid": "s_xxx", "paths": [...] }
iOS 收到 arm → startRecording → captureOutput 邊錄邊送 frames
  ...（球飛行中，點點串流到 dashboard）...
dashboard 按 Stop OR 超時
  → server broadcasts WS "disarm"
  → iOS finalizes MOV → POST /pitch (multipart, 含最終 payload)
  → if paths 含 ios_post: iOS 跑 LocalVideoAnalyzer → POST /pitch_analysis
  → if paths 含 server_post: server 收到 MOV 觸發 detect_pitch
  → 所有路徑完成後 session 結果 finalized，SSE "session_ended"
```

---

## 3. Schema 變更

### 3.1 `server/schemas.py`

```python
# 新增：路徑識別
class DetectionPath(str, Enum):
    live = "live"
    ios_post = "ios_post"
    server_post = "server_post"

_DEFAULT_PATHS: frozenset[DetectionPath] = frozenset({DetectionPath.server_post})

# Session 改造
@dataclass
class Session:
    id: str
    armed_at: float
    max_duration_s: float
    paths: set[DetectionPath] = field(default_factory=lambda: set(_DEFAULT_PATHS))
    uploaded_cameras: list[str] = field(default_factory=list)
    # 保留 mode 欄位作 backward-compat 讀取（舊 JSON 載入時轉換），但不再寫入
    # 新程式碼一律用 paths

# SessionResult 擴充：多路徑結果並存
class SessionResult(BaseModel):
    session_id: str
    solved_at: float
    triangulated: list[TriangulatedPoint]                    # 主結果（authority 規則決定）
    triangulated_by_path: dict[str, list[TriangulatedPoint]] # key: DetectionPath value
    frame_counts_by_path: dict[str, dict[str, int]]          # {"live": {"A": 1142, "B": 1128}, ...}
    error: str | None = None
    paths_completed: set[str] = Field(default_factory=set)   # 哪些路徑已完成
    aborted: bool = False
    abort_reasons: dict[str, str] = Field(default_factory=dict)

# PitchPayload 維持不變（MOV 上傳用）
# 但 on-disk enriched JSON 擴充三組 frames：
class StoredPitch(PitchPayload):
    frames_live: list[FramePayload] | None = None
    frames_ios_post: list[FramePayload] | None = None
    frames_server_post: list[FramePayload] | None = None
    # 舊欄位 frames 保留為 "authoritative" 合併結果，向後相容
    frames: list[FramePayload] | None = None
```

**Authority 規則**（決定 `SessionResult.triangulated` 取哪組）：
1. 若 `ios_post` 有結果 → 用 ios_post
2. 否則若 `server_post` 有結果 → 用 server_post
3. 否則若 `live` 有結果 → 用 live
4. 都沒有 → `error="no detection completed"`

### 3.2 WebSocket 訊息 schema

**上行 iOS → server：**
```json
{ "type": "hello", "cam": "A", "session_id": null, "app_version": "..." }
{ "type": "heartbeat", "cam": "A", "t_session_s": 123.456 }
{ "type": "frame", "cam": "A", "sid": "s_abc", "i": 42, "ts": 123.456,
  "px": 912.3, "py": 550.1, "detected": true }
{ "type": "cycle_end", "cam": "A", "sid": "s_abc", "reason": "disarmed|timeout|error" }
```

**下行 server → iOS：**
```json
{ "type": "arm", "sid": "s_abc", "paths": ["live","ios_post"], "max_duration_s": 60 }
{ "type": "disarm", "sid": "s_abc" }
{ "type": "settings", "hsv_range": [25,55,90,255,90,255], "chirp_threshold": 0.18, ... }
{ "type": "sync_command", "mutual_sync_id": "sy_..." }
{ "type": "calibration_updated", "cam": "A" }   // 讓 iOS 拉最新校正
```

### 3.3 SSE server → dashboard schema

```json
{ "event": "session_armed",  "data": { "sid": "s_abc", "paths": [...], "armed_at": 1.23 } }
{ "event": "frame_count",    "data": { "sid": "s_abc", "cam": "A", "path": "live", "count": 1142 } }
{ "event": "point",          "data": { "sid": "s_abc", "path": "live", "x": 0.1, "y": 2.3, "z": 0.8, "t_rel_s": 0.42 } }
{ "event": "session_ended",  "data": { "sid": "s_abc", "paths_completed": [...] } }
{ "event": "path_completed", "data": { "sid": "s_abc", "path": "server_post", "point_count": 238 } }
{ "event": "device_status",  "data": { "cam": "A", "online": true, "ws_latency_ms": 12 } }
{ "event": "calibration_changed", "data": { "cam": "A" } }
```

---

## 4. iOS 端變更

### 4.1 新增檔案

| 檔案 | 用途 |
|---|---|
| `ball_tracker/ServerWebSocketConnection.swift` | `URLSessionWebSocketTask` 封裝；連線、重連、send/recv、訊息 dispatch |
| `ball_tracker/LiveFrameDispatcher.swift` | 從 detection queue 拿到 FramePayload → 送進 WS；附加背壓與掉線 buffer |
| `ball_tracker/ConcurrentDetectionPool.swift` | 管理 3 個並行 worker queue、pixelBuffer retain/release、frame index 單調性 |

### 4.2 變更檔案

**`ball_tracker/CameraViewController.swift`**
- 拿掉 `detectionInFlight` flag（檔案內搜 serial gate）
- `detectionQueue` 改為 concurrent（`DispatchQueue(label:attributes: .concurrent)`）；設 target worker 上限 3
- `dispatchDetectionIfDue` 改名為 `dispatchDetection`（無 gate）；每幀必然 dispatch
- 加保留 `CVPixelBufferRetain` 進 closure，結束 `CVPixelBufferRelease`
- 新增 `mode-aware dispatch`：`guard currentSessionPaths.contains(.live) || currentSessionPaths.contains(.iosPost) else { return }`（cameraOnly 不跑）
- `handleFinishedClip` 的 `switch mode` 改為 `paths` 判斷：
  - `paths.contains(.serverPost)` → 上傳 MOV 到 `/pitch`
  - `paths.contains(.iosPost)` → `persistAnalysisJob` 走 LocalVideoAnalyzer
  - `paths.contains(.live)` → cycle-end 時送 `cycle_end` WS 訊息
- `ServerHealthMonitor` 的 HTTP `/heartbeat` 保留但降級為 fallback（WS 斷時才用）

**`ball_tracker/ServerUploader.swift`**
- `CaptureMode` enum 標記 `@available(deprecated)`，新增 `CapturePathSet` 結構
- `PitchPayload` 加 `paths: [String]?` 欄位（snapshot 當下的 paths）
- WS 上行訊息建構函式集中在這裡

**`ball_tracker/SettingsViewController.swift`**
- 移除 capture mode 顯示（現在在 dashboard 由勾選控制）
- 僅保留 server IP/port、role、chirp threshold、heartbeat interval（WS fallback 用）

### 4.3 並行 detection 實作細節

```swift
// 3 worker concurrent queue
private let detectionQueue = DispatchQueue(
    label: "ball.tracker.detection",
    qos: .userInteractive,
    attributes: .concurrent
)
private let detectionSemaphore = DispatchSemaphore(value: 3)  // 上限 3 並行

func dispatchDetection(pixelBuffer: CVPixelBuffer, timestampS: TimeInterval) {
    guard detectionSemaphore.wait(timeout: .now()) == .success else {
        // 3 worker 全忙 → 丟這幀（罕見；正常 3*5ms=15ms 頂住 240Hz*3frames=12.5ms）
        metrics.liveFrameDropped += 1
        return
    }
    CVPixelBufferRetain(pixelBuffer)
    let callIndex = detectionCallIndex.increment()  // atomic
    detectionQueue.async { [weak self] in
        defer {
            CVPixelBufferRelease(pixelBuffer)
            self?.detectionSemaphore.signal()
        }
        guard let self else { return }
        let detection = BTBallDetector.detect(in: pixelBuffer)
        let frame = FramePayload(...)
        self.liveFrameDispatcher.enqueue(frame)  // 送 WS
        self.detectionStateLock.lock()
        if self.currentGeneration == self.detectionGeneration {
            self.detectionFramesBuffer.append(frame)  // 給 fallback 用
        }
        self.detectionStateLock.unlock()
    }
}
```

**關鍵決策：**
- `detectionCallIndex` 用 `OSAtomicIncrement64`（或 `ManagedAtomic`）保證單調；不是 queue 順序
- 每 worker 自己 retain pixelBuffer，避免 capture queue 重用導致撕裂
- `detectionFramesBuffer` 的 append 仍經 lock（rare contention，3 worker 都剛好同時 append 的機率極低）

### 4.4 WebSocket 連線模型

```swift
final class ServerWebSocketConnection {
    enum State { case disconnected, connecting, connected, reconnecting }

    // 連線：URLSessionWebSocketTask with URL ws://host:port/ws/device/{cam}
    // 收訊：recursive receive() on dedicated queue
    // 送訊：sendMessage(.string(json))；失敗 → enqueue 到 pending buffer
    // 重連：exp backoff 從 1s 倍增到 30s
    // 心跳：每 1s 送 heartbeat msg；連續 3s 沒收到 server pong → force reconnect
    // 掉線 buffer：pending frames 上限 500（超過開始 drop 最舊）
}
```

**掉線降級策略：**
- WS 斷線時，live frames 繼續累積在 `pending` buffer；重連後 flush 送出
- 若 cycle 結束時 WS 仍斷 → 該 cycle 的 live path 標記 aborted，依賴 post-pass 補救
- HTTP `/heartbeat` fallback：WS 斷超過 10s 自動啟用，讓 dashboard 仍能看見 device online

---

## 5. Server 端變更

### 5.1 新增檔案

| 檔案 | 用途 |
|---|---|
| `server/ws.py` | WebSocket endpoint + 連線管理器；訊息 dispatch 到 state |
| `server/sse.py` | SSE broadcast hub；dashboard 訂閱；背壓與斷線清理 |
| `server/live_pairing.py` | 即時 A/B frame pairing + triangulate；獨立於現有 `pairing.py`（後者仍負責批次 post-pass）|

### 5.2 變更檔案

**`server/main.py`**
- 新 endpoint：`/ws/device/{cam}` WebSocket handler
- 新 endpoint：`/stream` SSE (GET)
- `State` 加欄位：
  - `live_connections: dict[str, WebSocket]` — 每 cam 的 WS
  - `sse_clients: set[SSESubscription]` — 所有 dashboard 訂閱者
  - `live_results: dict[str, list[TriangulatedPoint]]` — per-session live 累積點
- `arm_session` 參數從 `mode: CaptureMode` 改成 `paths: set[DetectionPath]`
- `record(PitchPayload)` 分流：依 session.paths 決定要跑哪些事後管線
- 新方法：`ingest_live_frame(cam, sid, frame)` — WS handler 呼叫
- 新方法：`broadcast_sse(event_type, data)` — 任何 state 變化觸發
- `_try_pair_live(sid, cam, ts)` — 增量配對，O(log n) 用 bisect 找對岸相近 ts

**`server/pipeline.py`**
- `detect_pitch` 維持不動（server post-pass 仍用）
- 新增 `detect_pitch_async` 版本：一個大 MOV 分段並行 decode+detect（加速 server post-pass）— **optional，not in MVP**

**`server/pairing.py`**
- 抽出 `pair_frames_windowed(frames_a, frames_b, window_s=0.008)` 成獨立 helper
- `live_pairing.py` 復用同一 helper，只是吃 stream 而非 list

### 5.3 即時配對邏輯

```python
class LivePairingState:
    def __init__(self, sid: str):
        self.sid = sid
        # 每 cam 維持一個 sorted-by-ts buffer（實務用 deque + bisect）
        self.frames: dict[str, list[FramePayload]] = {"A": [], "B": []}
        self.triangulated: list[TriangulatedPoint] = []

    def ingest(self, cam: str, frame: FramePayload) -> list[TriangulatedPoint]:
        """Return newly triangulated points."""
        self.frames[cam].append(frame)
        if not frame.ball_detected:
            return []
        other = "B" if cam == "A" else "A"
        # 在對岸 buffer 找 ts±8ms 且 ball_detected 的 frame
        matches = _find_pairs_near(self.frames[other], frame.timestamp_s, 0.008)
        new_points = []
        for other_frame in matches:
            if not other_frame.ball_detected:
                continue
            if _already_paired(self.triangulated, frame, other_frame):
                continue
            pt = _triangulate_pair(frame, other_frame, self.calibrations)
            if pt:
                self.triangulated.append(pt)
                new_points.append(pt)
        return new_points
```

**決策點：**
- Buffer 大小限制：每 cam 最多保留 500 frames（>2s @ 240Hz）；超過從頭裁掉
- 已配對標記：用 `(a.frame_index, b.frame_index)` tuple set 防重複三角化
- ts 8ms 配對視窗維持與現有 `pairing.py` 一致

### 5.4 SSE broadcast hub

```python
class SSEHub:
    def __init__(self):
        self.clients: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> AsyncIterator[str]:
        q = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self.clients.add(q)
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            async with self._lock:
                self.clients.discard(q)

    async def broadcast(self, event: str, data: dict):
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        async with self._lock:
            dead = []
            for q in self.clients:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                self.clients.discard(q)  # 慢 client 丟掉
```

### 5.5 持久化擴充

`data/pitches/session_*.json` 擴充欄位 `frames_live / frames_ios_post / frames_server_post`。載入時向後相容：
```python
def load_pitch(path: Path) -> StoredPitch:
    raw = json.loads(path.read_text())
    # 舊檔只有 frames：視為 server_post（最常見）
    if "frames" in raw and "frames_server_post" not in raw:
        raw["frames_server_post"] = raw["frames"]
    return StoredPitch.model_validate(raw)
```

`data/results/s_*.json` 擴充 `triangulated_by_path`；舊檔 `triangulated` 欄位複製到 `triangulated_by_path["server_post"]`。

---

## 6. Dashboard UX 變更

### 6.1 整體佈局調整

**目前**：52px top nav | 440px sidebar (Devices / Session / Events) | 右 canvas

**新版**：52px top nav | 440px sidebar (**Active Session → Events → Devices**) | 右 canvas

Sidebar 順序改動理由：操作中最常看的是正在發生什麼事，Devices 是 pre-flight。

### 6.2 頂部狀態條

```
BALL_TRACKER · Devices 2/2 · Calibrated 2/2 · Session s_7f3a21 · Stream ●● 234/238fps · RTT 11ms
```

新增：
- `Stream ●●` 兩顆 LED（每 cam 一顆 WS 狀態燈）；green=connected, yellow=degraded, red=disconnected
- `234/238 fps` 兩個 cam 的 live detection fps
- `RTT Nms` 端到端延遲（capture ts → dashboard render ts 中位數）

### 6.3 Active Session Card（新元件）

```
┌─ ACTIVE · s_7f3a21 ────────── 00:02.14 [●REC] ┐
│                                                │
│ Paths: [L][i][-]              (live + iOS post)│
│                                                │
│ A ▂▃▅▇▇▆▇▅▃▂  238 fps · 1,142 frames · ●conn   │
│ B ▂▃▅▆▇▇▇▅▄▂  234 fps · 1,128 frames · ●conn   │
│                                                │
│ Live pairs   224 pts · last 12ms · 2.87m ± 0.04│
│                                                │
│ Post-pass    [ios] pending · [srv] not armed   │
│                                                │
│ [ Stop ]                       [ Reset trail ] │
└────────────────────────────────────────────────┘
```

元素：
- 標題列：sid + 持續時間（mm:ss.ff，10Hz 更新）+ 錄影中 pulse 指示
- Paths chips：勾選中的路徑點亮
- 每 cam 一行：
  - 迷你 sparkline（最近 60 秒每秒 fps，Canvas 2D 手繪）
  - 即時 fps（最近 1s 的 frame count）
  - 累計 frame count
  - WS 連線狀態點
- Live pairs 列：累計三角化點數 + 距最後一點的 ms 數 + 最近 20 點的 Z 均值±std
  - Last > 200ms 變紅背景警告
- Post-pass 列：顯示 ios/srv 兩個 path 的狀態（not_armed / pending / running / done / failed）
- 按鈕列：Stop（強制結束 session）+ Reset trail（清 3D scene 但不停 session，給連續投球用）

Session 結束後整張卡片 fade-out + slide 進下方 Events 列表，轉成一行靜態摘要。

### 6.4 Events 列表變更

**每行新增路徑完成標示：**
```
s_7f3a21 · 02:14  [L][i][s] 1,832 pts · 2.8m  [viewer]
s_7f3a20 · 02:10  [L][-][s]   945 pts · 2.7m  [viewer]
s_7f3a1f · 02:06  [-][-][s] aborted · no sync [viewer]
```

三個 chip 分別代表 live/iOS post/server post；完成點亮，未執行 `-`，失敗紅色 `!`。
hover chip 顯示該路徑的 point count + 耗時。

### 6.5 3D Scene 即時化

**現況**：session 結束才 `Plotly.react` 一次重繪整條軌跡

**新版**：
- Arm 時建立空 scatter3d trace for live path
- 每收到 SSE `point` event → `Plotly.extendTraces(scene, {x:[[pt.x]], y:[[pt.y]], z:[[pt.z]]}, [liveTraceIdx])`
- Trace marker 顏色用 `colorscale` 映射 `t_rel_s`（舊點褪色，新點亮）
- Session 結束後整條 trace 凍結變灰色，作為 history 層
- 下次 Arm 時視 `Clear trails` 按鈕狀態：預設保留最近 3 條歷史（20% opacity），再舊的清掉
- Canvas 邊框 CSS `animation: recording-pulse 1s ease-in-out infinite` 在 armed 期間啟用

**性能預期：**
- 240 Hz × 0.5s 飛行 = 120 點/session，Plotly extendTraces 延遲 <5ms/call，頂得住
- 超過 5 條歷史軌跡後 scene 上的 scatter3d 合併成一個 trace 不影響 FPS

### 6.6 偵測路徑複選 UI

**位置**：目前 mode 按鈕區（靠近 Detection/HSV 卡片）。

**新版**：
```
┌─ DETECTION PATHS ──────────────────────┐
│ ☑ Live stream       iOS → WS           │
│   └─ 即時軌跡，<50ms/point              │
│ ☐ iOS post-pass     on-device analyzer │
│   └─ 240Hz 全幀，cycle 後 0.5-2s        │
│ ☑ Server post-pass  PyAV + OpenCV      │
│   └─ 240Hz 全幀 + forensic 存檔         │
│                                         │
│ [ Apply ]  ← 變更套用到下一次 arm        │
└─────────────────────────────────────────┘
```

**行為：**
- 勾選狀態存 dashboard 全域（`state.default_paths`，POST `/detection/paths`）
- 每次 `/sessions/arm` snapshot 當下的 paths 到 session
- 全不勾時 Apply 按鈕置灰 + 提示「至少需勾選一項或啟用純歸檔模式」
- 勾選下方展開 helper 文字提示組合效果（e.g. 「Live + Server」= 即時看 + server 認證）
- 對 `Live`：需 WS 可用 → 勾選後 UI 旁邊顯示 `requires WebSocket connection` 灰字

### 6.7 新增 affordances

**鍵盤快捷鍵：**
- `Space` → Arm / Stop toggle
- `R` → Reset trail
- `C` → 開 calibration panel
- `/` → focus search（events filter，未來）

**音效：**
- Arm 成功短 tone（200Hz 50ms）
- Session end 成功 tone（400Hz 100ms）
- 連線降級 / 失敗 tone（150Hz 200ms）
- 全部 `<audio>` tag 靜音預設，Settings 開關啟用

**Ghost preview：**
- Arm 前 3D scene 保留上一球軌跡 20% opacity 當鏡頭對位參考
- `Clear trails` 按鈕一鍵清除

**降級 banner：**
```
┌─ ⚠ Live stream degraded ─────────────────────┐
│ Cam A WebSocket lost — falling back to post- │
│ pass. Next session latency will be 2-8s.     │
│ [ Retry connection ]  [ Dismiss ]            │
└──────────────────────────────────────────────┘
```
出現時機：任一 cam WS 斷 >10s OR live frame 未送達率 >20%。

### 6.8 Viewer 頁（/viewer/{session_id}）

**結論**：[render_scene.py:114-122](server/render_scene.py:114) 的 scrubber 已經完整，**不動**。

未來可加的（非本計劃範疇）：
- 路徑選擇下拉 `View: live / ios_post / server_post / union`，讓使用者切換看同一 session 不同路徑的三角化結果
- 待多路徑資料累積有實際比對需求再做

---

## 7. 降級、容錯、觀測

### 7.1 降級矩陣

| 失敗情境 | 對 live | 對 post-pass | 使用者感知 |
|---|---|---|---|
| iOS WS 斷線 | live 暫停，frames 暫存 | 不影響 | Banner 警告；cycle 結束後 post-pass 補救 |
| iOS 熱節流 | detection fps 下降 | 不影響 | 狀態條 fps 變色 |
| Server 重啟 | 連線重建，live buffer 失去 | MOV 檔在；可 reprocess | 目前 session 降級為 archive |
| 某路徑偵測為 0 球 | 該路徑產不出點 | 其他路徑不影響 | Events chip 該路徑 `!` |
| 無 time sync | 所有路徑都無法三角化 | 同上 | 現有 `error="no time sync"` 邏輯 |
| 無 calibration | 同上 | 同上 | 現有 422 邏輯 |

### 7.2 新增 metrics / logs

Server：
- `live_frame_ingest_rate`（per cam, per sec）
- `live_pair_rate`（per session, per sec）
- `ws_latency_p50_p95`（從 iOS ts 到 server receive ts）
- `sse_client_count`
- `sse_dropped_client_count`（背壓被踢出的 dashboard）

iOS：
- `detection_worker_occupancy`（0-3 即時使用中）
- `live_frame_dropped_count`（3 worker 全忙）
- `ws_reconnect_count`
- `ws_send_queue_depth`

### 7.3 觀測 UI

Dashboard 加一個可收合的 `Telemetry` panel（預設收合，按鈕展開）：
- 最近 60s 的 live fps / pair rate / WS latency 折線圖
- 最近 10 次 session 的 path completion 矩陣
- Errors 與 warnings 即時日誌（SSE `log` event）

---

## 8. 測試策略

### 8.1 單元測試

**server/**
- `test_live_pairing.py`：模擬 A/B 交錯 stream，驗配對正確性 + 無重複
- `test_sse_hub.py`：多訂閱者、慢 client 踢出、斷線清理
- `test_schema_migration.py`：舊 mode=camera_only JSON 載入轉成 paths={server_post}

**ball_tracker/**
- `ConcurrentDetectionPoolTests.swift`：3 worker 並行正確性 + semaphore 上限
- `ServerWebSocketConnectionTests.swift`：重連、pending buffer、背壓
- `LiveFrameDispatcherTests.swift`：順序、去重、掉線累積

### 8.2 整合測試

- 端到端：mock iOS WS client 送 stream → server → SSE 客戶端收到點
- 兩台 iOS 同時 stream，server 正確配對
- 多路徑複選情境：勾 live+server_post 時兩組結果都存到 disk

### 8.3 現場驗收

- **精度驗證**：同一段投球，同時跑 dual (ios_post + server_post)，確認結果差異 <5% IoU
- **即時性驗證**：操作員丟球同時盯 dashboard，碼錶量第一點浮現延遲
- **耐久驗證**：連續 arm/disarm 100 次，檢查 WS 連線穩定 + 無記憶體洩漏

---

## 9. PR 切分與執行順序

**原則**：每個 PR 獨立可驗證、可 revert、不破壞現有行為。

### PR-A：schema paths 遷移（無行為變更）
- `CaptureMode` enum + `DetectionPath` enum 並存
- `Session.mode` 改 `Session.paths`，同時保留 `mode` 欄位自動 derive
- Dashboard UI 暫不改（仍是 radio）
- 所有舊 test pass
- **風險**：低。純重構。
- **PR size**：~300 LoC。

### PR-B：iOS concurrent detection pool
- 拿掉 `detectionInFlight`，改 semaphore + concurrent queue
- 效果：live detection fps 從 60-80 → 220-240
- 現有 `onDevice` post-pass 主路徑不變（仍用 LocalVideoAnalyzer）
- Metric 輸出：新 fps 進 HUD
- **風險**：中。pixelBuffer 生命週期、並行 append buffer 需仔細處理。
- **PR size**：~200 LoC + tests。

### PR-C：WebSocket transport（替代 heartbeat 命令通道）
- Server `/ws/device/{cam}` endpoint，只負責 arm/disarm/settings 命令下行 + heartbeat 雙向
- iOS `ServerWebSocketConnection`，與 `ServerHealthMonitor` 並存
- 優先 WS；WS 斷 10s 以上才 fallback HTTP heartbeat
- 命令延遲從 ~1s → <50ms
- **風險**：中高。iOS WS 背景/前景切換的生命週期需測試。
- **PR size**：~500 LoC。

### PR-D：Live frame streaming（建立在 PR-B + PR-C 上）
- iOS 送 `frame` 訊息過 WS
- Server 新增 `live_pairing.py` + `ingest_live_frame`
- 增量 triangulate 累積到 `State.live_results`
- 尚未 push 給 dashboard（下個 PR）
- **風險**：中。配對去重邏輯要嚴謹。
- **PR size**：~400 LoC。

### PR-E：SSE + dashboard Active Session card
- Server `/stream` SSE endpoint + `SSEHub`
- Dashboard EventSource 接收 + Active Session Card SSR/動態更新 + 3D scene extendTraces
- Events 列表加 path chips
- **風險**：中。Plotly extendTraces 效能需驗。
- **PR size**：~700 LoC（大量前端）。

### PR-F：Paths 複選 UI + 後端路由
- Dashboard radio → checkbox
- `/detection/paths` endpoint
- `/sessions/arm` 接 `paths: list[str]`
- Server 根據 session.paths 決定跑哪些管線
- Backward compat：舊 arm 呼叫（帶 mode）仍能用
- **風險**：低。前端工作為主。
- **PR size**：~400 LoC。

### PR-G：多路徑結果持久化 + 優先權 authority
- `SessionResult.triangulated_by_path`
- Pitch JSON 三組 frames 欄位
- Viewer 未動
- **風險**：低。
- **PR size**：~300 LoC。

### PR-H：Polish（可拆多個小 PR）
- 鍵盤快捷鍵、音效、ghost preview、降級 banner
- Telemetry panel
- **風險**：極低。
- **PR size**：~500 LoC 分 3 個 PR。

### PR-I（選配）：HTTP heartbeat 退場
- WS 穩定 1-2 週後
- 刪 `/heartbeat` endpoint + `ServerHealthMonitor` HTTP fallback
- **PR size**：~200 LoC 清理。

### 時程預估

假設單人每天 4h 實作：

| PR | 工期 | 累計 |
|---|---|---|
| A | 0.5 天 | 0.5 |
| B | 1 天 | 1.5 |
| C | 2 天 | 3.5 |
| D | 1.5 天 | 5 |
| E | 2 天 | 7 |
| F | 1 天 | 8 |
| G | 0.5 天 | 8.5 |
| H | 1.5 天 | 10 |
| I | 0.5 天 | 10.5 |

**MVP cutoff = PR-E 完成**（即時串流看得到點）≈ 7 工作天。
其後 PR-F/G/H 是完整度與 polish。

---

## 10. 風險與未決議題

### 10.1 技術風險

1. **iOS WS 背景化**：App 進背景時 `URLSessionWebSocketTask` 會被系統掛起。錄影 session 通常 app 在前景，問題不大；但長時間 armed idle 需 test。
2. **A17 熱節流**：3 worker 並行偵測 + 240fps capture + WS 連續送可能讓 iPhone 發熱。長錄 >30s 需實測 thermal state API。
3. **Plotly extendTraces 在長 session 下記憶體成長**：120 pts/session × 多 session 累積 → 需實作 trace 合併或 ring buffer 修剪。
4. **WS 掉包後 live path 的資料完整性**：中間斷幾秒的 frames 是否補送？決策：不補，該 cycle 的 live path 標記有 gap，最終依 post-pass 補全。
5. **SSE client 背壓**：dashboard 多開幾個視窗 + 慢 client 可能拖慢 server。決策：hub 踢慢 client，重新連即可。

### 10.2 產品決策未定

1. **Archive-only 模式（全不勾）是否允許 arm**？
   - 傾向允許，但需狀態條明確警告 no trajectory。
2. **Reset trail 是否清 server 端累積結果**？
   - 傾向 UI-only（dashboard 清 scene，server 端資料仍保留到 session end）。
3. **多路徑結果在 viewer 要不要強制同屏對照**？
   - MVP 不做，先存起來。實際比對需求出現再加 UI。
4. **鍵盤 `Space` = Arm 是否安全**？
   - 操作員手上握球可能誤觸；考慮長按 0.5s 觸發而非點按。

### 10.3 棄案

- ~~讓 dashboard 也用 WebSocket 雙向~~：SSE 已足夠（dashboard 不需上行），WS 增加複雜度無對應收益。
- ~~iOS 端也跑增量 triangulate~~：單機沒對岸資料，沒意義。
- ~~把 cameraOnly / onDevice / dual mode 三按鈕保留作 preset~~：paths 複選已完整涵蓋所有組合，preset 反而造成認知混亂。

---

## 11. 成功指標

PR-E ship 後一週內量測：

- **第一點延遲 p50 < 50ms**（chirp anchor → dashboard render）
- **Live detection fps p50 > 220**（兩台 cam 平均）
- **WS 連線可用率 > 99%**（單台 cam 每小時統計）
- **Session 完整完成率 > 95%**（arm 到 session_ended 無異常）
- **操作員主觀評分**：「軌跡即時浮現」vs 舊批次模式，>80% 偏好新版

---

## 12. 附錄

### 12.1 既有資產不動清單

- `/viewer/{session_id}` 頁與 scrubber
- `reprocess_sessions.py`
- `data/pitches/`, `data/videos/`, `data/results/` 檔案結構
- Calibration 管線（ArUco auto-cal、/calibration endpoint）
- Chirp sync 管線（`AudioChirpDetector`, `AudioSyncDetector`）
- HSV 設定熱推送
- `ClipRecorder` 寫 MOV

### 12.2 命名約定

- 新 path id 一律 lowercase snake：`live`, `ios_post`, `server_post`
- WS 訊息 type 一律 lowercase snake
- SSE event name 一律 lowercase snake
- 舊 `CaptureMode` 命名保留到 PR-I 才移除

### 12.3 參考檔案位置

- iOS 主要：[CameraViewController.swift](ball_tracker/CameraViewController.swift), [ServerUploader.swift](ball_tracker/ServerUploader.swift), [LocalVideoAnalyzer.swift](ball_tracker/LocalVideoAnalyzer.swift)
- Server 主要：[main.py](server/main.py), [schemas.py](server/schemas.py), [pipeline.py](server/pipeline.py), [pairing.py](server/pairing.py)
- Dashboard：[render_dashboard.py](server/render_dashboard.py), [render_scene.py](server/render_scene.py)

---

**審閱 checkpoint**：操作員確認此計劃前，不開 PR-A。
