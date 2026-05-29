using Godot;
using System;
using System.Collections.Generic;
public partial class Fielder : Node3D
{
    // --- 守備能力參數 ---
    [Export] public float RunSpeed = 7.5f;       // 跑速 (m/s)
    [Export] public float CatchRadius = 0.8f;    // 接球判定半徑 (m)
    [Export] public float ReactionTime = 0.25f;  // 反應時間 (秒)

    // --- 狀態機定義 ---
    public enum FielderState { Idle, Reacting, Running, Catching }
    public FielderState CurrentState = FielderState.Idle;

    // --- 運行時變數 ---
    public Vector3 TargetPosition;
    private float reactionTimer = 0f;
    // --- 🌟 階段五新增：視覺化除錯元件 ---
    private MeshInstance3D debugLine;
    private ImmediateMesh immediateMesh;

    public override void _Ready()
    {
        // 動態建立 Debug 用的線條節點
        debugLine = new MeshInstance3D();
        immediateMesh = new ImmediateMesh();
        debugLine.Mesh = immediateMesh;
        
        // 設定 TopLevel 為 true，讓線條的座標獨立於世界空間，不會跟著守備員跑動而跟著移動
        debugLine.TopLevel = true; 
        AddChild(debugLine);
        
        // 幫線條上一層螢光綠色的材質，並設定不受光照影響 (Unshaded)，方便在場景中看清楚
        var material = new StandardMaterial3D();
        material.AlbedoColor = Colors.Black;
        material.ShadingMode = BaseMaterial3D.ShadingModeEnum.Unshaded;
        debugLine.MaterialOverride = material;
    }
    // --- 🌟 階段五新增：繪製與清除路徑 ---
    private void DrawDebugPath(Vector3 start, Vector3 end)
    {
        immediateMesh.ClearSurfaces();
        immediateMesh.SurfaceBegin(Mesh.PrimitiveType.Lines);
        
        // 起點與終點都稍微抬高，貼著草皮表面
        immediateMesh.SurfaceAddVertex(start + new Vector3(0, 0.1f, 0));
        immediateMesh.SurfaceAddVertex(end + new Vector3(0, 0.1f, 0));
        
        immediateMesh.SurfaceEnd();
    }

    private void ClearDebugPath()
    {
        if (immediateMesh != null)
        {
            immediateMesh.ClearSurfaces();
        }
    }
    public override void _Process(double delta)
    {
        switch (CurrentState)
        {
            case FielderState.Idle:
                ClearDebugPath(); // 只要在發呆，就確保地板上沒有殘留的除錯線
                break;
            case FielderState.Reacting:
                // 模擬球員判斷球向的延遲時間
                reactionTimer -= (float)delta;
                if (reactionTimer <= 0)
                {
                    CurrentState = FielderState.Running;
                }
                break;

            case FielderState.Running:
                // 計算朝向目標的向量 (忽略 Y 軸高度，只在 XZ 平面跑動)
                Vector3 direction = TargetPosition - GlobalPosition;
                direction.Y = 0; 

                float distance = direction.Length();

                // 如果還沒到達目標點
                if (distance > CatchRadius * 0.5f)
                {
                    // 在跑動中持續微調目標點，讓守備員能跟隨球的實際軌跡
                    // 而不是死板地跑向預測點
                    GlobalPosition += direction.Normalized() * RunSpeed * (float)delta;
                }
                else
                {
                    // 到達預測攔截點，進入準備接球狀態
                    CurrentState = FielderState.Catching;
                }
                break;

            case FielderState.Catching:
                // 守備員已到達預測攔截點，持續微調朝向球的真實位置
                // 這樣即使球落地彈跳或預測有誤差，守備員也會跟著移動
                break;
        }
    }

    // --- 開放給外部 (DefenseManager) 呼叫的指令 ---
    public void CommandToCatch(Vector3 predictedLandingPoint)
    {
        TargetPosition = predictedLandingPoint;
        reactionTimer = ReactionTime;
        CurrentState = FielderState.Reacting;
        DrawDebugPath(GlobalPosition, TargetPosition);
    }

    public void ResetFielder(Vector3 basePosition)
    {
        GlobalPosition = basePosition;
        CurrentState = FielderState.Idle;
        ClearDebugPath();
    }
    // --- 🌟 階段三：尋找最佳攔截點 ---
    public void CalculateInterception(List<Vector3> ballPath, float deltaT)
    {
        // 預設最差的情況：只能跑到最終落點去撿球
        Vector3 bestInterceptionPoint = ballPath[ballPath.Count - 1];
        bestInterceptionPoint.Y = 0; // 守備員只在地面上跑
        bool canCatchFlyBall = false;

        // 遍歷未來的每一個時間點
        for (int i = 0; i < ballPath.Count; i++)
        {
            float t = i * deltaT;
            Vector3 ballPos = ballPath[i];

            // 條件一：球的高度必須在可接殺範圍內 (例如 2.5 公尺以下)
            // 且必須大於地面 (如果是彈跳球就不算接殺了，不過我們先簡化)
            if (ballPos.Y > 2.5f) continue;

            // 條件二：反應時間內，守備員無法移動
            if (t <= ReactionTime) continue;

            // 計算守備員跑到該點需要的平面距離
            float distanceToRun = new Vector2(ballPos.X - GlobalPosition.X, ballPos.Z - GlobalPosition.Z).Length();
            
            // 計算守備員在這個時間點能跑多遠
            float maxRunDistance = RunSpeed * (t - ReactionTime);

            // 核心判斷：如果跑得到的距離 大於或等於 需要跑的距離，代表來得及！
            if (maxRunDistance >= distanceToRun)
            {
                bestInterceptionPoint = ballPos;
                bestInterceptionPoint.Y = 0; 
                canCatchFlyBall = true;
                
                GD.Print($"🎯 [{Name}] 尋找到攔截點！預計 {t:F2} 秒後在座標 {bestInterceptionPoint} 交會。");
                break; // 找到最早的交會點就停止搜
            }
        }

        if (!canCatchFlyBall)
        {
            GD.Print($"⚠️ [{Name}] 來不及接殺，只能跑向落點撿球。");
        }

        // 呼叫我們在階段一寫好的指令，啟動狀態機開始跑動！
        CommandToCatch(bestInterceptionPoint);
    }
    public void MicroAdjust(Vector3 actualBallPos, float delta)
    {
        Vector3 dir = actualBallPos - GlobalPosition;
        dir.Y = 0; // 一樣只看地面的投影
        
        // 只要球的投影在地面上，就持續追蹤移動（不再限制最小距離）
        if (dir.Length() > 0.01f)
        {
            // 視線盯著球，用較慢的速度 (例如跑速的 60%) 進行最後的步伐調整
            GlobalPosition += dir.Normalized() * (RunSpeed * 0.6f) * delta;
        }
    }
}