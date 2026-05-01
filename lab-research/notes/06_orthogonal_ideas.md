# 06 — Orthogonal ideas（藍圖外的新方向）

接續 [`01_final_report.md`](01_final_report.md)、[`02_v11_followup.md`](02_v11_followup.md)。
V11 R=0.905 的剩 9.5% miss 有 86% 集中在兩個 desat-dominant session，
**Mode α specular**（球反白）+ **Mode β ambient color**（球反綠/灰）為
真兇。HSV cube、CLAHE、二層 fallback、Lab、hue-only 全已否決；FRST /
tiny FCN / dual pipeline 已派員探索。本檔提**現有藍圖之外**的方向。

所有 cost 估均 (rough estimate)。Budget reference：iPhone 14 A15
單核 4.16 ms / frame（240fps），V11 已用掉 ~2.3 ms，剩 ~1.8 ms 給新 cue。

---

## I1 — Stereo epipolar prior（A↔B 互為 spatial gate）

**一句話**：cam A 的 V11 candidate 投影到 cam B 的 epipolar line 上，B
端只在這條 line ±N px 內放寬 V11 的 S/V 門檻；反之亦然。

**直覺**：當前 V11 在 A、B 各自獨立跑，兩台 cam 各自 10% miss 是
*互不相關* 的（不同 viewing angle 看到不同 specular highlight）。若 cam A
看見球（hit），cam B 即使 desat 也只需要證明「epipolar line 上某處有可疑
blob」，搜尋空間從 1080p 整張縮到 ~1920×20 px。在這條窄帶上 S_min 可放
到 30、area 放到 1，spatial isolation gate 鬆掉也不爆 candidate（因為
搜尋面積已縮 100×）。

**Cost**：F-matrix 投影 + ROI inRange ~0.3 ms / frame。需要 A、B 時間軸
already aligned ±1.67 ms ✓。

**直擊**：α + β 都吃。Specular highlight 是 viewpoint-dependent，A 出
現 specular 時 B 不太可能同時 specular（除非光源恰好 bisect 兩相機）。

**風險**：(a) A、B 同時 miss 的 frame 救不到（看 170a6a89_b 那 31 frame
連續 miss 是否兩台都 miss — 未驗證，需要拉 cam A pair 的 GT）；
(b) F-matrix 來自 calibration，calibration 漂掉會送錯 prior；(c) WS
roundtrip 延遲讓 live 路徑做不到 frame-synced cross-validation —
**這條只能在 server post 路徑落地**，不解 live。

---

## I2 — Inter-frame difference accumulation（240fps 的免費信號）

**一句話**：240fps 下球每幀位移 ~50-300 px 而 bg 完全靜止，`|frame_t -
frame_{t-1}|` 的 thresholded diff 幾乎是球的剪影，folding 進 V11 mask
作 OR 補洞或 AND gate。

**直覺**：02 報告否決過「3-frame motion gate」(-3.5pp)，但那是用 motion
**做門檻砍 V11 candidate**。這裡是反過來——motion mask 當**擴張源**，
desat ball 在 V11 hsv mask 是空集合（M1 frame），但在 frame diff 上
仍是高對比 silhouette。把 motion blob 的 area/aspect 餵 shape gate 即可。
Static clutter 不會被 motion mask 救起，spatial isolation gate 自然
保持。Key insight：240fps 連續幀位移大於球直徑 → t 跟 t-1 的球**完全不
overlap**，diff 出來是 *兩個球輪廓相減* 的雙峰，挑面積較小那團（current
frame side）就是球的位置。

**Cost**：absdiff + threshold + CC ~0.5 ms / frame，ring buffer 1 frame
ok（~6 MB at 1080p YUV）。可在 iOS 端做。

**直擊**：α 主要受惠（specular 球邊緣對 bg 仍高對比）。β 部分受惠（球反
環境色但仍有 luminance edge）。

