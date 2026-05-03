# scripts/

研究腳本。每隻 self-contained，從 `_paths.py` 拿 repo / workspace 路徑。

## GT 處理工具（底層）

- `_mask_sheet.py` — contact-sheet renderer，視覺檢驗 SAM2 GT mask
- `_hsv_ratio_scan.py` — 全 GT mask 跑 HSV-blue 交集比例 → `outputs/_mask_audit/hsv_ratio_per_frame.json`
- `_auto_label_from_hsv.py` — 用 HSV ratio 自動分類 ok/borderline/bad
- `_aggregate_labels.py` — 多 reviewer label 聚合
- `_materialize_clean_gt.py` — 寫出 `masks_hsv/` 乾淨 GT（hsv_area >= 5 用 HSV-clean centroid，否則 drop）

## metric 重新框架（為什麼要丟 R_emit、改用 R_top1）

- `cand_count_hist.py` — per-frame n_cand 分布；PROD/V11/V11+D1 三條線疊圖
- `R_topK_baseline.py` — production shape-cost ranker 下的 R@K；V11+D1 R_emit=0.974 / R_top1=0.008，spray bonus 量化

## 偵測勝出方法

- `hybrid.py` — **R_top1 = 0.660 vs PROD 0.615**。
  PROD 有 emit → PROD shape cost 排序；PROD 空 → V11 + neighbor-persistence-asc 救援（球會動，distractor 不會）。
  兩個 hyperparam 都從物理推：`NEIGH_HALF=6` (50ms@240fps)、`MATCH_PX=5` (CC centroid 雜訊)。

## 反規則

- 不寫「per-session 微調 thresh / kernel / dedup_px」的腳本——過擬合
- 不走「emit 越多越好 + union 越多越好」這條死路
- 死實驗 `git rm`，不留編號占位
- outputs/ gitignored，自己看著辦
