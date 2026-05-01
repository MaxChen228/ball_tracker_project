# 球體偵測 pre-DL 演算法研究 — 最終報告

## 1. 研究問題

iOS 240fps（4.16 ms/frame budget on A15+）下對深藍硬球的逐幀偵測。
goal：盡量抓到每幀，雜訊可由下游軌跡層處理。**不是 production
ship**，是研究現役 pipeline 的瓶頸與可達上限。

## 2. 資料

`lab/standalone_workspace/items/` 內 SAM2 propagate 標註。

- 8 sessions in manifest，**排除 1 個壞 session** (`session_s_21af9a82_b`，
  只有 1 frame)
- 剩 7 sessions / **931 GT frames**（每張 1080×1920 binary mask）
- 總計 ~50 萬 ball pixel + ~250 萬 bg pixel（環形 sample）
- SAM2 mask 品質審查：74/931 frames (7.9%) 標 suspect
  （area > 3× session-median ∨ aspect < 0.4 ∨ fill < 0.45 ∨ multi-CC）
  → 下游 robust check 對有/無 suspect 結論不變

樣本侷限：6 個獨立 pitch event，連續 frame 高度相關，有效獨立樣本約
40-80 個決策點。macro 結論強，個別 session 大幅 Δ 統計力中等。

## 3. Production 系統的真實流程（讀 code 確認）

| 元件 | 行為 |
|---|---|
| `ball_tracker/BallDetector.mm` | iOS emit **所有** 通過 area≥20 ∧ aspect≥0.75 ∧ fill≥0.55 的 blob，按 area desc，**沒有 winner pick** |
| WS schema | 整 candidates list 送 server，無 top-K cap |
| `server/live_pairing.py:_resolve_candidates` | winner pick 寫進 `frame.px/py` scalar，**只給 dashboard 顯示** |
| `server/live_pairing.py:ingest` | `triangulate_pair` 對 `frame_a.candidates × frame_b.candidates` 全配對 |
| `server/pairing.py` | 物理 gate (`gap_threshold_m` / Y-residual) 過濾三角化 outlier |

**正確的 production recall metric** = per-frame 至少有一 candidate 距 GT
centroid ≤ TOL（即「球進入三角化 pool」）。**不是** scalar winner 的距離。
此前用錯指標導致初版報告誤判（已修正，archive 在 `_archive/`）。

## 4. PROD vs V10 主結果

### V10 配置

| 參數 | PROD | V10 | Δ 重要性（單軸從 PROD 改）|
|---|---|---|---|
| HSV.H | [105, 112] | [103, 118] | +4.6pp |
| HSV.S | [140, 255] | [120, 255] | +5.8pp |
| HSV.V | [40, 255] | [30, 255] | -0.5pp（無效，可砍）|
| aspect_min | 0.75 | 0.50 | **+7.1pp** ← 最大單一貢獻 |
| fill_min | 0.55 | 0.35 | +2.1pp |
| min_area | 20 | 5 | +0.8pp |

### 三層 metric 驗證 robustness

| Metric | n | PROD | V10 | Δ |
|---|---|---|---|---|
| centroid distance ≤ 10px (all) | 931 | 0.687 | 0.868 | **+18.1pp** |
| IoU(detector_CC, GT_mask) ≥ 0.3 | 931 | 0.615 | 0.768 | +15.3pp |
| IoU ≥ 0.5 | 931 | 0.454 | 0.623 | +16.9pp |
| centroid ≤ 10px (clean only) | 857 | 0.683 | 0.870 | +18.7pp |
| IoU ≥ 0.3 (clean only) | 857 | 0.602 | 0.761 | +15.9pp |
| centroid ≤ max(10, 0.5×r_GT) (clean) | 857 | 0.686 | **0.874** | **+18.8pp** |

6 種 metric 變體下結論完全一致。

### Per-session（clean frames, adaptive tol）

| session | n_clean | PROD | V10 | Δ |
|---|---|---|---|---|
| 16ec069a_b | 227 | 0.885 | **0.974** | +0.088 |
| 170a6a89_a | 102 | 0.873 | **0.980** | +0.108 |
| 170a6a89_b | 239 | 0.389 | **0.669** | +0.280 |
| 21af9a82_a | 69  | 0.290 | **0.797** | **+0.507** |
| 22d1835e_a | 53  | 0.981 | 0.981 | 0 |
| 22d1835e_b | 70  | 0.900 | **0.986** | +0.086 |
| 2546618f_a | 97  | 0.722 | **0.948** | +0.227 |

**V10 在 7/7 session 都不輸 PROD**（最差持平 0.981）。

