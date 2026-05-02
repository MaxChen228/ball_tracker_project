# GitHub/HuggingFace 開源生態調查
**目標**：iPhone 14 (A15) 上 200 fps 偵測深藍硬球，Pre-DL HSV baseline R=0.905 / 1.96 ms。  
**日期**：2026-05-01

---

## 1. TrackNet / TrackNetV2 / TrackNetV3

### 官方 repo
| Repo | Stars | License | 最後活躍 | iOS/CoreML |
|------|-------|---------|---------|-----------|
| [qaz812345/TrackNetV3](https://github.com/qaz812345/TrackNetV3) | 210 | MIT | 36 commits，無近期日期 | 無 |
| [alenzenx/TrackNetV3](https://github.com/alenzenx/TrackNetV3) | 142 | MIT | 37 commits | 無 |
| [yastrebksv/TrackNet](https://github.com/yastrebksv/TrackNet) | — | — | unofficial PyTorch port | 無 |

**未找到**：TrackNet 任何 CoreML conversion fork。

**評估**：TrackNetV3 (qaz812345) 是最強的羽毛球追蹤實作，MIT，準確率 97.51% vs YOLOv7 的 57.82%。架構是 heatmap-based（輸入 3 連續幀 → 輸出中心 heatmap），對 5 ms/frame 預算壓力很大（ResNet backbone）。沒有 mobile 轉換路徑，需要自己 `ct.convert()` + 量化。深藍硬球比羽毛球大，但速度相近，理論上可遷移 fine-tune。**只適合作演算法借鑑 / 蒸餾對象，不能直接用。**

---

## 2. YOLOv8-nano / YOLOv11-nano / YOLO-NAS-S — iPhone Core ML

| Repo | Stars | License | 最後 commit | 備註 |
|------|-------|---------|------------|------|
| [ultralytics/yolo-ios-app](https://github.com/ultralytics/yolo-ios-app) | 470 | AGPL-3.0 | 2026-04-17 | 支援 YOLO11 + YOLO26，NMS-free Swift postproc |
| [tucan9389/ObjectDetection-CoreML](https://github.com/tucan9389/ObjectDetection-CoreML) | 339 | MIT | 2023-04-07 | YOLOv8n iPhone 14 Pro 實測 **15 ms** |
| [TheCluster/YOLOv11-CoreML](https://huggingface.co/TheCluster/YOLOv11-CoreML) | 7 likes | AGPL-3.0 | 2024 | 已測 A15 iPhone 14，無具體 latency |

**關鍵 benchmark（tucan9389，iPhone 14 Pro）**：
- YOLOv8n：**15 ms**（≈ 67 FPS），距 5 ms 目標還有 3× gap
- YOLOv5n：24 ms；YOLOv8s：29 ms

**YOLO-NAS-S**：未找到 CoreML 官方移植，Deci.ai 的 YOLO-NAS 無公開 iOS 範例。

**評估**：YOLO11n + coremltools INT8 + Neural Engine 在 iPhone 14（A15）上推算約 10–15 ms，換算 67–100 FPS。**離 200 fps / 5 ms 有距離**，但若 crop ROI（縮小輸入解析度到 320×180）latency 可能壓到 5–8 ms。ultralytics/yolo-ios-app 是最完整的 Swift integration 範例（AGPL，個人 LAN 可用）。

---

## 3. MobileNetV3 / EfficientNet-Lite + detection head CoreML

| Repo | Stars | License | 說明 |
|------|-------|---------|------|
| [gouthamvgk/coreml_conversion_hub](https://github.com/gouthamvgk/coreml_conversion_hub) | 42 | 未標示 | 提供 MobileNetV3, EfficientDet-D0~D3, EfficientNet-Lite b0~b4 的 `.mlmodel` 下載 |
| [tucan9389/ObjectDetection-CoreML](https://github.com/tucan9389/ObjectDetection-CoreML) | 339 | MIT | MobileNetV2+SSDLite iPhone 14 Pro 17 ms |

**評估**：EfficientDet-Lite0 是最輕量的 detection head 選項，SSDLite + MobileNetV3 大體同量級。coreml_conversion_hub 提供可直接下載的 `.mlmodel`，但 star 數低（42）、最後 commit 時間不明、無 license。**MobileNetV2+SSDLite 的 17 ms 與 YOLOv8n 的 15 ms 相近，對本場景無決定性優勢；backbone 固定不易針對小球 fine-tune。**

---

## 4. Apple coremltools — 最新 INT8/量化支援

| 版本 | 發布日期 | 關鍵功能 |
|------|---------|---------|
| **9.0** | 2025-11-10 | iOS26/macOS26 target，int8 input/output dtype，PyTorch 2.7，AllowLowPrecisionAccumulationOnGPU hint |
| **8.3.0** | 2025-04-29 | MLModelBenchmarker（連線 iPhone 量測 latency），MLModelValidator/Comparator |

**INT8 量化**（coremltools docs）：
- W8A8（weight + activation int8）在 A17 Pro / M4 上有顯著 speedup（ResNet50: 1.52ms → 0.94ms）
- A15（iPhone 14）：W8A8 benefit 較小（A17/M4 特化），但 weight-only INT8 仍有 ~2× 壓縮
- **建議**：先用 weight-only INT8（`ct.optimize.coreml.linear_quantize_weights`），再實測 A15 latency；不要盲目開 activation quantization（CPU/GPU 路徑反而變慢）

---

## 5. Apple Vision framework — small object / VNCoreMLRequest 新 API

WWDC25（2025）主要更新：
- `RecognizeDocumentsRequest`（文字/表格識別）
- HandPose detection 換更小 model（latency 降、accuracy 升）
- `VNCoreMLRequest` 本身無新 small-object 強化 API

**未找到**：Vision framework 2025 對 small object detection 的新 API。

**現有路徑**：`VNCoreMLRequest` → `VNImageRectificationObservation` / `VNRecognizedObjectObservation`，後處理（NMS, confidence filter）仍需自己實作或用 model 內建。ultralytics/yolo-ios-app 的 YOLO26 branch 已把 NMS 移到 Swift 端做（Neural Engine 友好）。

---

## 6. FRST (Loy 2003) 開源實作

| Repo | Stars | License | 語言 | 說明 |
|------|-------|---------|------|------|
| [ChristianGutowski/frst_python](https://github.com/ChristianGutowski/frst_python) | 13 | GPL-3.0 | Python (NumPy+cv2) | PyPI: `pip install frst`，pure Python |
| [Xonxt/frst](https://github.com/Xonxt/frst) | 19 | 未標示 | C++ (OpenCV 3) | MATLAB port，header-only |
| [nathanin/FRST](https://github.com/nathanin/FRST) | — | — | Python | 無維護跡象 |

**未找到**：Swift / Metal port。

**評估**：frst_python (GPL-3.0) 是最容易驗證的路徑，但 pure Python 速度不足（240fps pipeline 絕對是瓶頸）。Xonxt/frst C++ 版可直接整合到 OpenCV pipeline。FRST 對圓形高光點很敏感，**適合作深藍硬球的輔助 seed detector，不能取代 HSV**——但可以在 5×5 ROI 內做 sub-pixel center refinement，或作 `candidates = 0` 時的 fallback（注意 CLAUDE.md 禁止 silent fallback，這要 explicit toggle）。

---

## 7. Specular highlight removal

| Repo | Stars | License | 說明 |
|------|-------|---------|------|
| [muratkrty/specularity-removal](https://github.com/muratkrty/specularity-removal) | 69 | 未標示 | Python，OpenCV inpainting (Telea/NS)，針對 endoscopic video |
| [gmichaeljaison/specularity-removal](https://github.com/gmichaeljaison/specularity-removal) | — | — | multi-view homography，不適用單目 |
| [fu123456/TSHRNet](https://github.com/fu123456/TSHRNet) | — | — | DL 三階段，too heavy |

**未找到**：Apple-native 或 Metal 的 specular removal 實作。

**評估**：深藍硬球的高光是白色圓形 blob，HSV 下 S 極低、V 極高。muratkrty 的方法（saturation map → inpaint）在 Python 可跑，但 240fps 上每幀 inpaint 不現實。**實用策略**：mask 生成時用 `cv2.threshold(hsv_v, 245, 255, THRESH_BINARY)` 找高光 blob → `cv2.inRange` 後做 morphological dilation 補洞，代替完整 inpaint pipeline。此為純 OpenCV，無需外部 repo。

---

## 8. Background subtraction — high-fps lightweight

| Repo | Stars | License | 說明 |
|------|-------|---------|------|
| [vandroogenbroeckmarc/vibe](https://github.com/vandroogenbroeckmarc/vibe) | 13 | **專利保護**（EP/US/JP）| C/C++/MATLAB/Python，real-time，但**商用需授權**（個人 LAN 研究使用 OK）|
| [Qengineering/Fast-Background-Substraction](https://github.com/Qengineering/Fast-Background-Substraction) | 12 | BSD-3-Clause | 加權平均 bg，Raspberry Pi 設計，有 MOG2/KNN 對比 |

**評估**：ViBe 在速度上確實比 MOG2 快（純樣本比較，無 Gaussian 擬合），但 star 數極低（13）、有多國專利（個人研究可接受）。對本場景而言，**bg subtraction 的最大問題是相機移動**（使用者手持拍攝？）。若相機固定，ViBe 是值得試的非 Gaussian 方法，可取代已棄用的 MOG2。Qengineering 版太輕量、沒有統計 model，不適合遮擋場景。

---

## Top 3 worth integrating

### #1 — `ultralytics/yolo-ios-app` + YOLO11n CoreML INT8
**為何選**：最新（2026-04-17 commit）、YOLO26 NMS-free Swift postproc、官方 Ultralytics 維護、470 stars。iPhone 14 Pro YOLOv8n 實測 15 ms，如果輸入縮到 320×180 crop-ROI 模式，A15 Neural Engine 有機會壓到 5–8 ms。  
**如何 integrate**：
1. `yolo export model=yolo11n.pt format=coreml int8=True` 生成 `.mlpackage`
2. Fork yolo-ios-app 的 `CameraViewController` 邏輯，替換現有 HSV 路徑成 CoreML inference
3. 用 coremltools 8.3.0 的 `MLModelBenchmarker` 連 iPhone 14 量實際 latency
4. 若 latency > 5 ms，縮 input resolution 或換 crop-ROI 策略（先用 HSV ROI 定位，再餵小 patch 給 YOLO）
**注意**：AGPL-3.0，個人 LAN 工具無問題。fine-tune 需要深藍球 labeled dataset。

### #2 — `qaz812345/TrackNetV3` 作蒸餾/演算法借鑑
**為何選**：heatmap-based tracking 天然比 bounding-box YOLO 更適合小球偵測（無 NMS、直接輸出 pixel-level confidence map）；MIT。  
**如何 integrate（不是直接用）**：
1. 用 TrackNetV3 在現有 session MOV 上生成 pseudo-label（server_post 路徑）
2. 用這批 pseudo-label 訓練輕量 heatmap head（MobileNetV3 backbone + 1 conv heatmap decoder）
3. 轉 CoreML，target 是 < 5 ms / 200fps
**這是蒸餾路徑，需要 2–3 週研究投入**；短期不如 #1 直接。

### #3 — `Xonxt/frst` (C++ FRST) 作 sub-pixel center refinement
**為何選**：FRST 對圓形對稱點（球心）天然敏感，C++ header-only 可嵌入 server-side；對現有 HSV+CC pipeline 的 centroid 估計可提升精度，不替換整條路徑。  
**如何 integrate**：
1. 在 `detection.py` 的 `_resolve_candidates` 之後，對勝出 bounding rect 的 16×16 ROI 跑 FRST（Python ctypes 或 PyBind11 呼叫 C++）
2. FRST 輸出 radial symmetry map → argmax → sub-pixel center
3. 用新 center 覆蓋 CC centroid，重算 px/py
**低風險、低成本**（只改 server-side detection，不動 iOS）；預期 triangulation 誤差改善 1–3 px。

---

*Generated 2026-05-01. Fetch 來源均為實際 repo page / Apple docs，無記憶補全 star 數。*
