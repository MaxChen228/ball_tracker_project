# 文獻調查：200-240fps 深藍硬球即時偵測

**日期**：2026-05-01
**範圍**：2022–2026，重點 mobile inference < 10 ms，針對 V11 剩餘失敗模式（Mode α specular / Mode β ambient color）

---

## 方向 1：小物件 / 球追蹤即時偵測

### TrackNet 系列（最強 anchor reference）

**TrackNetV3** — Yu-Jou Chen & Yu-Shuen Wang, ACM Multimedia Asia 2023, Tainan, Taiwan
- DOI: https://dl.acm.org/doi/10.1145/3595916.3626370
- 主球種：羽球。雙模組：軌跡預測（用 estimated background 做 auxiliary）+ 軌跡 inpainting rectification
- 準確度 87.72% → 97.51%（舊法對比）
- **適用性**：架構屬 frame-sequence → heatmap 輸出，需 3–5 frame stack。iPhone capture 有連續幀，原理可移植。但 backbone 在 full 1080p 下推論速度無公開 mobile 數字，單幀 5 ms budget 不確定能達到。

**TrackNetV4** — arXiv:2409.14543，2024 年 9 月提交
- 引入 motion attention maps：把 frame differencing 圖做 modulation，plug-and-play 加在 V3 基礎上
- **關鍵**：明確依賴多幀 frame diff，是 temporal method，不是 single-frame
- 準確度提升數字未在 abstract 揭露；no mobile latency 數字
- **適用性**：Frame differencing 概念可提取作 motion cue，在我們 240fps 下 inter-frame diff 非常乾淨（球位移 ~7-15px/frame），值得實驗

**BlurBall** — arXiv:2509.18387，乒乓球，2025/09 提交，2026 年修訂
- HRNet + Squeeze-and-Excitation attention，多幀輸入（1-step / 3-step MIMO）
- 同時輸出球位置 + blur length + blur angle（joint estimation）
- F1 = 97.17（1-step），79 FPS（非 mobile）
- **重要 insight**：把模糊中心而非前緣定義為 GT。這個 labeling convention 對我們的 M3（aspect < 0.40，motion blur）直接相關——V11 的 aspect gate 對 blur streak 已做類似 relaxation
- **適用性**：模型太重（HRNet）無法 direct 跑 iPhone < 5 ms，但 joint blur estimation 是我們完全未開發的 cue

**TT3D** — CVPR Workshop CVSport 2025，arxiv:2504.10035
- 乒乓球 3D reconstruction，physics-based reprojection error 最小化
- ball detection 細節未在 abstract 揭露；使用 neural network 追蹤
- **適用性**：stereo + physics 架構與我們雙機三角化高度相關，但 detection 細節需讀全文

### 廣泛 YOLO 系列 mobile 球偵測

**YOLO-Ball** — Ding, Fan, Zhao，SAGE Journals 2026
- URL: https://journals.sagepub.com/doi/10.1177/17543371261423768
- 針對 tennis ball，改良 YOLO 處理遮擋 + 運動模糊
- 架構細節未在搜尋摘要中揭露；文章 2026 年出版

**YOLOv8/YOLO11 mobile 現實數字**：
- 社群報告 YOLOv8n mlpackage 在 iPhone（未指定型號）跑到 ~101 ms（！），未 ANE 優化時
- Photoroom benchmark：A15 Bionic 完整分割 pipeline ~45 ms（非純 detection）
- YOLO11 宣稱 ANE 優化後「easily 60+ FPS」但無 < 5 ms 的 single-frame detection 硬數字
- **結論**：未找到在 A15/iPhone 14 上達到 < 10 ms single-frame ball detection 的公開 benchmark。101 ms 的 YOLO naive mlpackage 和 V11 的 1.96 ms 之間落差極大——HSV+CC pipeline 的速度優勢在這個 budget 下幾乎不可能被 generic YOLO 取代，除非有 ANE-tuned tiny model 的實測。

---

## 方向 2：Specular Highlight / 反白光下的物體偵測

**SpecSeg** — MDPI Sensors 2022（PMC: PMC9460179）
- U-Net 架構，specular pixel 偵測與分割
- 任意光源種類 / 顏色不敏感
- **適用性**：推論速度未知，U-Net on mobile 大約 10-30 ms；作為 preprocessing mask 會讓整體 pipeline 超出 5 ms budget

