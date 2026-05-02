# lab/research

球體偵測研究工作區。**研究與量化驗證用，不是 production code**。

iOS / server production detector 在 `ball_tracker/BallDetector.mm` 與
`server/detection.py`（HSV + CC + aspect/fill gate；研究內部代號 PROD）。
研究目標見 [`CLAUDE.md`](CLAUDE.md)：在 SAM2 GT 上以可泛化方法擊敗 PROD。

## 現況

R_top1 比較（1956 GT frames，TOL=10 px，production shape-cost ranker）：

| | R_top1 | 備註 |
|---|---|---|
| PROD | 0.615 | tight HSV+gate，每幀 ~1.2 cand |
| **28d_hybrid** | **0.660** | PROD 為主，PROD 空時 V11 + persistence rescue |

詳見 `scripts/28d_hybrid.py` + `outputs/28d_hybrid.json`。

## 目錄

```
lab/research/
├── README.md          ← 你在這裡
├── CLAUDE.md          agent mandate（讀這個，比 README 重要）
├── notes/             空，等寫
├── outputs/           gitignored；scripts 自己產
└── scripts/
    ├── _paths.py      路徑 helper
    ├── _*.py          GT QA / cleanup pipeline
    ├── 27*.py         metric 重新框架（R_emit → R_top1）
    └── 28d_hybrid.py  贏 PROD 的方法
```

## 環境

```bash
cd lab/research
uv run --project ../../server python scripts/28d_hybrid.py
```

`server/.venv`（uv 管理，Python 3.13）。需要 `opencv-python` / `numpy` /
`matplotlib`（27 系列圖表用）。
