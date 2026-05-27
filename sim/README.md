# ball_tracker_sim

ball_tracker 的 Godot 端 3D 棒球模擬視覺化（接收 stereo triangulation 軌跡，跑物理 + 守備 + 判決 UI）。

## Data flow

```
ball_tracker session (data/results/session_*.json)
      ↓  uv run python server/godot_bridge.py --session s_xxx
sender.py:8888                (UDP, 本專案 / Python middleware)
      ↓  hit-feature extraction (X-Z 折點 + Z 軸反轉)
Baseball.cs:9999              (UDP, Godot C# 3D 模擬 + PitchDecisionMenu)
```

兩段 UDP 都是 localhost 鬆耦合，沒有 import 依賴。

## Wire schema

**ball_tracker `godot_bridge.py` → `sender.py:8888`**（已 swap Y↔Z，bridge 端負責）

```json
{
  "trajectory": [
    {"t": 0.0, "x": 0.0, "y": 1.5, "z": -18.44},
    ...
  ]
}
```

- `t` 秒；`x, y, z` 公尺，Godot frame（見座標系）
- 至少 3 點才會跑 hit-feature 抽取

**`sender.py` → `Baseball.cs:9999`**

```json
{
  "pitch_x_m": 0.08,
  "pitch_y_m": 0.82,
  "call": "WAITING_UI",
  "exit_velocity_mph": 0.0,
  "launch_angle_deg": 0.0,
  "spray_angle_deg": 0.0,
  "spin_rate_rpm": 2200.0
}
```

- 投球無打擊時 exit/launch/spray 全 0，Godot 收到 `WAITING_UI` 會彈 PitchDecisionMenu
- 偵測到擊球折點時三項填實值

## Run

兩個 terminal：

**Terminal A** — Godot 端 middleware（daemon）

```bash
python sender.py --daemon
# 可選：--listen-port 8888 --godot-host 127.0.0.1 --godot-port 9999
```

接著用 Godot 開啟 `ball_tracker_sim.sln` / `project.godot` 跑模擬。

**Terminal B** — ball_tracker 推送（在 `ball_tracker_project/` 下）

```bash
uv run python server/godot_bridge.py --list                 # 看 session 列表
uv run python server/godot_bridge.py --session s_xxx        # 推送指定 session
```

`sender.py` 也保留互動模式：直接 `python sender.py` 進選單，可選內建擊球/揮空/讀 JSON/切到監聽。

## 座標系（Godot frame）

- **X** 左右（+X 一壘方向）
- **Y** 上（+Y 天空）
- **Z** 朝/離投手（-Z 朝投手丘 / 中外野，+Z 朝本壘後方）
- 原點：本壘板正上方地面

ball_tracker 那邊用的是相機/世界座標，`godot_bridge.py` 已負責 swap Y↔Z 與符號對齊，**本專案不要再做 transform**。

## Coupling note

`sender.py` 的 wire schema 是 ball_tracker `godot_bridge.py` 的 reverse contract。**任一邊改 schema，兩邊都要同步動**（field 名、單位、座標約定）。兩個 repo lockstep 部署，不做向後相容 shim。

## Files

- `sender.py` — Python middleware（UDP in 8888 → hit-feature → UDP out 9999）
- `Baseball.cs` — Godot 端 UDP listener + 3D 物理 + 判決流程（**不要改動**）
- `PitchDecisionMenu.cs` / `ScoreBugUI.cs` / `DefenseManager.cs` / `Fielder.cs` 等 — Godot 端 UI / 守備
- `project.godot` / `ball_tracker_sim.sln` / `ball_tracker_sim.csproj` — Godot 4.6 + C# 專案檔