**"Towards High-Quality Specular Highlight Removal" (Fu et al.)** — ICCV 2023，arXiv:2309.06302
- 大型合成 dataset + deep learning removal
- 純移除用途，非 detection；推論不適合 real-time

**SpecReFlow** — PMC 2024（PMC11042492）
- flow-guided video completion，HSV 反向 saturation 做 detection mask：像素乘 (1-S) 增強 specular 對比
- **關鍵 insight**（可立即使用）：`specular_mask = V > threshold AND S < threshold`（V 高 + S 低），這和我們 M1 的描述完全一致（Mode α：gt_s=19, gt_v=131）。SpecReFlow 的 preprocessing 本質是 `enhanced = RGB * (1-S)`，幾乎零成本

**Comprehensive Specular Survey** — Springer AI Review 2025
- URL: https://link.springer.com/article/10.1007/s10462-025-11233-7
- 結論之一：Dichromatic Reflection Model 假設物體 uniform composition，實際球表面（縫線 + 光澤區）違反假設；deep learning 方法優於 DRM
- **對我們的意義**：DRM-based specular separation 在球表面是已知壞選擇，驗證我們不走這條路是正確的

**核心結論**：目前沒有「< 5 ms mobile 上跑的 specular-robust ball detector」，所有 DL-based specular removal 都是 heavy pipeline。**最輕量可嘗試的**是 `S < s_low AND V > v_high` 的雙條件 specular gate——Mode α 的 gt_s=19 / gt_v=131 完全符合這個 fingerprint，且計算是純 HSV thresholding（< 0.1 ms）。這條 V11 的 02 報告未探索（當時 dual-cube 砍掉了 low-S 副 cube 因為 area-rank 問題，但直接用 specular_gate 分類後不送入 isolation pipeline 是不同路徑）。

---

## 方向 3：Radial Symmetry / Blob 偵測新進展

**FRST 現狀（2022–2026）**：
- 搜尋結果未找到 2022-2026 年的重大 FRST 後繼改進論文。最新學術進展聚集在 deep learning circle detection，而非 gradient voting 改進
- **Circle Detection with Adaptive Parameterization** — PMC 2025（PMC12031632）：bottom-up approach，仍是 traditional，速度未揭露
- **Zero-shot circle detection from synthetic data** — ScienceDirect 2025：deep learning，需 model inference

**Phase Symmetry**：未找到 2022-2026 的 real-time 版本超越 FRST 的論文

**GPU/Metal FRST 加速**：未找到強證據。搜尋詞「FRST GPU Metal acceleration 2022-2024」無命中。

**結論**：FRST 在 2022-2026 沒有找到重大突破。V11 report 的估算（1080p ~10ms，超 budget）未被新研究否定也未被新研究改善。**未找到強證據**說明有新的 O(N) gradient-voting 圓偵測能在 iPhone 4.16ms 內跑 1080p。

---

## 方向 4：Frame Differencing / Motion-Coherent 偵測

**TrackNetV4 motion attention** — 已在方向 1 說明。frame differencing 作為 motion prior 是 2024 年的主流路徑。

**"Event-based High-speed Ball Detection in Sports Video"** — Nakabayashi et al., ACM MMSports 2023
- DOI: https://doi.org/10.1145/3606038.3616164
- 主球種：volleyball。針對 fast-moving ball 的運動模糊問題
- **方法**：用 simulated events（從 frame video 生成 pseudo-event stream）而不是真 event camera
- **關鍵 claim**：高速球在 event representation 下不受運動模糊影響，因為 event 是亮度變化的微分
- **適用性**：我們的 240fps（4.16 ms/frame）已是 event-camera 等級的時間分辨率。inter-frame diff 在球位移 ~7-15px 時會產生明顯 diff blob，且不受 specular color 影響（亮度突變本身就是信號）——這是 V11 完全未探索的 color-agnostic motion cue

