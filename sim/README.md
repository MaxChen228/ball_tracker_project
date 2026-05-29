# Godot 棒球軌跡模擬模型

Godot 4.6.3 mono 專案 — 3D 棒球場景與軌跡視覺化。

## 開啟專案

```bash
# 用 Godot 4.6.3+ 開啟
open -a Godot_mono.app .
# 或
godot --path .
```

## 專案結構

| 檔案 | 用途 |
|---|---|
| `BallTrackingCam.cs` | 追蹤球體的相機控制器 |
| `BallTrackingCamera.cs` | 球體追蹤相機（備用/擴展） |
| `Baseball.cs` | 棒球實體邏輯 |
| `CameraSpeedUI.cs` | 相機縮放速度 UI 控制 |
| `DefenseManager.cs` | 防守球員管理 |
| `Fielder.cs` | 單一防守球員 AI |
| `FreeCamera.cs` | 自由攝影機（手動操控） |
| `ManualInputUI.cs` | 手動輸入 UI 面板 |
| `PitchDecisionMenu.cs` | 投球決策選單 |
| `ScoreBugUI.cs` | 比分顯示 UI |
| `SettingsUI.cs` | 設定面板 UI |
| `StadiumDisplays.cs` | 球場場景顯示 |
| `StartMenu.cs` | 啟動選單 |
| `StrikeZoneVisualizer.cs` | 好球帶視覺化 |
| `stadium.tscn` | 球場場景 |
| `start_menu.tscn` | 選單場景 |
| `sprint.glb` | 球場 3D 模型 |

## 依賴

- Godot 4.6.3 mono
- `Godot.NET.Sdk` 4.6.3+