**風險**：(a) cam shake / handheld → diff 整張亮，需先對 bg static
points 做 affine align（但這 setup 是 tripod，可能不需要）；(b) 球進
入靜止點短暫剎那（top of arc, ~3-5 frame）motion 為零 — 但這通常是
HSV 還能抓的 frame，互補 OK；(c) 與 V11 candidate 大量重複 → 需要
dedup before sending。

---

## I3 — Top-down ballistic prior 反向 feed live ROI

**一句話**：post 路徑跑完 ballistic LSQ fit 得到該 pitch 的 g/v0/launch
point，**下次** pitch arm 時把這條軌跡 reproject 進 live frame 當 prior
ROI（不是當前 pitch 自己救自己）。

**直覺**：使用者多半連續投同類型球（相同投手、相同距離），第 N 球的
trajectory 是第 N+1 球的 strong prior。live 路徑在 ROI 內可放寬 S/V
（spatial isolation 已由 ROI 提供，不需 S 撐）。即使 prior 偏 100px
也比沒有強——ROI 取 prior ±200px 仍只有原圖 5%。

**Cost**：post 路徑反正本來要 fit；live 路徑在 ROI 內額外跑寬 cube ~0.5 ms。

**直擊**：β 為主（穩定環境的環境色反射在 pitch 間相似）。α 部分（specular
位置依賴光源幾何，多球相似）。

**風險**：(a) 第一球無 prior — cold start 退化成 V11；(b) 投手換邊 / 換
動作 prior 整個錯，需 outlier rejection（前 K 球軌跡 variance 太大就
丟棄 prior）；(c) 違反 stateless？— 不違反 frame-level stateless，這是
**session-level prior**，跟 calibration 同層。

---

## I4 — Defocus signature as depth prior（240fps binned 1080p 的副產品）

**一句話**：iPhone 14 main cam 240fps 強制 2×2 pixel binning，binned
sensor 在 near-field 球的 defocus 比 non-binned 大一倍，球 edge 的 PSF
寬度反映 ball-to-camera 距離。

**直覺**：問題改寫成「給定 candidate blob，它是球（合理 defocus）還是
背景物體（in-focus, sharp edge）？」。對 V11 candidates 算 edge gradient
magnitude p90 / blob radius，ball 在 5-15 m 距離應有 *特定* defocus
profile（可離線從 GT 學一條 curve）；牆 / 地板 in-focus 的 candidate
邊緣銳得多 → 一個 cheap shape-orthogonal cue。

**Cost**：Sobel on candidate ROI（~30×30）~0.1 ms / candidate × 30 cands ≈ 3
ms — **超 budget**。改成只對 V11 borderline candidate（aspect 0.4-0.5 或
fill 0.35-0.45）跑，<5 cands/frame → 0.5 ms 可行。

**直擊**：β 弱、α 不直接（specular 反而讓 edge 假性銳利）。**真正用途
是降誤殺**：當前 aspect 0.4 是怕誤殺，有了 defocus orthogonal cue 後
可以更激進地放 aspect 到 0.3 而不爆 false positive。

**風險**：(a) defocus profile 隨光照變動（高光 frame edge gradient 偽銳）；
(b) binned PSF 有 sub-pixel artifact 需要實測 calibrate；(c) 這條更像
「擴展 V11 op range」而非直擊 desat 上限——上限改善估 +0.5pp。

---

## I5 — Swap cascade order: ML ROI proposer → V11 inside ROI

**一句話**：顛倒目前「V11 主、ML 補」的 cascade。改成 tiny model
（mobilenet-style classifier 32×32 sliding 或 1-anchor SSD）只輸出
「球可能在這附近」的 ROI proposals，再把 V11 的全套色彩 + shape gate
跑在 ROI 內。

