# YOLO on Apple ANE Latency Check

**調查日期**: 2026-05-01  
**結論**: "1-5 ms FP16 YOLO on ANE" **是錯誤的** — 該數字來自 NVIDIA T4 GPU + TensorRT，與 iPhone ANE 無關。

---

## 實測數據彙整

### iPhone 上的 YOLO Core ML 真實測量值

| Model | Resolution | Quant | Device | Chip | Latency | Source |
|---|---|---|---|---|---|---|
| YOLOv5s | 320×192 | FP8 | iPhone 12 | A14 | **14.3 ms** | ultralytics/yolov5 #1276 |
| YOLOv5m | 320×192 | FP8 | iPhone 12 | A14 | 16.5 ms | ultralytics/yolov5 #1276 |
| YOLOv5l | 320×192 | FP8 | iPhone 12 | A14 | 21.0 ms | ultralytics/yolov5 #1276 |
| YOLOv5x | 320×192 | FP8 | iPhone 12 | A14 | 28.8 ms | ultralytics/yolov5 #1276 |
| YOLOv5s | 320×192 | FP8 | iPhone XR/XS | A12 | 22.3 ms | ultralytics/yolov5 #1276 |
| YOLOv5s | 320×192 | FP8 | iPhone 11 | A13 | 17.4 ms | ultralytics/yolov5 #1276 |
| YOLOv8n | 未知 | 未知 | iPhone 14 Pro | A16 | **15 ms** | tucan9389/ObjectDetection-CoreML |
| YOLOv8s | 未知 | 未知 | iPhone 14 Pro | A16 | 29 ms | tucan9389/ObjectDetection-CoreML |
| YOLOv8m | 未知 | 未知 | iPhone 14 Pro | A16 | 37 ms | tucan9389/ObjectDetection-CoreML |
| YOLOv8l | 未知 | 未知 | iPhone 14 Pro | A16 | 45 ms | tucan9389/ObjectDetection-CoreML |
| YOLOv8x-pose | 未知 | FP16 | 未知 | 未知 | **101 ms** (nano mlpackage) | ultralytics/ultralytics #6788 |
| YOLO11 (size unknown) | 未知 | 未知 | 未知 | 未知 | **~12 ms** (85 FPS) | Ultralytics blog (unsourced) |

**重要備注**:
- tucan9389 repo 的 YOLOv8n 15ms 未說明解析度（推測 640×640 FP16）
- "85 FPS = 11.8ms" 的 case study 未指明 iPhone 型號、YOLO 型號或解析度，無法驗證
- 公開可找到的最佳 iPhone Core ML YOLO 推論：YOLOv8n @ iPhone 14 Pro ≈ **15 ms**

### 被誤引的「1-5 ms」數字來源

| Model | 標稱 latency | 實際硬體 | 量化 | Source |
|---|---|---|---|---|
| YOLOv10-N | **1.84 ms** | **NVIDIA T4 GPU + TensorRT FP16** | FP16 TRT | arxiv 2405.14458，論文明確寫 "T4 GPU with TensorRT FP16" |
| YOLOv10-S | 2.49 ms | NVIDIA T4 GPU + TensorRT FP16 | FP16 TRT | 同上 |
| YOLOv10-M | 4.74 ms | NVIDIA T4 GPU + TensorRT FP16 | FP16 TRT | 同上 |
| YOLO11n | **1.5 ms** | **NVIDIA T4 GPU + TensorRT 10** | FP16 TRT | docs.ultralytics.com/models/yolo11 |
| YOLO11n | 2.4 ms | NVIDIA Titan Xp GPU | PyTorch | arxiv 2407.12040v7 |
| YOLO11n | 56.1 ms | CPU (ONNX) | FP32 | docs.ultralytics.com/models/yolo11 |

**這就是 claim 的根源**：YOLOv10 GitHub README 和 Ultralytics 文件的 latency table 是 T4 TensorRT 數字，但 README 未在表格旁標明硬體，讀者直接誤植為「YOLO 的推論速度」。

---

## 理論下限計算（ANE FP16）

