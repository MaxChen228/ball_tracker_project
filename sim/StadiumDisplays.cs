using Godot;
using System;

public partial class StadiumDisplays : Node3D
{
    private Label3D leftBoard;
    private Label3D centerBoard;
    private Label3D rightBoard;

    // --- 比賽狀態模擬 ---
    private int balls = 0;
    private int strikes = 0;
    private int outs = 0;
    private int inning = 8;
    private int guestScore = 8;
    private int homeScore = 3;

    public override void _Ready()
    {
        centerBoard = GetNode<Label3D>("CenterBoard");

        ResetForPitch();
    }

    // --- 顯示主審判決 (好壞球) ---
    public void ShowPitchCall(string call)
    {
        if (centerBoard == null) return;
        
        centerBoard.Text = $"[ {call} ]";
        
        if (call == "BALL")
            centerBoard.Modulate = Colors.Green;
        else if (call == "STRIKE")
            centerBoard.Modulate = Colors.OrangeRed;
        else
            centerBoard.Modulate = Colors.White;
    }

    // --- 顯示打擊結果 ---
    public void ShowHitResult(string result)
    {
        if (centerBoard == null) return;
        
        centerBoard.Text = result;
        
        if (result.Contains("HOME RUN"))
            centerBoard.Modulate = Colors.Red;
        else if (result.Contains("FOUL"))
            centerBoard.Modulate = Colors.Gray;
        else
            centerBoard.Modulate = Colors.Cyan;
    }

    // --- 準備投球狀態 ---
    public void ResetForPitch()
    {
        if (centerBoard == null) return;
        
        centerBoard.Text = "PITCHING...";
        centerBoard.Modulate = Colors.White;
    }
}