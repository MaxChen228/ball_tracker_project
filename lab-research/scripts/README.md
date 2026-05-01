# scripts/

研究腳本入口。命名大致按研究時間序，但不是嚴格線性 pipeline。

## 分類

- `01`-`18`: V10/V11 基線、ablation、failure mode、preprocess 探索
- `19`: FRST
- `21`-`26`: Y-diff / cue independence / distillation / dichromatic / residual
- `ball_detector.py`: 研究版參考 detector
- `_archive/`: 已確認 dead-end 的舊探索，不再當前線

## 目前常用

- `19_frst.py`
- `21_yplane_diff.py`
- `22_cue_independence.py`
- `23_ensemble_distillation.py`
- `24_roi_frst.py`
- `25_dichromatic.py`
- `26_consensus_residual.py`
- `26_multiscale_ydiff.py`
- `26b_cluster.py`

## 注意

- `22_dl_upper_bound.py` 仍指向缺失中的 `notes/10_dl_upper_bound.md`；這條研究線文件未收尾。
- 多數腳本會自己建立 `lab-research/outputs/`，但不會整理舊輸出。
