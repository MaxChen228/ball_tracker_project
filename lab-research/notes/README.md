# notes/

`lab-research` 的文字入口。用途分三種：

- `report`: 已有結論、可直接引用
- `design`: 實驗設計或 proposal
- `survey`: 文獻 / API / 外部方案整理

## 推薦閱讀順序

1. [`00_synthesis_2026-05-01.md`](00_synthesis_2026-05-01.md) — 最新總整
2. [`02_v11_followup.md`](02_v11_followup.md) — V11 基線主報告
3. [`11_cue_independence.md`](11_cue_independence.md) — V11 與 Y-diff 正交性
4. [`19_multiscale_ydiff.md`](19_multiscale_ydiff.md) — 多尺度 temporal cue
5. [`20_consensus_residual_analysis.md`](20_consensus_residual_analysis.md) — 剩餘死角分析

## Index

| Note | Type | 角色 |
|---|---|---|
| [`00_synthesis_2026-05-01.md`](00_synthesis_2026-05-01.md) | report | 最新研究總整與下一步 |
| [`01_final_report.md`](01_final_report.md) | report | 7-session V10 原報告 |
| [`02_v11_followup.md`](02_v11_followup.md) | report | 9-session V11 follow-up，現行 HSV 基線 |
| [`03_dual_pipeline_arch.md`](03_dual_pipeline_arch.md) | design | 雙路徑架構思考 |
| [`03_frst_eval.md`](03_frst_eval.md) | report | FRST 單路評估 |
| [`04_literature_survey.md`](04_literature_survey.md) | survey | 文獻掃描 |
| [`05_github_survey.md`](05_github_survey.md) | survey | 外部 repo / implementation 掃描 |
| [`06_orthogonal_ideas.md`](06_orthogonal_ideas.md) | design | 正交 cue 腦暴 |
| [`07_epipolar_rescue.md`](07_epipolar_rescue.md) | design | stereo / epipolar 補救方向 |
| [`08_yplane_diff.md`](08_yplane_diff.md) | report | Y-plane diff 初版結果 |
| [`09_a15_detection_benchmarks.md`](09_a15_detection_benchmarks.md) | survey | A15 / ANE latency 現實檢查 |
| `10_dl_upper_bound.md` | missing | `scripts/22_dl_upper_bound.py` 引用，但目前檔案不存在 |
| [`11_cue_independence.md`](11_cue_independence.md) | report | V11 / Y-diff cue independence |
| [`12_ensemble_distillation_design.md`](12_ensemble_distillation_design.md) | design | distillation 設計 |
| [`13_ensemble_distillation_results.md`](13_ensemble_distillation_results.md) | report | distillation 實驗結果 |
| [`14_vision_circle_api_check.md`](14_vision_circle_api_check.md) | survey | Apple Vision circle API 事實查核 |
| [`15_yolo_ane_latency_check.md`](15_yolo_ane_latency_check.md) | survey | YOLO / ANE latency 事實查核 |
| [`16_roi_frst.md`](16_roi_frst.md) | report | ROI-FRST 結果 |
| [`17_dichromatic_design.md`](17_dichromatic_design.md) | design | dichromatic 路線設計 |
| [`18_dichromatic_results.md`](18_dichromatic_results.md) | report | dichromatic 負結果 |
| [`19_multiscale_ydiff.md`](19_multiscale_ydiff.md) | report | multi-scale Y-diff 結果 |
| [`20_consensus_residual_analysis.md`](20_consensus_residual_analysis.md) | report | consensus residual 深挖 |

## 命名上的現況

- `03_*` 有兩個檔，是不同分支，不做更名以免破壞現有引用。
- `10_*` 缺號目前保留，提醒這條研究線的文件還沒補齊。
- `21+` 之後的實驗大多只有 script / output，仍沿用 note 編號與 script 編號不完全同步的現況。