**直覺**：02 報告否決 hue-only 與低 S 是因為「frame-wide」放寬會撞背景。
但若 ROI 已由 ML model 縮到 50×50，那麼**在這個 50×50 內 hue-only 都
不會跟背景 merge**——因為 ROI 邊界本身就是 spatial isolation。ML 的角色
不是「detect」，是「localize a region where color rules can be relaxed」。
這比 tiny FCN distillation 容易訓（label 是 ROI 不是 mask），且 inference
極輕（input 縮到 192×108）。

**Cost**：96-feature MobileNetV3-small ~ 0.5-1.5 ms 在 A15 NPU（rough
estimate, Core ML benchmark 數量級）；V11 在 ROI 內跑 ~0.3 ms。Total
~2 ms，仍在 budget。

**直擊**：α + β。ML 看 luminance edge + spatial context，不依賴 chroma。

**風險**：(a) tiny FCN distillation 已派員，這條跟它高度重疊但**輸出
維度不同**（FCN 輸出 mask，proposer 輸出 ROI）；簡單度上 proposer 贏；
(b) 兩個 desat session 的 ML training data 必須充足，否則學到的是 V11
distribution 一樣 desat 死；(c) 顛倒 cascade 讓 V11 失敗時整條死——需要
fallback 路徑。

---

## I6 — Polarization / specular suppression at capture（不是 algorithm 是 stack 改造）

**一句話**：在 iPhone 主鏡前夾 circular polarizer (CPL) 砍直接反射的
specular component，球的本色 chroma 回來。

**直覺**：Mode α 的物理是 Fresnel 反射的 polarized light。CPL 旋到正確
角度可砍 ~80% 的 surface reflection 而保留 ball body 的 diffuse Lambertian
return。球的 deep blue 飽和度直接從 47 拉回 100+ → V11 cube 直接命中。
這條繞過所有 algorithm 限制，從問題根源動手。

**Cost**：~$30 magnetic CPL filter，losing ~1.5 EV（240fps 是否還夠光？
iPhone 14 240fps 已強制 1/1000s 上限快門 + 高 ISO，再 -1.5 EV 可能 noise
floor 撞牆需驗）。**Algorithm 端零 cost**。

**直擊**：α 直接 nuke。β 部分（環境色反射有 partial polarization 但
較弱）。

**風險**：(a) 240fps + CPL 光不夠 → noise 上升反而 V11 失效；
(b) outdoor 場景 sky polarization 與 ball polarization 互動複雜；
(c) 兩台 iPhone 需各夾一片；(d) 02 報告 4.5 已列「CPL filter」於
capture-side suggestions 但只一行——這條值得獨立跑一場 capture 實驗
量化 S 改善。**最高 ROI 候選之一**。

---

## I7 — Re-define problem: 從「detect every frame」改成「detect every
pitch event」

**一句話**：放棄逐幀 recall 目標。改成「給定一個 pitch 240fps × 1.5s =
360 frame，只要有 ≥30% frame 偵到 + 軌跡能 fit，就算成功」，把剩 9.5%
miss 重新定義成 *acceptable*。

**直覺**：使用者真實需求是估球速 + 軌跡，不是逐幀打卡。已知 ballistic
fit 在 10+ point 即收斂；240fps 下 1.5s pitch 有 360 frame，V11 0.905
= 326 hits >> 10。**已經夠了**。剩餘 desat frame 的「miss」對下游沒
影響。研究方向應該轉向：把 saved budget（V11 的 45% 剩餘 + 不再追求
recall）投資到 (a) sub-pixel centroid refinement、(b) 軌跡 outlier
robustness、(c) pitch event boundary detection。

**Cost**：零（這是策略改變）。

**直擊**：都不直擊——它是承認 α/β 物理上不可救而轉移火力。

**風險**：(a) 使用者可能不接受「研究上限就到這」的結論；(b) 連續 31
frame run 仍可能讓 fit 失敗（整段球進入 deep desat）— 需驗證 fit
robustness 在 worst run 是否仍工作。**這是 meta-direction，搭配其他
methods 用**。

---