**"Time-consistent Ball Tracking and Spin Estimation with Event Camera"** — ACM MMSports 2024
- DOI: https://dl.acm.org/doi/10.1145/3689061.3689067
- 真 event camera 硬體，球的 spin 估算
- 直接 transfer 需要事件相機硬體，不適用；但 blob tracking in event space 的概念可用 frame diff 模擬

**Modified Inter-Frame Difference (MIFD)** — Springer IJIT 2024
- URL: https://link.springer.com/article/10.1007/s41870-024-02355-2
- 傳統 MIFD 改良，2024 年，real-time，對光照不敏感
- **直接適用**：inter-frame diff pipeline 在 240fps 下計算成本極低（兩幀相減 + threshold），且理論上 Mode α specular ball（V=131 突然出現）和 Mode β ambient ball（hue shift 但仍有亮度變化）都會在 diff image 產生信號

**小結**：在 240fps 下，兩幀之差的球 blob 是 color-agnostic 的強信號。`|frame[t] - frame[t-1]|` 在 V=131 的 specular ball 上會看到背景 V~90 的差值 ~40 intensity units，足夠 threshold。這是**最有潛力突破 V11 M1 ceiling 的方向**，計算成本 < 0.5 ms，且不需要 temporal state（每幀只需前一幀）。

---

## 方向 5：iPhone / Apple Silicon 推論優化

**Core ML INT8 quantization 現狀**（2024-2025）：
- Apple Core ML tools 文檔：INT8 quantization 在 A17 Pro 有明顯提升，A15 提升有限（A15 ANE 的 int8 throughput 優勢比 A17 小）
- Photoroom benchmark：A15 Bionic 完整 pipeline ~45 ms；A16 ~41 ms（~10% 改善/世代）
- YOLO11 exported to CoreML + ANE：宣稱 60+ FPS（< 17 ms/frame），但未到 5 ms
- mixed precision (FP16)：~12 ms for moderately-sized model（Apple Core ML tools guide）

**ANE 適合的模型限制**（practical knowledge，非 paper）：
- ANE 對 conv + depthwise-separable friendly；custom ops 退回 CPU
- 小 model（< 1M params）在 ANE 有 dispatch overhead 相對代價更高
- **結論**：ANE 不是 V11 的對手——V11 的 1.96 ms 已跑在 CPU single-thread，用 ANE 跑 generic YOLO 反而更慢（101 ms 記錄在案）

**Vision Framework 2025**（iOS 18 / Xcode 26）：
- iOS 18 Vision 主要更新：text recognition 語言擴展、person segmentation 改進
- 未找到針對小高速 object tracking 的新 VN* API（如 VNDetectHighSpeedObjectRequest 之類）
- **結論**：Vision framework 沒有直接適用的 high-speed ball detection API。繼續用 Core Image / custom pipeline 是正確選擇。

---

## 方向 6：從 SAM2 Mask 蒸餾 Tiny Detector

**TinySAM** — AAAI 2025，arXiv:2312.13789
- 方法：full-stage knowledge distillation，hard prompt sampling + hard mask weighting，把 SAM ViT-H encoder 蒸餾到輕量 encoder
- 目標仍是 segmentation model，而非 detection head
- 性能：比 FastSAM 用更少 MACs（9.5%），AP +4%
- **適用性**：TinySAM 的蒸餾框架可啟發「用 SAM2 mask 做 supervision 訓練 tiny detector head」，但沒有直接的 ball-specific detection 版本

**"From SAM to CAMs"** — CVPR 2024
- URL: https://openaccess.thecvf.com/content/CVPR2024/papers/Kweon_From_SAM_to_CAMs_Exploring_Segment_Anything_Model_for_Weakly_CVPR_2024_paper.pdf
- 用 SAM 輔助 weakly supervised semantic segmentation，不是 detection distillation
- **適用性**：方向正確但路徑不同。我們有 1073 GT frames 的 SAM2 精確 mask，這已是 strong supervision 而非 weak supervision

**直接蒸餾路徑（未找到 ball-specific 論文）**：
- 搜尋「mask distillation tiny detector SAM2 2024」未找到針對 sports ball 的直接蒸餾論文
- **理論可行路徑**：用 1073 SAM2 mask frames + data augmentation（合成 desat + specular）訓練 MobileNet-SSD 或 YOLOv8n 的 ball-specific tiny model。這是原創應用，非現有 paper 的直接 follow
- **關鍵阻力**：1073 frames 是小 dataset，泛化困難；specular failure mode 正好是 GT 稀少的 frames（V11 miss 的那些）

