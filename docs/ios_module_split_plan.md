# iOS 模組拆分計劃 — CameraViewController 瘦身

> **Status**: Plan (pre-implementation)
> **Date**: 2026-04-21
> **Scope**: 把 `ball_tracker/CameraViewController.swift`（2816 行）的非 UI 職責拆出獨立檔案
> **Goal**: 讓每塊邏輯可單獨寫單元測試，且主 controller 只負責 AVFoundation 生命週期 + UI 狀態

---

## 1. 現況與動機

### 1.1 CameraViewController 當前負擔

`CameraViewController.swift` 2816 行裡塞了這些正交職責：

| 職責 | 主要函式 / 屬性 | 行數估 |
|---|---|---|
| AVFoundation 生命週期（session/format/device I/O） | `setupPreviewAndCapture`, `configureCaptureFormat`, `startCapture`, `stopCapture` | ~400 |
| UI 狀態機（standby/timeSyncWaiting/recording/uploading）+ HUD | `enterRecordingMode`, `exitRecordingToStandby`, `updateUIForState` | ~200 |
| 並行 detection 池（concurrent queue + semaphore(3)）| `dispatchDetection`, `detectionQueue`, `detectionSemaphore` | ~80 |
| WebSocket 傳輸層（connect/reconnect/send/receive/dispatch） | `connectWebSocket`, `sendWebSocketJSON`, `handleWebSocketText`, `receiveNextWebSocketMessage` | ~180 |
| Live frame 上行（從 detection 結果包成 WS message 送出）| `dispatchDetection` 尾段 | ~30 |
| 時間同步協調（手動 + 遠端 + mutual）| `startTimeSync`, `beginTimeSync`, `completeTimeSync` | ~200 |
| 校正 frame 上傳 | `uploadCalibrationFrame` | ~50 |
| Pitch cycle 路由（cameraOnly/onDevice/dual 三路）| `handleFinishedClip`, `handleCameraOnlyCycle`, `persistAnalysisJob` | ~200 |
| 其他（曝光、FOV、intrinsics、settings 套用）| 雜項 | ~400 |

### 1.2 痛點

- **單元測試不可寫**：想測 WS 重連、detection semaphore、live frame dispatcher 都被迫整台 `UIViewController` + `AVCaptureSession` 吊起來，SPM / XCTest 做不到
- **閱讀成本爆表**：2816 行單一類別，新入的人要花半天才能建立 mental model
- **變更風險耦合**：改 WS 重連邏輯可能踩到時間同步，改時間同步可能撞到 detection pool
- **live streaming 架構計劃 §8.1** 已明確要求這三個檔案拆出來做單元測試：
  - `ConcurrentDetectionPoolTests.swift`
  - `ServerWebSocketConnectionTests.swift`
  - `LiveFrameDispatcherTests.swift`

### 1.3 目標

1. 抽出 **3 個純邏輯模組**到獨立檔案，各自可在 XCTest 中直接 init + 驅動
2. 保留 `CameraViewController` 作為**協調者**，只負責 AVFoundation / UI / 串接
3. 不破壞現有行為 — 每個 PR 綠色通過全測試 + iOS build 成功 + 手動 smoke test
4. 每個新檔案 **自帶單元測試**

---

## 2. 拆分目標三個模組

### 2.1 `ConcurrentDetectionPool.swift`

**職責**：接收 `CVPixelBuffer` + timestamp，排給並行 worker 跑 `BTBallDetector.detect`，產出 `FramePayload`；保證 pixelBuffer 生命週期正確、frame index 單調、concurrency 上限。

**公開介面：**
```swift
final class ConcurrentDetectionPool {
    /// Maximum number of in-flight detection dispatches. Capture frames
    /// that arrive while the pool is saturated are dropped silently
    /// (metric counter only).
    let maxConcurrency: Int

    /// Published when a detection worker produces a new FramePayload.
    /// Called on the detection queue — callers must hop to their own
    /// queue if they touch shared state.
    var onFrame: ((ServerUploader.FramePayload) -> Void)?

    /// Counter of frames dropped because all workers were busy. Cleared
    /// only on explicit reset(). Useful for the telemetry panel.
    var droppedFrameCount: Int { get }

    init(maxConcurrency: Int = 3)

    /// Non-blocking dispatch. Returns false if dropped (pool saturated).
    func enqueue(pixelBuffer: CVPixelBuffer, timestampS: TimeInterval) -> Bool

    /// Bump generation — workers in flight will discard their results
    /// instead of emitting `onFrame`. Use at cycle boundaries to prevent
    /// stale detections from bleeding into the next cycle.
    func invalidateGeneration()

    /// Reset counters; does not drain in-flight work.
    func reset()
}
```

