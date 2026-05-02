# lab/research

球體偵測研究工作區。**研究與量化驗證用，不是 production code**。

iOS 編譯進去的 detector (`ball_tracker/BallDetector.mm`) 是 HSV+CC+aspect/fill
gate（研究稱它為 V11，是研究的起點不是研究產物）。研究最高成果 V11+Y-diff
(R=0.970) 尚未整合進 iOS。

## 從哪開始讀

| 目的 | 看這裡 |
|---|---|
| 30 秒抓全景 | [`MAP.md`](MAP.md) |
| 上次 GT 擴張改了哪些數字 | [`CHANGELOG.md`](CHANGELOG.md) |
| 想 30 分鐘進入研究 | `notes/00_synthesis_2026-05-01.md` → `02_v11_followup.md` → `11_cue_independence.md` → `19_multiscale_ydiff.md` → `20_consensus_residual_analysis.md` |

## 目錄

```
lab/research/
├── README.md            ← 你在這裡
├── MAP.md               單頁全景（演算法世代 × scripts × notes 矩陣）
├── CHANGELOG.md         GT 擴張或結構改動記錄
├── notes/               研究筆記、報告、設計稿（內容不依編號順序，看 MAP）
├── outputs/             scripts 產出（含 _figures/ 收集視覺化 PNG）
└── scripts/
    ├── _paths.py        ROOT/WS/OUT helper + manifest 適配 + mask reader
    ├── ball_detector.py V11 reference impl
    ├── 0X_*.py 1X_*.py  研究 script（編號為時間軸，狀態見 MAP.md）
    └── _archive/
        ├── 0X_*.py      pre-V10 早期 heuristic 探索（化石）
        └── falsified/   負結果 code（dichromatic / DL distillation）
```

## 重跑

```bash
cd lab/research
# active headlines（GT 擴大時值得重跑）
for s in 02_head_to_head 19_frst 21_yplane_diff 22_cue_independence \
         24_roi_frst 26_consensus_residual 26_multiscale_ydiff; do
  uv run --project ../../server python scripts/${s}.py
done
```

完整 active list、每支 script 用途、negative result 對照見 [`MAP.md`](MAP.md)。

## 環境

跑在 `server/.venv`（`uv` 管理，Python 3.13）。一律以
`uv run --project ../../server python scripts/<n>.py` 執行。

依賴：`opencv-python` / `numpy` / `scipy`（傳統 CV 腳本）；
`torch`（僅 falsified 的 DL 腳本需要）。
