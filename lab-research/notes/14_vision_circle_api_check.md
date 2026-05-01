# Vision Framework Circle API 查證報告

**查證日期**：2026-05-01  
**查證方式**：developer.apple.com 直接 fetch、iOS SDK header dump（xybp888/iOS-SDKs iPhoneOS18.0.sdk）、Google site: search、WWDC session fetch

---

## 核心結論

**`VNDetectCirclesRequest` 不存在。**

- `site:developer.apple.com "VNDetectCirclesRequest"` 零結果
- iPhoneOS 18.0 SDK `Vision.framework/Headers/` 目錄下**無任何含 Circle 字眼的 header**（完整掃描確認）
- GitHub/SO 全域搜尋 `"VNDetectCirclesRequest"` 和 `"VNDetectCircles"` 均無命中
- WWDC 2018–2025 所有 Vision session 均無提及 circle detection API
- 任何聲稱「`VNDetectCirclesRequest` 跑 60 FPS」的 claim **屬實捏造，不是既有 API**

---

## Apple Vision Framework — VNDetect* 完整 inventory（iOS 18.0 SDK header 確認）

| Request | 功能 | 可用起 |
|---|---|---|
| `VNDetectAnimalBodyPoseRequest` | 動物肢體姿態 | iOS 17 |
| `VNDetectBarcodesRequest` | 條碼 / QR code | iOS 11 |
| `VNDetectContoursRequest` | 邊緣輪廓偵測 | iOS 14 |
| `VNDetectDocumentSegmentationRequest` | 文件區域裁切 | iOS 15 |
| `VNDetectFaceCaptureQualityRequest` | 臉部捕捉品質評分 | iOS 13 |
| `VNDetectFaceLandmarksRequest` | 臉部特徵點 | iOS 11 |
| `VNDetectFaceRectanglesRequest` | 臉部矩形框 | iOS 11 |
| `VNDetectHorizonRequest` | 水平線偵測 | iOS 11 |
| `VNDetectHumanBodyPose3DRequest` | 人體 3D 姿態 | iOS 17 |
| `VNDetectHumanBodyPoseRequest` | 人體 2D 姿態 | iOS 14 |
| `VNDetectHumanHandPoseRequest` | 手部姿態 | iOS 14 |
| `VNDetectHumanRectanglesRequest` | 人體矩形框 | iOS 13 |
| `VNDetectRectanglesRequest` | 矩形偵測 | iOS 11 |
| `VNDetectTextRectanglesRequest` | 文字區域矩形框 | iOS 11 |
| `VNDetectTrajectoriesRequest` | 拋物線軌跡偵測 | iOS 14 |

WWDC24 新增（非 VNDetect 命名）：
- `CalculateImageAestheticsScoresRequest`（iOS 18）

WWDC25 新增：
- `DetectCameraLensSmudgeRequest`（iOS 26）

**無任何 circle / blob 偵測 request。**

---

## 有沒有 functionally similar API？

### 1. `VNDetectTrajectoriesRequest`（iOS 14+）— 最接近球偵測

**能做什麼**：偵測影片幀序列中符合拋物線軌跡的移動物件，返回 `VNTrajectoryObservation`（含軌跡點座標 + 拋物線方程式）。

**關鍵參數**：
- `frameAnalysisSpacing`（CMTime）：幀處理間隔，kCMTimeZero = 不跳幀
- `trajectoryLength`：需要累積 ≥5 個點才輸出觀測結果
- `objectMinimumNormalizedRadius` / `objectMaximumNormalizedRadius`：按物件大小過濾

**對本專案的意義**：
- 優點：原生偵測拋物線球軌跡，不需要 HSV 顏色標記
- 缺點：
  1. 需要連續多幀（至少 5 點）才輸出，latency 高
  2. 偵測的是「會拋物線移動的物件」，不區分球色 — 高噪環境容易誤判
  3. **不返回逐幀球心座標**，只返回軌跡段；無法替換本專案逐幀座標串流架構
  4. 要求物件在多幀間連續可見，240fps 快速飛過 + 遮擋環境下可靠性未知

**結論**：不是 VNDetectCirclesRequest 的替代，是完全不同用途（軌跡擬合 vs 逐幀偵測）。

### 2. `VNDetectContoursRequest`（iOS 14+）— 可後處理但重量級

返回圖像所有邊緣輪廓（`VNContoursObservation`），理論上可對輪廓做圓形度（circularity = 4πA/P²）篩選。

**問題**：
- 輸出是全圖所有輪廓，後處理量大
- 240fps 逐幀跑代價未知，比 HSV+CC 更重
- 非 ball-specific；不如現行 HSV 管線精準

### 3. `CIDetector`（Core Image）

`CIDetector` 支援 `CIDetectorTypeFace`、`CIDetectorTypeRectangle`、`CIDetectorTypeQRCode`、`CIDetectorTypeText`，**無 circle type**。

### 4. `VNTrackObjectRequest` / `VNTrackRectangleRequest`

追蹤 request，需要已知初始 bounding box，不做 detection。

---

## 回應原始 claim

> 「VNDetectCirclesRequest 在 iPhone 15 Pro 跑 60 FPS」

- **API 不存在**：iOS 18.0 SDK header 直接確認，WWDC 2018–2025 均無此 API
- **不是命名差異**：Vision framework 沒有任何 circle 相關 detection request
- 此 claim 是 LLM 幻覺或資訊錯誤，**不可信**

---

## 對 ball_tracker_project 的建議

現行 HSV+CC+shape gate 管線（`server/detection.py` + iOS `CameraViewController`）**是正確選擇**：

1. Apple 沒有原生 circle detection → 自訂 HSV 管線無替代品
2. `VNDetectTrajectoriesRequest` 不能替換逐幀偵測架構（需累積多幀才輸出）
3. 若未來要嘗試無色標偵測，`VNDetectTrajectoriesRequest` 值得在 lab 實驗（加 `trajectoryLength=5`，看能否在 240fps 下穩定觸發）

---

*Sources*:
- iPhoneOS 18.0 SDK headers: https://github.com/xybp888/iOS-SDKs/tree/master/iPhoneOS18.0.sdk/System/Library/Frameworks/Vision.framework/Headers
- WWDC24 Vision session: https://developer.apple.com/videos/play/wwdc2024/10163/
- VNDetectTrajectoriesRequest doc: https://developer.apple.com/documentation/vision/vndetecttrajectoriesrequest
- VNDetectContoursRequest doc: https://developer.apple.com/documentation/vision/vndetectcontoursrequest