## 5. Variant sweep — V10 為什麼是 sweet spot

| Pipeline | R_emit | nc_p50 | nc_p95 | per-session 退步 |
|---|---|---|---|---|
| V0 PROD | 0.687 | 1 | 3 | (baseline) |
| V1 wide HSV[100,125], no gate | 0.911 | 121 | 147 | **22d1835e_b -19.5pp** |
| V2 wide + motion gate | 0.895 | 25 | 116 | -19.5pp |
| V3 wide + motion + soft gate | 0.870 | 10 | 43 | -20.8pp |
| V8 narrow + motion + soft | 0.776 | 1 | 8 | (none) |
| V9 mid + motion + soft | 0.833 | 3 | 17 | (none) |
| **V10 mid + soft (no motion)** | **0.868** | 18 | 28 | **0** |

### Wide HSV 失敗在哪

22d1835e_b session：wide H[100,125] 把 ball 跟附近藍色 clutter 合成大 blob，
centroid 偏移 → -19.5pp。Mid HSV [103,118] 避開這問題。**HSV 寬度
不是越寬越好**，過寬會觸發 spatial merge。

### Motion gate 為什麼不採用

3-frame diff motion gate 對 V10 mid HSV 有 -3.5pp 副作用（慢球幀誤殺）。
mid HSV 已經把 static clutter 控制住，motion gate 變成淨負貢獻。
（Wide HSV 下 motion gate 才有意義，因為 wide HSV 大量吃進靜態
clutter 需要 motion 殺掉，但 wide 本身不採用。）

## 6. V10 殘留 13% miss 的機制歸因

111 個 miss frames：

| Mode | n | % of miss | 性質 |
|---|---|---|---|
| **M1 HSV cube 在 GT 區零像素** | 64 | 57.7% | ball 整個跑出 cube |
| **M2 HSV 抓到但無 ≥5px CC** | 25 | 22.5% | 像素過於碎裂 |
| M3 CC aspect < 0.50 | 14 | 12.6% | motion blur / 拉長 |
| M4 CC fill < 0.35 | 1 | 0.9% | 邊緣 case |
| M5 centroid drift > 10px | 7 | 6.3% | merge 鄰近 clutter |

**80% 的 miss 在 HSV/CC 上游**，下游 shape gate 只佔 ~14%。

### Saturation 是真兇

| | S_mean p10 | p50 | p90 | V_mean p50 |
|---|---|---|---|---|
| HIT (746 frames) | 85 | 138 | 174 | 105 |
| MISS (111 frames) | 13 | **54** | 97 | 120 |
| M1 only (64 frames) | 12 | **47** | 56 | 122 |

Miss frame 的球**飽和度中位數 = 47**（V10 cube 下限 = 120），完全在 S 軸
下方。V_mean 反而比 hit frame 高 → 這些是球**過度受光、變灰白色高光點**
的 frames。Hue 沒漂（p50 100 vs 106），單純是 saturation 崩。

**2 個 session 吃掉 84% 的 miss**（170a6a89_b 79 + 21af9a82_a 14）。
其他 5 個 session 平均 recall ≈ 96%。

## 7. 拿掉 S 限制反而毀掉一切

直覺：如果 S 是兇手，拿掉它應該救 desaturated ball。

實測：

| Variant | R_emit | nc_p50 | nc_p95 |
|---|---|---|---|
| V10 baseline | **0.870** | 18 | 29 |
| V10 + hue only (S≥0, V≥0) | **0.637** | 113 | 147 |
| hue only + 無 shape gate | 0.664 | 230 | 293 |

Per-session：

| session | V10 | hue-only | Δ |
|---|---|---|---|
| 22d1835e_b | 0.986 | **0.071** | **-91.4pp** |
| 22d1835e_a | 0.981 | 0.623 | -35.8pp |
| 21af9a82_a | 0.797 | 0.449 | -34.8pp |
| 2546618f_a | 0.928 | 0.608 | -32.0pp |
| 170a6a89_a | 0.971 | 0.686 | -28.4pp |
| 16ec069a_b | 0.974 | 0.789 | -18.5pp |
| 170a6a89_b | 0.669 | 0.707 | **+3.8pp** |

**只 1 個 session 受益**（170a6a89_b 那個本來最 desaturated 的），其他 6 個
災難性崩盤。

### 機制

拿掉 S/V 後 mask 吞下：淺藍背景、灰色物（hue 不穩定但落在藍範圍）、
陰影區（low V，hue 雜訊）。Candidate 從 18 → 113。**真球的 CC 跟附近
hue-similar 區域 merge 成大 blob**（M5 centroid drift），centroid 飄掉 → miss。

