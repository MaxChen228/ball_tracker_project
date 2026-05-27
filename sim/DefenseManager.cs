using Godot;
using System;
using System.Collections.Generic;

// 注意：C# 的 Enum 不能以數字開頭，所以 1B 改為 FirstBase
public enum FieldPosition { P, C, FirstBase, SecondBase, ThirdBase, SS, LF, CF, RF }

public partial class DefenseManager : Node3D
{
    // 儲存場上 9 名守備員的參考
    public Dictionary<FieldPosition, Fielder> Fielders = new Dictionary<FieldPosition, Fielder>();
    public Fielder ActiveFielder = null;
    // 定義基礎守備陣位 (依據 stadium.tscn 的比例推算)
    private Dictionary<FieldPosition, Vector3> basePositions = new Dictionary<FieldPosition, Vector3>()
    {
        { FieldPosition.P, new Vector3(0, 0, -18.44f) },       // 投手丘
        { FieldPosition.C, new Vector3(0, 0, 1.5f) },          // 捕手 (本壘後方)
        { FieldPosition.FirstBase, new Vector3(22f, 0, -17f) },// 一壘手 (稍微離開壘包)
        { FieldPosition.SecondBase, new Vector3(10f, 0, -28f)},// 二壘手
        { FieldPosition.ThirdBase, new Vector3(-22f, 0, -17f)},// 三壘手
        { FieldPosition.SS, new Vector3(-10f, 0, -28f) },      // 游擊手
        { FieldPosition.LF, new Vector3(-40f, 0, -65f) },      // 左外野
        { FieldPosition.CF, new Vector3(0f, 0, -85f) },        // 中外野
        { FieldPosition.RF, new Vector3(40f, 0, -65f) }        // 右外野
    };

    public override void _Ready()
    {
        // 尋找底下作為子節點的守備員並進行註冊
        foreach (Node child in GetChildren())
        {
            if (child is Fielder fielder)
            {
                // 假設你在 Godot 編輯器中把節點命名為 "CF", "SS" 等等
                if (Enum.TryParse(child.Name, out FieldPosition pos))
                {
                    Fielders[pos] = fielder;
                    fielder.ResetFielder(basePositions[pos]);
                }
                else
                {
                    GD.PrintErr($"[DefenseManager] 無法解析守備員名稱: {child.Name}");
                }
            }
        }
        
        GD.Print($"✅ [DefenseManager] 成功初始化 {Fielders.Count} 名守備員。");
    }

    // 提供給外部呼叫，將所有球員歸位 (例如下一球開始前)
    public void ResetAllFielders()
    {
        ActiveFielder = null; // 🌟 清空
        foreach (var kvp in Fielders)
        {
            kvp.Value.ResetFielder(basePositions[kvp.Key]);
        }
    }
	// --- 🌟 階段二新增：接收擊球訊號與預測軌跡 ---
    // --- 🌟 階段三：指派守備員 ---
    public void OnBallHit(List<Vector3> ballPath, float deltaT)
    {
        if (ballPath.Count == 0) return;

        // 1. 取得落點方向 (拿陣列最後一個點當作這球最終的落點)
        Vector3 finalLandingPoint = ballPath[ballPath.Count - 1];

        // 2. 尋找離落點最近的守備員
        Fielder bestFielder = null;
        float minDistance = float.MaxValue;

        foreach (var kvp in Fielders)
        {
            // 簡單防呆：通常投手 (P) 和捕手 (C) 不會去追深遠飛球，可以先略過
            if (kvp.Key == FieldPosition.P || kvp.Key == FieldPosition.C) continue;

            // 計算守備員與最終落點的平面距離
            float dist = new Vector2(kvp.Value.GlobalPosition.X - finalLandingPoint.X, 
                                     kvp.Value.GlobalPosition.Z - finalLandingPoint.Z).Length();
            if (dist < minDistance)
            {
                minDistance = dist;
                bestFielder = kvp.Value;
            }
        }

        // 3. 把軌跡陣列交給這位被選中的守備員，讓他自己算攔截點
        if (bestFielder != null)
        {
            GD.Print($"🏃‍♂️ [DefenseManager] 指派 {bestFielder.Name} 去追球！");
            ActiveFielder = bestFielder; // 🌟 存起來
            bestFielder.CalculateInterception(ballPath, deltaT);
        }
    }
    public string CheckCatch(Vector3 ballPos, bool ballHasBounced)
    {
        if (ActiveFielder == null) return null;

        // 計算球與守備員的 2D 平面距離 (圓柱體碰撞概念)
        float horizontalDist = new Vector2(ballPos.X - ActiveFielder.GlobalPosition.X, ballPos.Z - ActiveFielder.GlobalPosition.Z).Length();

        // 判定條件：進入接球半徑，且高度在合理範圍內 (地面到 2.5 公尺之間)
        if (horizontalDist <= ActiveFielder.CatchRadius && ballPos.Y <= 2.0f && ballPos.Y >= 0.037f)
        {
            ActiveFielder.CurrentState = Fielder.FielderState.Idle; // 守備員停止跑動
            ActiveFielder = null; // 任務結束

            if (!ballHasBounced)
            {
                return "OUT (FLY OUT)"; // 直接接殺
            }
            else
            {
                return "FIELDED"; // 攔截到落地安打/滾地球 (未來要接續傳球系統)
            }
        }
        
        return null; // 還沒接到
    }
    public void TrackRealBall(Vector3 realBallPos, float delta)
    {
        // 只有當有人在追球，而且他已經跑到預測落點開始「等球」時，才啟動微調
        if (ActiveFielder != null && ActiveFielder.CurrentState == Fielder.FielderState.Catching)
        {
            ActiveFielder.MicroAdjust(realBallPos, delta);
        }
    }
}