**內部實作**：
- `DispatchQueue(label: "...", qos: .userInteractive, attributes: .concurrent)`
- `DispatchSemaphore(value: maxConcurrency)`
- `Unmanaged.passRetained(pixelBuffer)` → worker 內 `takeUnretainedValue` + `release`
- `os_unfair_lock` 保護 `callIndex` / `generation` / `droppedFrameCount`
- Worker 跑完前做 `guard generation == self.currentGeneration else { return }`

**從哪裡搬來**：
- `CameraViewController.swift:166-190`（detectionQueue / detectionSemaphore / detectionStateLock / generation / callIndex 四個屬性）
- `CameraViewController.swift:2672-2718`（`dispatchDetection` 整個函式）
- `CameraViewController.swift:2720-2735`（`resetBallDetectionState` — 變成 `invalidateGeneration` + `reset`）

**調用方改成**：
```swift
// In CameraViewController
private let detectionPool = ConcurrentDetectionPool(maxConcurrency: 3)

override func viewDidLoad() {
    detectionPool.onFrame = { [weak self] frame in
        self?.handleDetectedFrame(frame)
    }
}

func captureOutput(...) {
    let ok = detectionPool.enqueue(pixelBuffer: pb, timestampS: ts)
    // `ok == false` 時 telemetry 會記到 droppedFrameCount
}
```

### 2.2 `ServerWebSocketConnection.swift`

**職責**：`URLSessionWebSocketTask` 的完整封裝；connect / disconnect / send / receive / 自動重連 / 訊息 dispatch 給外部 handler。**不碰業務邏輯** — arm / disarm / settings 只是把 payload 傳給 delegate。

**公開介面：**
```swift
protocol ServerWebSocketDelegate: AnyObject {
    /// Called on the main queue.
    func webSocketDidConnect(_ connection: ServerWebSocketConnection)
    func webSocketDidDisconnect(_ connection: ServerWebSocketConnection, reason: String?)
    func webSocket(_ connection: ServerWebSocketConnection, didReceive message: [String: Any])
}

final class ServerWebSocketConnection {
    enum State { case disconnected, connecting, connected, reconnecting }

    weak var delegate: ServerWebSocketDelegate?
    private(set) var state: State = .disconnected
    private(set) var reconnectAttempt: Int = 0

    init(baseURL: URL, cameraId: String)

    /// Start (or re-start) the connection. Idempotent — safe to call
    /// from a foreground-reenter hook.
    func connect(initialHello: [String: Any]?)

    /// Cleanly close. Cancels any pending reconnect.
    func disconnect()

    /// Send a JSON-serialisable dict. If not currently connected, the
    /// message is dropped and a metric tick is recorded (do NOT queue —
    /// live frames arriving during a disconnect are stale by the time
    /// reconnect happens, so we lose less by dropping).
    func send(_ payload: [String: Any])

    /// Change base URL / camera id without tearing down a live socket
    /// unless the URL actually changed.
    func reconfigure(baseURL: URL, cameraId: String)
}
```

**內部實作**：
- 專屬 `DispatchQueue(label: "camera.websocket.queue", qos: .utility)`
- Exp backoff：1s → 2s → 4s → 8s → cap 30s；success 歸零
- Receive loop 用 recursive `task.receive {}` pattern
- 所有 delegate callback 都 `DispatchQueue.main.async` 確保 UI 安全
- 收到任何 "type": "..." 就打包成 `[String: Any]` 給 delegate；parse 交給 CameraViewController

**從哪裡搬來**：
- `CameraViewController.swift:218-219`（`webSocketTask` / `webSocketQueue`）
- `CameraViewController.swift:1458-1590`（整塊 WS: connect / disconnect / reconnect / receive / handle / send）

**調用方改成**：
```swift
// In CameraViewController
private lazy var ws = ServerWebSocketConnection(
    baseURL: settings.serverURL,
    cameraId: settings.cameraRole
)

override func viewDidLoad() {
    ws.delegate = self
    ws.connect(initialHello: ["type": "hello", "cam": settings.cameraRole, ...])
}

extension CameraViewController: ServerWebSocketDelegate {
    func webSocket(_ c: ServerWebSocketConnection, didReceive msg: [String: Any]) {
        guard let type = msg["type"] as? String else { return }
        switch type {
        case "arm":    applyRemoteArm(payload: msg)
        case "disarm": applyRemoteDisarm()
        case "settings": applySettingsPush(msg)
        case "sync_command": applyRemoteSyncCommand(msg)
        case "calibration_updated": refreshPeerCalibration(msg)
        default: break
        }
    }
}
```