22d1835e_b -91pp 極端：那 session 背景大概有大片淺藍區（牆 / 地板？），
球幾乎每幀都被吞掉。

### 真正的 insight

**S_min 不是 "ball gate"，是 "spatial isolation gate"**。

- 它的功能不是辨識 ball，是**隔離 ball 與背景**
- 拿掉 S → mask 連通到背景 → CC merge → 失去 ball boundary
- HSV cube 的三軸**不能獨立放寬**，三軸是 entangled 的隔離機制

## 8. 否決掉的方向（負面結果）

| 思路 | 結果 |
|---|---|
| Lab a*b* Mahalanobis 取代 HSV | LOSO macro AUC 0.886 vs HSV 0.898，Lab 略輸 |
| 全寬 HSV cube H[100,125] | 22d1835e_b 退步 -19pp（spatial merge）|
| Three-frame motion gate | 對 V10 -3.5pp（慢球幀誤殺）|
| Morph CLOSE 5×5 | 對 V10 不必要（mid HSV mask 已不碎裂）|
| Log-Gaussian size prior + winner pick | production 不用 winner，無意義 |
| RANSAC ballistic trajectory（單檔全程 fit）| 收斂失敗，需 sliding-window + tracklet linking |
| Hue only（拿掉 S/V）| 6/7 session 崩盤 |

## 9. 突破 V10 上限需要的方向（未做）

V10 的 13% upper bound 的根因是 saturation，**HSV cube 怎麼調都救不了**
（飽和度已與背景重疊）。

理論上能突破的方向：

1. **Temporal ROI tracking**：球進 desaturation 短暫期間，從 prev 軌跡
   anchor 一個小 ROI，僅在 ROI 內放寬 S
2. **Local-contrast normalization**：對 frame 做 local normalization 後再
   inRange，減少 highlight 的飽和崩潰
3. **二層 cube fallback**：主 cube V10 + 副 cube (low-S, high-V)，**只在
   主 cube 為空時** fallback — 避免 background 噪音吞噬 ball
4. **Multi-cue gate**：除 HSV 外加 spatial gradient / radial symmetry
   (FRST 2003)，降低對單一色彩通道依賴
5. **Sliding-window RANSAC + tracklet linking**：跨幀軌跡 + 物理約束
   過濾，恢復個別 frame miss

## 10. Latency

- `lab-research/scripts/ball_detector.py --bench`：240 張真實 session frame
  on Mac M-class CPU 單 thread → **2.57 ms/frame** @ 1080p，21
  cands/frame
- iPhone 14 (A15) 單核 ≈ M1 0.85× → 預期 **~3.0 ms**
- 240fps budget 4.16ms，留 28% margin

## 11. Files

```
lab-research/
├── notes/
│   └── 01_final_report.md            ← 本檔
├── scripts/
│   ├── ball_detector.py              V10 reference 實作 (純 cv2)
│   ├── 01_sample_pixels.py           pixel sampling base building block
│   ├── 02_head_to_head.py            PROD vs V10 head-to-head
│   ├── 03_variant_sweep.py           8 variant sweep + per-session
│   ├── 04_mask_quality_audit.py      SAM2 mask 品質 audit
│   ├── 05_robust_eval.py             3-layer metric robust eval
│   ├── 06_ablation.py                +18.8pp 各參數貢獻拆解
│   ├── 07_failure_modes.py           V10 殘留 13% miss 機制歸因
│   ├── 08_hue_only.py                拿掉 S/V 限制的反直覺實驗
│   └── _archive/                     探索期 dead-end (RANSAC, Lab, motion gate)
└── outputs/
    ├── pixel_samples.npz             section 4 用，500k+250k pixel
    ├── head_to_head.npz              02 結果
    ├── pipeline_bottleneck.npz       early bottleneck (尚保留)
    ├── loose_pipeline.npz            early loose (尚保留)
    ├── motion_gated.npz              motion gate 結果（已否決）
    └── separability.npz              Lab vs HSV（已否決）
```

## 12. 結論濃縮

對深藍硬球：
- **PROD 上限 R = 0.687**，瓶頸在 strict shape gate（aspect 0.75）
- **V10 上限 R = 0.874**（+18.8pp，clean+adaptive tol metric），瓶頸在
  saturation 崩潰的 high-V highlight frames
- **V10 上限不可能用 HSV cube 突破**，需要 temporal / spatial / multi-cue
  方法
- **三維 HSV cube 的軸不可獨立**，S 是 spatial isolation gate 不是 color gate
- 計算成本 ~3.0 ms 在 iPhone 14 等級單核，全程純 OpenCV，無 ML