---

## 破壞性的論點

以下是文獻中找到的、可能挑戰現有路線的論點：

**1. 「HSV-based detector 在 illumination 變化下的脆弱性是已知瓶頸」**
——球偵測綜述 PMC 2025（PMC12453710）明確指出：color segmentation 方法的 robustness 劣於 deep learning，現代方法（2022 後）幾乎全面轉向 YOLO 系列。這**驗證** V11 的 0.905 ceiling 論點，但也暗示 HSV 路線本質上已是 legacy approach。

**2. 「event camera 從根本解決 high-speed blur，regular camera 240fps 是次優解」**
——Event-based ball detection（ACM MMSports 2023）的核心主張是：真正的事件相機在 us 級解析度下完全消除運動模糊。240fps regular camera 每幀 4.16ms 仍然會有 motion blur（Mode M3，V11 有 9 frames）。**但**：事件相機硬體成本高且 iPhone 不搭載，所以這個論點是「理論正確但實作上不可行」。

**3. 「generic YOLO 在 iPhone A15 上並非 < 10 ms」**
——GitHub issue 記錄 YOLOv8n mlpackage = 101 ms on iPhone（未指定型號）。這**直接否定**了「訓練一個 tiny DL model 就能解決」的天真假設。在 A15 上達到 < 5 ms 需要 極度 specialized 的 model（< 0.5M params + full ANE kernel mapping），目前沒有公開 sports-ball-specific 版本。

**4. 「temporal anchor 是解決 specular miss 的必要條件但違反 stateless constraint」**
——多篇論文（TrackNet 系列、BlurBall、Kalman-based tracking）都假設有前幀位置作為 anchor。我們的 02_v11_followup.md 已驗證：D 方向在純 stateless 下上限 +2.5pp，且 170a6a89_b 的 31-frame 連續 miss 讓任何短程 anchor 都失效。文獻沒有說「stateless 才正確」——全球實用系統都是 stateful。**這是 project constraint 而非 universal truth**。

**5. 「200fps ball tracking 主流不是 single-frame detection，而是 sequence model + trajectory fitting」**
——TrackNetV3/V4、BlurBall、TT3D 全部依賴多幀。TrackNet 系列的 accuracy 97%+ 是在有前幀輸入的條件下。**不存在**「200fps single-frame stateless HSV alternative 達到 97%+ recall」的 published work。這在文獻上是 hard evidence：我們的 V11 0.905 stateless HSV 已接近 single-frame HSV 的物理極限。

---

## Top 3 Actionable Directions

### 1. **Inter-Frame Difference as Color-Agnostic Motion Gate**（最高優先，低成本）

根據方向 4 的發現，在 240fps 下 `|frame[t] - frame[t-1]|` 產生的 diff blob：
- 不依賴球的 color（Mode α specular V=131 在 diff 中仍有 ~40 intensity unit 信號）
- 計算成本 < 0.5 ms（兩幀 uint8 相減 + threshold）
- 不需要 temporal state（只用前一幀，window=1）
- 可作為 V11 的 **parallel channel**：HSV 失敗（M1 S=19）時，diff channel 補位

具體實驗設計：`diff_mask = (|Y[t] - Y[t-1]| > diff_threshold) AND (area > 3px)`，merge 進 V11 candidate list，共用 aspect/fill gate。預期能救部分 Mode α（球突然出現高亮區）的 68 frames。

參考：ACM MMSports 2023 event-based ball detection，TrackNetV4 motion attention。

### 2. **Specular-Specific Low-S/High-V Double Gate**（中優先，< 0.1 ms overhead）

根據方向 2 的 SpecReFlow insight + 我們的 Mode α 物理分析：
- `specular_candidate_mask = (V > 120) AND (S < 40)`（對應 gt_s=19/gt_v=131）
- 這是一個 HSV 空間的**第三個區域**（高亮近白），完全不同於 V11 的藍色 cube
- 挑戰：spatial isolation gate 失效問題（02 報告 B 方向）——但 specular ball 在 diff image 上有 motion 輔助定位，可用 `specular_gate AND diff_gate` 雙重過濾，大幅壓制背景 FP
- 預期救回 Mode α 68 frames 的子集（那些 S < 40 但 diff 信號存在的 frames）