**延伸：未來 `sync_command` + `calibration_updated` 兩個下行訊息類型的處理**也會走這個 switch，不會再污染 connection 本身。

### 2.3 `LiveFrameDispatcher.swift`

**職責**：`ConcurrentDetectionPool` 產出 `FramePayload` 時，負責把它包成 WS 訊息送出去；處理 back-pressure（WS send buffer 滿時的策略）、停火條件（非 live path 時不送）。

**公開介面：**
```swift
final class LiveFrameDispatcher {
    /// Inject the WS connection + session-state provider (so the
    /// dispatcher doesn't need to own these).
    init(
        connection: ServerWebSocketConnection,
        currentSessionId: @escaping () -> String?,
        currentPaths: @escaping () -> Set<ServerUploader.DetectionPath>,
        cameraId: String
    )

    /// Non-blocking. Drops the frame silently if:
    ///  - `.live` not in current paths (nothing to stream)
    ///  - sessionId is nil (no armed session)
    ///  - WS not connected
    /// Metrics counter increments so the UI / telemetry can surface it.
    func dispatch(_ frame: ServerUploader.FramePayload)

    /// Frames dropped for "no live path" / "no session" / "ws down"
    /// reasons, classified so the telemetry panel can display them
    /// separately.
    var dropCounters: (notLive: Int, noSession: Int, wsDown: Int) { get }

    func resetCounters()
}
```

**內部實作**：
- 純邏輯，無 queue，無 timer
- 每次 `dispatch` 先三個 guard 短路，各自 bump 對應 counter
- 通過後調 `connection.send([...])`

**從哪裡搬來**：
- `CameraViewController.swift:2704-2716`（`sendWebSocketJSON([...])` live frame 那塊）

**調用方改成**：
```swift
// In CameraViewController
private lazy var frameDispatcher = LiveFrameDispatcher(
    connection: ws,
    currentSessionId: { [weak self] in self?.currentSessionId },
    currentPaths: { [weak self] in self?.currentSessionPaths ?? [] },
    cameraId: settings.cameraRole
)

private func handleDetectedFrame(_ frame: ServerUploader.FramePayload) {
    // Append to buffer (for fallback / post-pass path)
    detectionFramesBuffer.append(frame)  // 仍在 CameraViewController
    // Live streaming if enabled
    frameDispatcher.dispatch(frame)
}
```

---

## 3. 拆分後的 CameraViewController 體質

拆完之後，`CameraViewController` 會變成：

```swift
final class CameraViewController: UIViewController {
    // --- Dependencies (injected or lazy) ---
    private let detectionPool = ConcurrentDetectionPool(maxConcurrency: 3)
    private lazy var ws = ServerWebSocketConnection(...)
    private lazy var frameDispatcher = LiveFrameDispatcher(...)
    // 既有：recorder, clipRecorder, healthMonitor, chirpDetector, uploader...

    // --- UI state machine --- (ca. 200 lines)
    // --- AVFoundation lifecycle --- (ca. 400 lines)
    // --- Cycle routing (handleFinishedClip + 三路 helper) --- (ca. 200 lines)
    // --- Time sync coordinator --- (ca. 200 lines, 下期再拆)
    // --- Settings apply --- (ca. 100 lines)
}

extension CameraViewController: ServerWebSocketDelegate { ... }
extension CameraViewController: AVCaptureVideoDataOutputSampleBufferDelegate { ... }
```

從 **2816 行 → 估 ~1500 行**，減半。主要 AVFoundation + UI 職責仍在。

---

## 4. PR 切分

四個 PR，每個獨立可驗證。

### PR-iOS-1：`ConcurrentDetectionPool.swift`（最低風險）

**動作：**
- 新增 `ball_tracker/ConcurrentDetectionPool.swift`
- 把 `dispatchDetection` + `resetBallDetectionState` 的 queue/semaphore/lock/gen/callIndex 邏輯完整搬進去
- 公開 `onFrame` callback
- `CameraViewController.captureOutput` 改呼叫 `detectionPool.enqueue(...)`
- `handleDetectedFrame(frame)` 接 callback，裡面做既有兩件事：append buffer + 送 WS（後者在 PR-iOS-3 才改）

