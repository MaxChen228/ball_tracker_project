# sim — Godot trajectory viewer

ball_tracker 的 **3D 軌跡視覺化端**。職責單一：把 server 算好的彈道擬合
（`SegmentRecord.{p0, v0, t_anchor, ...}`）在 3D 空間畫出來、播放一顆球
沿軌跡飛。

點 / 進壘 / strike zone 判決 / 計分等 gameplay 由 dashboard 處理，**不在
這裡**。這邊只負責「給定 session id，把擬合軌跡渲染成 3D 場景」。

## Data flow

```
ball_tracker server (ball_tracker_project/server)
   GET /sessions/{sid}/trajectory?algorithm={algo}
       ↓ HTTP JSON, server world frame, segments only
sim/TrajectoryViewer.cs
       ↓ 反向座標轉換 (server world → Godot Y-up)
       ↓ ImmediateMesh polyline + animated Sphere
Godot 3D 場景
```

**沒有 UDP、沒有中介 process、沒有 raw points 過線。** raw 三角化點留在
`/results/{sid}` 給 dashboard 畫散佈，本 viewer 只吃擬合結果。

## Wire schema

`GET /sessions/{sid}/trajectory?algorithm={algorithm_id}`：

```json
{
  "session_id": "s_xxx",
  "algorithm_id": "ios_capture_time",
  "frame": "server_world",
  "gravity": [0.0, 0.0, -9.81],
  "segments": [
    {
      "p0": [x, y, z],
      "v0": [vx, vy, vz],
      "t_anchor": 1.234,
      "t_start": 1.200, "t_end": 1.560,
      "rmse_m": 0.018,
      "speed_kph": 142.3
    }
  ]
}
```

Client 端用標準彈道公式取樣畫曲線：

```
p(τ) = p0 + v0·(τ - t_anchor) + ½·G·(τ - t_anchor)²
```

擬合曲線的 single source of truth 是這幾個參數，dashboard / Godot / 未來
notebook 各自取樣，不會 drift。

`algorithm` query 預設 `ios_capture_time`（live 路徑）；server_post
algorithm 用 `?algorithm=v11_hsv_cc` 之類。session 沒對應 algorithm 的
fit 結果會 404 並列出可用算法 — **不會 silent 回空陣列**。

## 座標系轉換（在 sim 端）

| 軸 | server world | Godot frame |
|---|---|---|
| X | 右（baseline 方向） | 右 |
| Y | 本壘 → 投手丘 | **上** |
| Z | **上** | 本壘 → 投手丘的 **反方向** (-Z) |

對應映射（`TrajectoryViewer.cs::ServerToGodot`）：

```
godot.x =  server.x
godot.y =  server.z
godot.z = -server.y
```

重力檢查：`(0, 0, -9.81)` server → `(0, -9.81, 0)` Godot ✓。

座標轉換**只在 consumer 做**。server 永遠送 server world frame，dashboard
也是 consumer，自己 transform。依賴方向才不會反掉。

## Run

兩個 terminal：

**Terminal A** — ball_tracker server（如果還沒在跑）：

```bash
cd ball_tracker_project/server
uv run uvicorn main:app --host 0.0.0.0 --port 8765
```

**Terminal B** — Godot：用 Godot 4.6 開 `ball_tracker_sim.sln` /
`project.godot`，按 ▶。main scene 是 `trajectory_viewer.tscn`。

UI 左上角輸入 session id (`s_xxx`) → Load → Play/Pause。WASD 飛行、滑鼠
轉視角、ESC 釋放滑鼠。

## 改 server URL / 預設值

`TrajectoryViewer` 是 `Node3D`，Inspector 裡有 5 個 export：

- `Server Base Url`（預設 `http://127.0.0.1:8765`）
- `Default Session Id`（預設空，要手動輸入）
- `Default Algorithm Id`（預設 `ios_capture_time`）
- `Samples Per Segment`（每段擬合曲線取樣點數，預設 80）
- `Playback Speed`（球飛行速度倍率，預設 0.5）

## 不要做的事

- 不要在 sim 加 raw 三角化點的散佈 — 那是 dashboard debug 視覺
- 不要在 sim 加 strike zone / pitch decision UI — dashboard 的事
- 不要從 sim 動 server 的座標約定（server world frame 是 server 的內部
  事，sim 配合 transform 就好；transform 不對是 sim 改、不是 server 改）
- 不要重新引入 UDP / sender.py 那條兩跳路徑（已退役）