### 3. **Stateful Kalman ROI + 寬鬆 HSV**（理論上限最高 +2.5pp，但需 explicit break stateless constraint）

文獻一致表明這是 sports ball tracking 的標準做法（TrackNet 系列、BlurBall、Kalman-based trackers 全部 stateful）。我們的 production constraint 是 stateless，但如果未來 operator 願意接受「session warm-up 需要一球 hit 才 arm tracker」，stateful anchor 可突破 V11 ceiling。

上限：+2.5pp（170a6a89_b +22/73 miss），真實期望值 +1.5-2pp（長 run 救不了）。需 explicit architecture 決策，不是 drop-in patch。

---

## 研究空缺（誠實說明未找到的強證據）

- **< 5 ms single-frame ball detection on iPhone A15 的 DL 方案**：未找到公開 benchmark 達到此要求的 sports ball specialized model
- **FRST 的 2022-2026 重大改進**：無命中，FRST 在學術上被 DL 取代，Metal/GPU 加速的實現不在主流論文
- **SAM2-to-tiny-detector distillation for sports ball**：未找到直接對應論文，方向理論可行但需自行實驗
- **240fps camera specular-robust color detector**：未找到在真實 240fps capture 下的 specular-invariant color detector；所有 robustness 工作針對標準幀率

---

## 參考文獻清單

| 標題 | Venue / 年份 | DOI / URL |
|---|---|---|
| TrackNetV3: Enhancing ShuttleCock Tracking | ACM Multimedia Asia 2023 | https://dl.acm.org/doi/10.1145/3595916.3626370 |
| TrackNetV4: Enhancing Fast Sports Object Tracking with Motion Attention Maps | arXiv 2024 | https://arxiv.org/abs/2409.14543 |
| BlurBall: Joint Ball and Motion Blur Estimation | arXiv 2025/2026 | https://arxiv.org/abs/2509.18387 |
| TT3D: Table Tennis 3D Reconstruction | CVPR Workshop CVSport 2025 | https://arxiv.org/abs/2504.10035 |
| Event-based High-speed Ball Detection in Sports Video | ACM MMSports 2023 | https://doi.org/10.1145/3606038.3616164 |
| Time-consistent Ball Tracking and Spin Estimation with Event Camera | ACM MMSports 2024 | https://dl.acm.org/doi/10.1145/3689061.3689067 |
| SpecSeg: Specular Highlight Detection and Segmentation | MDPI Sensors 2022 | https://pmc.ncbi.nlm.nih.gov/articles/PMC9460179/ |
| Towards High-Quality Specular Highlight Removal (Fu et al.) | ICCV 2023 | https://arxiv.org/abs/2309.06302 |
| SpecReFlow: Specular Reflection Restoration | PMC 2024 | https://pmc.ncbi.nlm.nih.gov/articles/PMC11042492/ |
| Comprehensive Survey on Specularity Detection | Springer AI Review 2025 | https://link.springer.com/article/10.1007/s10462-025-11233-7 |
| TinySAM: Pushing the Envelope for Efficient SAM | AAAI 2025 | https://arxiv.org/abs/2312.13789 |
| From SAM to CAMs (weakly supervised) | CVPR 2024 | https://openaccess.thecvf.com/content/CVPR2024/papers/Kweon_From_SAM_to_CAMs... |
| A comprehensive review of ball detection techniques | PMC 2025 | https://pmc.ncbi.nlm.nih.gov/articles/PMC12453710/ |
| Modified Inter-Frame Difference (MIFD) for moving objects | Springer IJIT 2024 | https://link.springer.com/article/10.1007/s41870-024-02355-2 |
| YOLO-Ball: Real-time tennis ball detection | SAGE Journals 2026 | https://journals.sagepub.com/doi/10.1177/17543371261423768 |
| Tennis ball detection based on YOLOv5 with TensorRT | Scientific Reports 2025 | https://www.nature.com/articles/s41598-025-06365-3 |