**測試（新檔 `ball_trackerTests/ConcurrentDetectionPoolTests.swift`）：**
- `testDispatchUnderConcurrencyLimitFiresAllFrames` — 送 10 幀，全部 callback 觸發
- `testDispatchOverConcurrencyLimitDropsExcess` — 故意讓 worker 睡眠，第 4 個起被 drop
- `testInvalidateGenerationSilencesInFlightWorkers` — 送 3 幀 + invalidate，callback 只觸發 invalidate 前的幀
- `testFrameIndexIsMonotonicAcrossConcurrentDispatches` — 20 並發 dispatch，index 仍單調
- `testPixelBufferRetainReleaseBalance` — mock CV hooks 確認 retain count 歸零

**風險**：低。純邏輯搬家，行為一致。

**LoC 估**：新檔 ~150 + 測試 ~200，CameraViewController 減 ~80。

### PR-iOS-2：`ServerWebSocketConnection.swift`（中風險）

**動作：**
- 新增 `ball_tracker/ServerWebSocketConnection.swift`
- 搬整塊 WS：`connectWebSocket`, `disconnectWebSocket`, `scheduleWebSocketReconnect`, `receiveNextWebSocketMessage`, `handleWebSocketText` (parse 那層留下來), `sendWebSocketJSON`
- `CameraViewController` 改成 delegate 實作
- 保留所有 iOS 端 business logic（arm apply / settings apply）在 CameraViewController

**測試（`ServerWebSocketConnectionTests.swift`）：**
- `testConnectTransitionsStateThroughExpectedPhases`
- `testDisconnectCancelsPendingReconnect`
- `testSendDropsSilentlyWhenDisconnected`
- `testReconnectBackoffDoublesUntilCap`
- `testReconfigureWithSameURLDoesNotTearDown`
- `testReconfigureWithDifferentURLReconnects`
- `testMalformedReceivedPayloadIsIgnored`

測試用 **fake WebSocket**（mock URLSession 或自建 MockWebSocketConnection 讓 integration test 不需實際網路）。

**風險**：中。State machine + reconnect timing 需仔細。背景/前景切換行為需手動 smoke test。

**LoC 估**：新檔 ~200 + 測試 ~250，CameraViewController 減 ~130。

### PR-iOS-3：`LiveFrameDispatcher.swift`（低風險）

**動作：**
- 新增 `ball_tracker/LiveFrameDispatcher.swift`
- 把 `handleDetectedFrame` 的 WS send 那段抽出來
- `ConcurrentDetectionPool.onFrame` → `frameDispatcher.dispatch(frame)` + `detectionFramesBuffer.append(frame)` 並列

**測試（`LiveFrameDispatcherTests.swift`）：**
- `testDispatchSendsWhenLivePathIsActive`
- `testDispatchDropsWhenNotLivePath`
- `testDispatchDropsWhenSessionIdNil`
- `testDispatchDropsWhenWSDisconnected`
- `testDropCountersClassifyCorrectly`

用 `MockServerWebSocketConnection`（無實際網路）驗 `send(_:)` 被叫幾次。

**風險**：極低。

**LoC 估**：新檔 ~80 + 測試 ~150，CameraViewController 減 ~30。

### PR-iOS-4：XcodeProj 整合 + smoke test（收尾）

**動作：**
- 確認三個新檔都進了 XcodeProj 的 target（Xcode 16 `PBXFileSystemSynchronizedRootGroup` 自動帶，但仍需驗證）
- 確認測試 target 的 scheme 跑得到三組新測試
- 手動 smoke：手機連 server，arm 一次，驗 live frame 真的送得到，重啟 server 驗 reconnect，切背景 / 回前景驗 state machine

**風險**：低。純整合驗證。

**LoC 估**：0（只改專案檔）。

---

## 5. 不在本計劃範圍內（但列出避免漏）

以下是當前 `CameraViewController` 也該拆但**留給後續 PR**的東西，本計劃不包：

- **Time sync coordinator**（~200 行）：`startTimeSync` / `beginTimeSync` / `cancelTimeSync` / `completeTimeSync` + mutual sync。邏輯複雜（雙裝置發給對方 + legacy chirp anchor），單獨一個 PR 更安全。
- **Cycle routing**（~200 行）：`handleFinishedClip` + 三路分流。等 live streaming 成為主路徑後，cameraOnly / onDevice 分流邏輯會重構，現在搬等於白做。
- **Exposure / FOV / intrinsics apply**（~400 行）：跟 AVFoundation 綁死，拆出去意義不大，留在 controller。
- **Settings diff apply**：跨多個子系統，目前直接寫在 controller 內做 fan-out 反而清楚，拆出去反而破壞一站式閱讀。

