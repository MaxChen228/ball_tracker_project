using Godot;
using System;

public partial class ScoreBugUI : CanvasLayer
{
    // --- UI 節點參照 ---
    private Label countLabel;     // 顯示好壞球數 (如 0-0)
    private Label inningLabel;    // 顯示局數 (如 ▼ 1)
    private ColorRect out1;       // 第一個出局數燈號
    private ColorRect out2;       // 第二個出局數燈號
    private MarginContainer marginContainer;
    private PanelContainer mainBackground;
    private float lastViewportWidth = 0;

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
        marginContainer = FindChild("ScoreBugMargin", true, false) as MarginContainer;
        mainBackground = FindChild("MainBackground", true, false) as PanelContainer;

        // 🔍 檢測點 3：確認文字節點有沒有被 FindChild 找到
        if (countLabel == null) GD.PrintErr("❌ [ScoreBugUI.cs] 找不到 CountLabel 節點！");
        if (inningLabel == null) GD.PrintErr("❌ [ScoreBugUI.cs] 找不到 InningLabel 節點！");

        UpdateUI();
        UpdateSize();
    }

    public override void _Process(double delta)
    {
        var vp = GetViewport().GetVisibleRect().Size;
        if (vp.X != lastViewportWidth)
        {
            lastViewportWidth = vp.X;
            UpdateSize();
        }
    }

    private void UpdateSize()
    {
        if (marginContainer == null) return;
        var vp = GetViewport().GetVisibleRect().Size;
        
        // 比例化 margin 和背景大小
        float marginL = Math.Max(15f, vp.X * 0.015f);
        float marginT = Math.Max(15f, vp.Y * 0.02f);
        float marginR = Math.Max(15f, vp.X * 0.015f);
        float marginB = Math.Max(15f, vp.Y * 0.02f);
        
        marginContainer.AddThemeConstantOverride("margin_left", (int)marginL);
        marginContainer.AddThemeConstantOverride("margin_top", (int)marginT);
        marginContainer.AddThemeConstantOverride("margin_right", (int)marginR);
        marginContainer.AddThemeConstantOverride("margin_bottom", (int)marginB);
        
        // 更新背景面板大小
        if (mainBackground != null)
        {
            float bgW = Math.Max(200f, vp.X * 0.15f);
            mainBackground.CustomMinimumSize = new Vector2(bgW, 0);
        }
        
        // 更新所有文字大小
        float fontSize = Math.Max(20f, vp.X * 0.025f);
        if (countLabel != null) countLabel.AddThemeFontSizeOverride("font_size", (int)fontSize);
        if (inningLabel != null) inningLabel.AddThemeFontSizeOverride("font_size", (int)fontSize);
        
        // 更新 TeamAName, TeamBName, TeamAScore, TeamBScore, BatterName
        var teamAName = FindChild("TeamAName", true, false) as Label;
        var teamBName = FindChild("TeamBName", true, false) as Label;
        var teamAScore = FindChild("TeamAScore", true, false) as Label;
        var teamBScore = FindChild("TeamBScore", true, false) as Label;
        var batterName = FindChild("BatterName", true, false) as Label;
        
        if (teamAName != null) teamAName.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.9f));
        if (teamBName != null) teamBName.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.9f));
        if (teamAScore != null) teamAScore.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.9f));
        if (teamBScore != null) teamBScore.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.9f));
        if (batterName != null) batterName.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.9f));
        
        // 更新出局數燈號大小
        float dotSize = Math.Max(12f, vp.X * 0.012f);
        if (out1 != null) out1.CustomMinimumSize = new Vector2(dotSize, dotSize);
        if (out2 != null) out2.CustomMinimumSize = new Vector2(dotSize, dotSize);
        
        // 更新壘包大小
        var basesDiamond = FindChild("BasesDiamond", true, false) as Control;
        if (basesDiamond != null)
        {
            float baseSize = Math.Max(30f, vp.X * 0.03f);
            basesDiamond.CustomMinimumSize = new Vector2(baseSize, baseSize);
        }
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