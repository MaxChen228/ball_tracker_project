# A15 Detection Latency Benchmarks & Tiny CNN Feasibility

> 調查日期：2026-05-01  
> 目標：決定 live 路徑能否在 5–10 ms 預算下跑 tiny detection head（heatmap）

---

## 一、實測 Latency 表格

### 1-A. Detection / YOLO 類（直接相關）

| Model | Input | Params | Quantization | Device / Chip | Latency (ms) | Compute Unit | Source |
|---|---|---|---|---|---|---|---|
| YOLOv5s | 192×320 | ~7M | FP8 (CoreML) | iPhone 12 / A14 | **10 ms** (ANE) / 47 ms (GPU) / 27 ms (CPU) | ANE | [Dluznevskij 2022, BJMC](https://www.bjmc.lu.lv/fileadmin/user_upload/lu_portal/projekti/bjmc/Contents/9_3_07_Dluznevskij.pdf) |
| YOLOv5s | 192×320 | ~7M | FP8 | iPhone 11 / A13 | **17.4 ms** | ANE+GPU (`all`) | [Ultralytics #1276](https://github.com/ultralytics/yolov5/issues/1276) |
| YOLOv5s | 192×320 | ~7M | FP8 | iPhone 12 / A14 | **14.3 ms** | ANE+GPU (`all`) | [Ultralytics #1276](https://github.com/ultralytics/yolov5/issues/1276) |
| YOLOv8n (mlpackage) | 640×640 (est.) | ~3M | FP16 | 未指定 iOS 裝置 | **101 ms** | `.all` | [Ultralytics #6788](https://github.com/ultralytics/ultralytics/issues/6788) |
| YOLO11 (CoreML) | 未指定 | 中型 | INT8/FP16 | 未指定 iPhone | **60+ FPS (~16 ms)** | ANE (`.all`) | [Roboflow iOS blog](https://blog.roboflow.com/best-ios-object-detection-models/) |

> ⚠ iPhone 13/A15 直接 detection benchmark 未找到公開數字。

### 1-B. Classification / Backbone（間接參考）

| Model | Input | Quantization | Device / Chip | Latency (ms) | Compute Unit | Source |
|---|---|---|---|---|---|---|
| MobileNetV3-Large | 224×224 | FP16 | iPhone 13 / **A15** | **58.7 ms** (ANE) / 11.3 ms (GPU) | ANE | [TildAlice TFLite vs CoreML](https://tildalice.io/tflite-vs-coreml-ios-latency-benchmark/) |
| ResNet-50 | 224×224 | FP16 | iPhone 13 / **A15** | **19.8 ms** (ANE) | ANE | [TildAlice TFLite vs CoreML](https://tildalice.io/tflite-vs-coreml-ios-latency-benchmark/) |
| MobileNetV2-1.0 | 224×224 | FP16 | iPhone 14 Pro / A16 | **0.48 ms** | `.all` | [coremltools opt docs](https://apple.github.io/coremltools/docs-guides/source/opt-quantization-perf.html) |
| MobileNetV2-1.0 | 224×224 | W+A INT8 | iPhone 14 Pro / A16 | **0.27 ms** | `.all` | [coremltools opt docs](https://apple.github.io/coremltools/docs-guides/source/opt-quantization-perf.html) |
| ResNet-50 | 224×224 | FP16 | iPhone 14 Pro / A16 | **1.52 ms** | `.all` | [coremltools opt docs](https://apple.github.io/coremltools/docs-guides/source/opt-quantization-perf.html) |

> ⚠ coremltools 官方 doc 的 0.48 ms 是 `.all`（不一定跑在 ANE），且是 batch=1 純 backbone，**無 detection head**。

### 1-C. Segmentation / Heatmap（Guided Cutout，最接近本場景）

| Task | Device / Chip | Latency (ms) | Compute Unit | Source |
|---|---|---|---|---|
| 語義分割 pipeline（guided cutout） | iPhone 13 Pro / **A15** | **45 ms** | `.all` | [Photoroom 2022](https://www.photoroom.com/inside-photoroom/core-ml-performance-2022) |
| 語義分割 pipeline（guided cutout） | iPhone 14 Pro / A16 | **41 ms** | `.all` | [Photoroom 2022](https://www.photoroom.com/inside-photoroom/core-ml-performance-2022) |

> Photoroom pipeline 包含 encoder+decoder（估計類似 U-Net），是本場景**最相關**的 proxy。

### 1-D. Pose / Heatmap Keypoint（架構類似）

| Model | Input | Quantization | Device / Platform | Latency (ms) | Compute Unit | Source |
|---|---|---|---|---|---|---|
| BlazePose Lite | 256×256 | FP16 | Pixel 2 / Android | ~32 ms (31 FPS) | GPU | [Google Blog](https://research.google/blog/on-device-real-time-body-pose-tracking-with-mediapipe-blazepose/) |
| Pose estimation (unnamed) | 未指定 | 未指定 | iPhone X / A11 | **~70 ms** (含後處理) | 未知 | [Fritz AI](https://fritz.ai/just-point-it-machine-learning-on-ios-with-pose-estimation-ocr-using-core-ml-and-ml-kit/) |
| MoveNet Lightning | 192×192 | INT8 | 現代 smartphone | **30+ FPS** (~33 ms 上限) | GPU/NNAPI | [TF Blog](https://blog.tensorflow.org/2021/08/pose-estimation-and-classification-on-edge-devices-with-MoveNet-and-TensorFlow-Lite.html) |

> 未找到 A15 ANE 直接跑 heatmap head 的公開數字。

---

## 二、ANE Op 支援 / Fallback 風險

### 關鍵約束（來源：[hollance/neural-engine](https://github.com/hollance/neural-engine/blob/master/docs/unsupported-layers.md)）

| Op | ANE 支援？ | 備注 |
|---|---|---|
| Conv 2D（kernel ≤ 7） | ✅ | 主要 compute primitive |
| Depthwise Conv | ⚠️ **GPU 更快** | A15 ANE 上 MobileNet depthwise conv → CoreML ANE 58 ms vs TFLite GPU 11 ms（5× 倒退） |
| Transposed Conv / Deconv | ❓ **未確認** | hollance repo 未列出；machinethink 部落格提到 Core ML 支援 deconvolution layer，但未說明是否跑 ANE |
| Bilinear Upsample（scale ≤ 2） | ✅ | `Espresso::ANERuntimeEngine::upsample_kernel` 存在 |
| Bilinear Upsample（scale > 2） | ❌ **ANE 不支援** | 直接 fallback CPU/GPU |
| Pixel Shuffle | ⚠️ **間接** | 需要 Reshape + Permute 組合；不保證全在 ANE |
| NMS（Non-Maximum Suppression） | ❌ | 永遠 CPU fallback |
| SPP（Spatial Pyramid Pooling，kernel > 7） | ❌ | YOLOv5 v2 SPP layer 是 ANE fallback 主因 |
| LSTM / RNN | ❌ | ANE 不支援 |
| Gather / Broadcastable ND ops | ❌ | CoreML 3 新 layer type 在 ANE fallback |

### ANE ↔ CPU/GPU 切換開銷

- 每次 compute unit 切換增加延遲；整個 model 在單一 CU 比混合 CU 更快
- ANE dispatch overhead ≈ 0.095 ms（M 系列量測）
- iOS 17 曾出現 bug：某些 model 在 iOS 17 強制 CPU，latency 暴增 [coremltools #2004](https://github.com/apple/coremltools/issues/2004)
- Depthwise conv 是**本案最大陷阱**：MobileNet 系列主幹全是 depthwise，在 ANE 反而比 Metal GPU 慢 5×

---

## 三、結論

### 5–10 ms 預算下可行解析度上限

| 解析度 | FLOPs 倍數（相對 192×320） | A15 ANE 可行性 | 風險 |
|---|---|---|---|
| 96×96 | ~0.1× | ✅ **極可能 < 5 ms** | 球 radius 4-10 px → heatmap 太小，定位精度不足 |
| 192×192 | ~0.3× | ✅ **5–10 ms 可行** | 需避開 depthwise conv 主幹 + scale > 2 的 upsample |
| 256×256 | ~0.5× | ⚠️ **邊緣** | 傳統 backbone + decoder 結構可能超 10 ms |
| 384×384 | ~1.1× | ❌ 超出預算 | YOLOv5s 192×320 = 10 ms（A14），384×384 估計 25-40 ms |
| 512×512 | ~2× | ❌ | 不考慮 |

> 基準推算邏輯：YOLOv5s 在 A14 @ 192×320 = 10 ms（ANE 跑滿）。A15 ANE 為 15.8 TOPS（A14 11.0 TOPS），計算力 +44%。256×256 的 FLOPs 約 YOLOv5s @ 192×320 的 1.1×，A15 可吸收，但前提是模型全程跑 ANE。

### 最終判斷：live 跑 tiny CNN @ 256×256 **邊緣可行，高依賴架構選擇**

- **可行條件**：backbone 全用 standard conv（不用 depthwise），decoder 用 bilinear upsample（scale ≤ 2 逐次），不含 NMS、SPP、LSTM
- **不可行條件**：MobileNet/EfficientNet backbone（depthwise conv 在 ANE 慢 5×）；transposed conv 主導的 decoder（ANE 支援未確認，fallback 風險高）；一次 4× upsample

---

## 四、推薦 Model 形狀（Ball Detection 用）

### 目標：單球 centroid heatmap，1-channel 輸出，256×256 input

```
Backbone（全 standard conv，無 depthwise）：
  Conv 3×3 s2 → 128×128  (ch: 16)
  Conv 3×3 s2 → 64×64    (ch: 32)
  Conv 3×3 s1 → 64×64    (ch: 64)
  Conv 3×3 s2 → 32×32    (ch: 128)
  Conv 3×3 s1 → 32×32    (ch: 128)
  ≈ 0.8M params，~300M FLOPs

Decoder（bilinear upsample × 2 逐次，scale = 2）：
  Upsample 2× → 64×64 + Conv 1×1 (ch: 32)
  Upsample 2× → 128×128 + Conv 1×1 (ch: 16)
  Conv 1×1 → heatmap 1-ch

推算 latency（A15 ANE）：~5–8 ms
```

**為什麼不用 MobileNet 系列**：depthwise conv 在 CoreML ANE 上比 Metal GPU 慢 5×，用 standard conv 的小模型更適合 ANE 路徑。

**為什麼不用 transposed conv**：ANE 支援狀態未確認；bilinear upsample（scale ≤ 2）有明確的 `ANERuntimeEngine::upsample_kernel` 佐證。

**Pixel Shuffle 替代方案**：若需要更精確的 sub-pixel upsample，可用 2× bilinear + 1×1 conv；pixel shuffle 需要 Reshape+Permute 組合，不保證全程 ANE。

### 替代方案：FOMO 形狀（更保守）

```
MobileNetV2 α=0.35 backbone（depthwise）→ 跑 Metal GPU（不要 ANE）
stride-8 output → 直接在縮圖偵測 centroid，省去 decoder
輸入 192×192 → heatmap 24×24
latency 估計：TFLite Metal GPU ~5–8 ms（依 Raspberry Pi 4 @ 60 FPS 推算）
```
> 若接受走 GPU 路徑（非 ANE），FOMO @ 192×192 是更有文獻支撐的選項。

---

## 五、資料缺口聲明

以下數字**未找到 A15 公開直接 benchmark**：
- A15 ANE 跑 detection head + heatmap decoder 的 ms 數字
- transposed conv 在 ANE 的確切行為（支援 / fallback / 速度）
- 256×256 以上解析度 tiny detector 在 A15 ANE 的實測值

推算均基於：A14 YOLOv5s @ 192×320 ANE = 10 ms（最接近的 proxy）+ A15/A14 TOPS 比（×1.44）+ FLOPs 縮放。誤差範圍 ±50%，**結論只能是「邊緣可行」而非「確定可行」**。

**確認前建議**：用 `coremltools.models.MLModel.predict()` 配合 `MLModelConfiguration(computeUnits: .cpuAndNeuralEngine)` 在真機跑一次 prototype，並用 Instruments → CoreML 確認有無 CPU fallback 段。

---

## 來源列表

- [Photoroom CoreML 2022 iPhone 14 benchmark](https://www.photoroom.com/inside-photoroom/core-ml-performance-2022)
- [Photoroom CoreML 2023 iPhone 15 benchmark](https://www.photoroom.com/inside-photoroom/core-ml-performance-benchmark-2023-edition)
- [TildAlice TFLite vs CoreML iOS (A15 MobileNetV3 58.7ms)](https://tildalice.io/tflite-vs-coreml-ios-latency-benchmark/)
- [coremltools quantization perf (A16 MobileNetV2 0.48ms)](https://apple.github.io/coremltools/docs-guides/source/opt-quantization-perf.html)
- [hollance/neural-engine unsupported-layers](https://github.com/hollance/neural-engine/blob/master/docs/unsupported-layers.md)
- [hollance/neural-engine ane-vs-gpu](https://github.com/hollance/neural-engine/blob/master/docs/ane-vs-gpu.md)
- [Ultralytics YOLOv5 iOS speed table #1276](https://github.com/ultralytics/yolov5/issues/1276)
- [Ultralytics YOLOv5 Neural Engine opt #2526](https://github.com/ultralytics/yolov5/issues/2526)
- [Ultralytics YOLOv8 high latency mlpackage #6788](https://github.com/ultralytics/ultralytics/issues/6788)
- [Dluznevskij 2022 YOLOv5 iPhone benchmark](https://www.bjmc.lu.lv/fileadmin/user_upload/lu_portal/projekti/bjmc/Contents/9_3_07_Dluznevskij.pdf)
- [Roboflow best iOS detection models](https://blog.roboflow.com/best-ios-object-detection-models/)
- [TF Blog MoveNet edge devices](https://blog.tensorflow.org/2021/08/pose-estimation-and-classification-on-edge-devices-with-MoveNet-and-TensorFlow-Lite.html)
- [Google BlazePose blog](https://research.google/blog/on-device-real-time-body-pose-tracking-with-mediapipe-blazepose/)
- [CoreML iOS 17 Neural Engine regression #2004](https://github.com/apple/coremltools/issues/2004)