---

## 6. 回歸測試策略

每個 PR 上 main 前必須通過：

### 6.1 自動化
- **Server pytest 全綠**：214+ tests（拆 iOS 不該動到 server）
- **iOS unit tests**：新增的測試 + 既有 `ball_trackerTests`
- **iOS build succeeds**：Xcode 16 Clean Build，target iPhone 14 Pro，Debug + Release

### 6.2 手動 smoke（每個 PR 都做一次）
1. 開 server，啟動 app
2. 觀察 HUD：`Server` 顯示 ARMED/IDLE 正確
3. Dashboard `Arm` → iPhone 狀態變成 recording → 模擬丟球 → 看 `Events` 出現
4. **PR-iOS-1 專項**：開 Telemetry 面板看 `A fps` 是否穩定 ~230（並行 pool 正常）+ `droppedFrameCount` 為 0
5. **PR-iOS-2 專項**：server 重啟一次，觀察 iPhone WS 重連（`Last contact` 跳回很近的數）
6. **PR-iOS-3 專項**：Dashboard 切 `paths = [live]`，看 3D canvas 點即時浮現

### 6.3 性能回歸（PR-iOS-1 必驗）
- 並行 detection 不該比序列版本慢
- A17 CPU usage 滿載錄影 30s 下測量 max temp；若 throttle 太早則調整 maxConcurrency

---

## 7. 時程預估

| PR | 實作 | 測試 | 手動驗證 | 總計 |
|---|---|---|---|---|
| PR-iOS-1 | 2h | 2h | 0.5h | 0.5 天 |
| PR-iOS-2 | 4h | 4h | 1h | 1 天 |
| PR-iOS-3 | 1h | 1.5h | 0.5h | 0.5 天 |
| PR-iOS-4 | 0.5h | — | 0.5h | 0.5 天 |
| **合計** | | | | **2.5 工作天** |

---

## 8. 決策點 / 開放問題

1. **是否也同時拆 `HeartbeatCommandBus`？**
   目前 `ServerHealthMonitor` 仍跑 HTTP `/heartbeat` 作為 WS fallback，其處理命令的邏輯跟 WS 收命令邏輯幾乎一樣。可以考慮做個 `CommandBus` 統一 dispatch，但當前兩者 side-by-side 也不算混亂。傾向：**不拆**，等 HTTP heartbeat 淘汰（plan PR-I）時再整合。

2. **`LiveFrameDispatcher` 是否要加上 send retry / buffer？**
   Live frame 的特性是「過期即失效」，buffer 重送意義不大。plan §4.4 明確寫 drop 不補。堅持 drop。

3. **測試替身：`MockWebSocketConnection` vs `URLSessionWebSocketTask` + localhost？**
   傾向 Mock — 不啟任何網路，測試 100ms 內跑完。integration test 留給 PR-iOS-4 的手動 smoke。

4. **檔名位置：放 `ball_tracker/` 根目錄還是新建 `ball_tracker/Networking/` + `ball_tracker/Detection/`？**
   看專案慣例。目前所有 Swift 都平鋪在 `ball_tracker/` 下，先跟慣例。未來規模再大再分子資料夾。

---

## 9. 成功指標

此計劃完成後量測：

- **CameraViewController.swift 行數 ≤ 1600**（目標：從 2816 → 50%+ 減少）
- **3 個新檔案各有 ≥ 5 個單元測試，覆蓋率 ≥ 70%**
- **既有 iOS + server 測試 100% 通過**
- **手動 smoke test 全過**
- **CPU / 熱 benchmark 不退步**

---

## 10. 附錄：參考位置

- [CameraViewController.swift:166-190](ball_tracker/CameraViewController.swift:166) — detection pool 狀態屬性
- [CameraViewController.swift:2672-2718](ball_tracker/CameraViewController.swift:2672) — `dispatchDetection`
- [CameraViewController.swift:1458-1590](ball_tracker/CameraViewController.swift:1458) — WS 整塊
- [CameraViewController.swift:2704-2716](ball_tracker/CameraViewController.swift:2704) — live frame WS send
- [docs/live_streaming_architecture_plan.md](docs/live_streaming_architecture_plan.md) §4.1, §8.1 — 本計劃源自的上層計劃