```
YOLO11n FLOPs: 6.5 GFLOPs @ 640×640 (Ultralytics 官方)
YOLOv10-N FLOPs: ~6.7 GFLOPs (類似規模)

A15 ANE peak: 15.8 TOPS (INT8)
A15 ANE FP16 實效: 保守估 ~8 TFLOPS（TOPS 定義為 INT8 ops，FP16 約半速）
A17 ANE peak: ~35 TOPS (INT8) → FP16 實效 ~17 TFLOPS

理論最低延遲 (A15 FP16):
  6.5 GFLOPs / 8,000 GFLOPs = 0.81 ms  ← 純矩陣數學理論下限

實際 overhead 乘數：
  - Memory bandwidth 瓶頸（ANE 非 compute-bound for small models）
  - Layer boundary sync、compile overhead
  - NMS postprocessing（非 ANE 執行）
  - 典型 overhead: 10-20×

實際預估:
  A15 FP16: 0.81 ms × 15 = ~12 ms  （與 tucan9389 YOLOv8n 15ms 吻合）
  A15 INT8: ~6-8 ms（INT8 在 A15 有硬體支援）
  A17 FP16: 理論 ~5-7 ms（更快 ANE，但 memory BW 仍限制）
  A17 INT8: ~3-5 ms（A17 增加 int8-int8 throughput）
```

---

## 各情境可達性評估

| 情境 | 估計 latency | 可行? |
|---|---|---|
| YOLO11n 640px FP16, A15 (iPhone 13/14) | 12-20 ms | 50-80 FPS，不達 200 FPS |
| YOLO11n 320px FP16, A15 | 5-10 ms | 100-200 FPS，**邊緣可行** |
| YOLO11n 320px INT8, A17 Pro (iPhone 15 Pro) | **3-6 ms** | 160-330 FPS，理論上 5ms 可達 |
| YOLO11n 192px INT8, A17/A18 | **2-4 ms** | 250-500 FPS，5ms 預算內 |
| FP16 640px 任何 iPhone ANE | 12-50 ms | 20-80 FPS，不可行 |

---

## 結論

### 「1-5 ms FP16 YOLO on ANE」— 這個 claim 為偽

1. **數字來自 T4 TensorRT，不是 ANE**：YOLOv10 paper (arxiv 2405.14458) 明確寫 "T4 GPU with TensorRT FP16"；YOLO11 Ultralytics 文件的 latency table 同樣是 T4 TensorRT10。沒有任何 verifiable source 展示 iPhone ANE FP16 YOLO 達到 1-5 ms。

2. **已知最佳 iPhone ANE 實測值**：YOLOv8n @ iPhone 14 Pro (A16) ≈ **15 ms**（解析度未知，可能 640px）。YOLOv5s @ iPhone 12 (A14) @ 320×192 FP8 = 14.3 ms。

3. **FP16 640px 的真實範圍**：基於現有實測推估，現代 iPhone ANE FP16 @ 640px 的 YOLO nano 約 **12-20 ms**（50-80 FPS），不是 1-5 ms。

4. **什麼情境能達 5 ms 以內**：
   - 低解析度（192-320px）+ INT8 量化 + A17 Pro 以上（iPhone 15 Pro / 16 系列）
   - 這三個條件缺一不可
   - INT8 量化需要 A17 Pro 以上的硬體支援（A17 才加入 int8-int8 ANE 路徑）

### 對 200 fps (5 ms 預算) 球追蹤的可行性

**困難**，但邊緣可能：
- 640×640 FP16：**不可行**（15-20 ms）
- 320×320 FP16 A17+：**勉強**（8-12 ms，80-125 FPS）
- 192×320 INT8 A17+：**可能**（3-6 ms）但需要實機驗證，且 192px 解析度對快速小球可能 miss detection
- 備選方案：傳統 HSV+CC 在 iOS (現行架構) 已達 240 fps，成本幾乎為零；YOLO 的增益需要 offset 建模成本

**建議**：若要嘗試 YOLO 路徑，先在 iPhone 15 Pro (A17) 跑 YOLO11n 320px INT8 benchmark，確認實際 ms 再決定是否值得。不要相信任何未標明「iPhone + Core ML + 實機測量」的 YOLO latency 數字。

---

## 參考來源

- YOLOv10 paper benchmark hardware: https://arxiv.org/abs/2405.14458
- YOLO11 official latency table (T4): https://docs.ultralytics.com/models/yolo11/
- YOLOv5 iPhone iDetection speed table: https://github.com/ultralytics/yolov5/issues/1276
- YOLOv8 on iPhone 14 Pro (tucan9389): https://github.com/tucan9389/ObjectDetection-CoreML
- YOLOv8 high latency issue: https://github.com/ultralytics/ultralytics/issues/6788
- Apple coremltools quantization perf: https://apple.github.io/coremltools/docs-guides/source/opt-quantization-perf.html
- A17 Pro INT8 ANE support: https://apple.github.io/coremltools/docs-guides/source/opt-overview.html
- Fruitlet detection paper (Titan Xp benchmark): https://arxiv.org/html/2407.12040v7