## I8 — Event camera emulation: temporal contrast on raw NV12 Y plane

**一句話**：NV12 Y plane 直取 + per-pixel `|Y_t - Y_{t-1}| > θ` 產出
binary event map，球邊緣是強 event source，bg 完全靜（同 I2 但 sub-frame
精度 + 純 luminance 不依賴 chroma）。

**直覺**：I2 已提類似想法但仍用 BGR diff。這條更激進：完全不過 BGR 轉
換管線，直接在 capture 出來的 YUV420 Y plane（亮度單通道）做 diff，
luminance edge 在 desat 球上也很強（球從藍變灰白 → 仍與草地 luminance
不同）。Event-camera-style sparse map → CC → blob centroid，**完全跳過
HSV**。可作為 V11 並行第二信號，physics gate (epipolar / area) 後
union candidates。

**Cost**：Y plane 直取免轉色（省 ~0.5 ms 的 BGR convert）；diff +
threshold + CC ~0.4 ms。**比 V11 還省**。

**直擊**：α + β 都直擊。Specular 球的 luminance edge 對草地仍高對比，
chroma 退讓沒事。

**風險**：(a) 兩個球 silhouette 重疊（高速時不重疊但中速時部分重疊）造
成 CC 怪形；(b) 任何 cam micro-vibration 整張噪音；(c) 球與類 luminance
背景（白色觀眾、白牆）會瞎；(d) 雙峰現象需要 「pick the one closer to
predicted trajectory」 的 disambiguation 邏輯。**與 I3 + I1 組合最強**。

---

# Top 3 to validate first

| # | Idea | Cost | 預期增益 | 技術風險 | 為何挑 |
|---|---|---|---|---|---|
| **1** | **I8 Y-plane temporal contrast** | 0.4 ms (省了 0.5 ms 比 V11 還快) | +3-5pp（直擊 α+β）| 低（純 OpenCV、可離線在 1073 GT 上即時驗）| 邊際成本最低、改動 surface 最小、跟 V11 並行不衝突；可立刻在現有 SAM2 GT 上跑 ablation |
| **2** | **I1 Stereo epipolar prior** | 0.3 ms server post + WS payload 改造 | +2-4pp on post 路徑（直擊兩台 *獨立* miss 那部分）| 中（需 cam A B 都標 GT 才好驗，目前 GT 是 per-cam-session 不是 paired event）| 唯一直接利用 dual-cam setup 的方向；server post 路徑無 latency 限制完美匹配；解 86% miss 集中問題 |
| **3** | **I6 CPL filter capture-side** | $30 + 一場 capture 實驗 | 可能直接砍掉 α 整類 (50% of α miss = +1-2pp)，或破壞 240fps（兩極） | 高（光量風險未知，需實測）| 唯一從**問題根源**動手；如果 work，algorithm 端剩餘問題退化成 trivial；不 work 也只花一個下午確認 dead-end |

**為什麼不挑 I5 (ML proposer)**：跟已派員的 tiny FCN distillation 高度
重疊，等那條結論回來再決定要不要併。**為什麼不挑 I3 (ballistic prior)**：
依賴 post 路徑先有 working pipeline，先後關係上排在 I1 後面。**為什麼
不挑 I7 (redefine)**：是 meta，不是方法本身；驗證它需要先完成 1+2
看新天花板再判斷使用者是否仍 unhappy。

# 結尾備注

- I1 + I8 在 server post 路徑可直接組合：epipolar prior × Y-plane 雙信號，
  各自獨立失效模式
- I6 是唯一不消耗 algorithm budget 的方向，若可行應**最先排**因為它讓
  其他方向的有效性 baseline 改變（CPL 後 V11 可能直接到 0.95+，I8/I1
  增益空間隨之縮）
- 沒列：active illumination（IR LED + IR-pass filter）— 過於 invasive，
  改動 capture stack 比 CPL 大一階；列入 I6 的衍生分支但不獨立 brief
