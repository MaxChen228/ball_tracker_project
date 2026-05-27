using Godot;
using System;

public partial class ScoreBugUI : CanvasLayer
{
    // --- UI 節點參照 ---
    private Label countLabel;     // 顯示好壞球數 (如 0-0)
    private Label inningLabel;    // 顯示局數 (如 ▼ 1)
    private ColorRect out1;       // 第一個出局數燈號
    private ColorRect out2;       // 第二個出局數燈號

    // --- 比賽狀態變數 ---
    private int balls = 0;
    private int strikes = 0;
    private int outs = 0;
    private int inning = 1;

    public override void _Ready()
    {
        countLabel = FindChild("CountLabel", true, false) as Label;
        inningLabel = FindChild("InningLabel", true, false) as Label;
        out1 = FindChild("Out1", true, false) as ColorRect;
        out2 = FindChild("Out2", true, false) as ColorRect;

        // 🔍 檢測點 3：確認文字節點有沒有被 FindChild 找到
        if (countLabel == null) GD.PrintErr("❌ [ScoreBugUI.cs] 找不到 CountLabel 節點！");
        if (inningLabel == null) GD.PrintErr("❌ [ScoreBugUI.cs] 找不到 InningLabel 節點！");

        UpdateUI();
    }

    public void ShowPitchCall(string call)
    {
        // 🔍 檢測點 4：確認 ScoreBugUI 有沒有被呼叫到
        GD.Print($"📥 [ScoreBugUI.cs] ShowPitchCall 收到指令: '{call}'");

        if (call == "BALL")
        {
            balls++;
            if (balls >= 4) { ResetCount(); } 
        }
        else if (call == "STRIKE" || call == "STRIKE (MISS)")
        {
            strikes++;
            if (strikes >= 3) { ResetCount(); AddOut(); }
        }
        else if (call == "IN_PLAY")
        {
            ResetCount();
            GD.Print("scorebugknow");
        }
        else
        {
            GD.PrintErr($"⚠️ [ScoreBugUI.cs] 收到無法辨識的好壞球指令: '{call}'");
        }
        
        UpdateUI();
    }

    public void ShowHitResult(string result)
    {
        GD.Print($"📥 [ScoreBugUI.cs] ShowHitResult 收到指令: '{result}'");
        // ... (維持原本的邏輯) ...
        if (result.Contains("FOUL")) { if (strikes < 2) strikes++; }
        else if (result.Contains("OUT") || result.Contains("CAUGHT")) { ResetCount(); AddOut(); }
        else { ResetCount(); }
        
        UpdateUI();
    }

    private void UpdateUI()
    {
        // 🔍 檢測點 5：確認最後計算出來的數字對不對
        GD.Print($"📺 [ScoreBugUI.cs] 更新畫面文字 -> {balls}-{strikes}, 出局數: {outs}");

        if (countLabel != null) countLabel.Text = $"{balls}-{strikes}";
        if (inningLabel != null) inningLabel.Text = $"▼ {inning}";
        if (out1 != null) out1.Color = outs >= 1 ? Colors.Red : Colors.DarkGray;
        if (out2 != null) out2.Color = outs >= 2 ? Colors.Red : Colors.DarkGray;
    }

    // 🌟 處理出局數增加與換局邏輯
    private void AddOut()
    {
        outs++;
        if (outs >= 3)
        {
            outs = 0;
            inning++; // 三出局換下一局
        }
    }

    // 🌟 重置當前打者的好壞球數
    private void ResetCount()
    {
        balls = 0;
        strikes = 0;
    }
}