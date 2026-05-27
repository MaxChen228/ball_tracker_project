using Godot;
using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text;

// --- JSON 資料結構 (包含主審判決) ---
public class HitDataConfig
{
    [JsonPropertyName("exit_velocity_mph")] public float ExitVelocityMph { get; set; }
    [JsonPropertyName("launch_angle_deg")] public float LaunchAngleDeg { get; set; }
    [JsonPropertyName("spray_angle_deg")] public float SprayAngleDeg { get; set; }
    [JsonPropertyName("spin_rate_rpm")] public float SpinRateRpm { get; set; }
    [JsonPropertyName("pitch_x_m")] public float PitchXM { get; set; } 
    [JsonPropertyName("pitch_y_m")] public float PitchYM { get; set; } 
    [JsonPropertyName("call")] public string PitchCall { get; set; } // 主審判決
}

public partial class Baseball : Node3D
{
    // --- 物理常數 ---
    private readonly Vector3 GRAVITY = new Vector3(0, -9.81f, 0);
    private float airDensity = 1.225f;
    private float ballMass = 0.145f;
    private float ballArea = Mathf.Pi * 0.037f * 0.037f;
    private float Cd = 0.33f;
    private float Cl = 0.15f;

    private Vector3 velocity;
    private Vector3 spin;
    private bool isSimulating = false;
    private bool isRolling = false;
    private bool hasBounced = false;
    private bool hasPassedWall = false;

    private float wallDistance = 120.0f;
    private float wallHeight = 4.0f;

    // --- 視覺、UI 與 歷史紀錄 ---
    private MeshInstance3D trajectoryLine;
    private ImmediateMesh immediateMesh;
    private MeshInstance3D pitchMarker;
    private List<Vector3> pathPoints = new List<Vector3>();
    private StadiumDisplays displays;
    private ScoreBugUI scoreBug;
    // 🌟 【改動說明：新增變數】
    // pitchDecisionMenu: 用來存放我們在場景中建立的 UI 選單節點。
    // currentHitData: 用來「暫存」當前這顆球的資料。因為引入了選單暫停機制，
    //                 我們必須把 JSON 解析出來的資料存起來，等選單點擊後才能拿出來計算物理。
    private PitchDecisionMenu pitchDecisionMenu;
    private HitDataConfig currentHitData;

    private PacketPeerUdp udpReceiver;
    private const int LISTEN_PORT = 9999;
    private List<string> pitchHistory = new List<string>();
    private int currentPitchIndex = -1;
    // --- 🌟 階段二新增：軌跡預測資料 ---
    public List<Vector3> PredictedPath = new List<Vector3>();
    public float PredictionDeltaTime = 0.016f; // 假設 60fps 的時間步長 (Delta T)
    public override void _Ready()
    {
        trajectoryLine = GetNode<MeshInstance3D>("TrajectoryLine");
        trajectoryLine.TopLevel = true;
        immediateMesh = trajectoryLine.Mesh as ImmediateMesh;

        pitchMarker = GetNodeOrNull<MeshInstance3D>("PitchMarker") ?? GetNodeOrNull<MeshInstance3D>("baseball");
        if (pitchMarker != null) pitchMarker.TopLevel = true;

        displays = GetNodeOrNull<StadiumDisplays>("../StadiumDisplays");
        
        scoreBug = GetNodeOrNull<ScoreBugUI>("../ScoreBugUI");
        if (scoreBug == null)
        {
            GD.PrintErr("❌ [Baseball.cs] 找不到 ScoreBugUI 節點！請檢查 GetNodeOrNull 裡面的路徑。");
        }
        else
        {
            GD.Print("✅ [Baseball.cs] 成功抓到 ScoreBugUI 節點！");
        }
        // 🌟 【改動說明：綁定選單事件】
        // 1. 取得 UI 選單節點。
        // 2. 將選單發出的 DecisionSelected 訊號，連線到本程式的 OnManualDecisionReceived 函數。
        // 這樣只要按鈕一被按，就會立刻通知 Baseball.cs。
        pitchDecisionMenu = GetNodeOrNull<PitchDecisionMenu>("../UI/PitchDecisionMenu");
        if (pitchDecisionMenu != null)
        {
            pitchDecisionMenu.DecisionSelected += OnManualDecisionReceived;
        }

        udpReceiver = new PacketPeerUdp();
        udpReceiver.Bind(LISTEN_PORT);
    }
    // --- 🌟 階段二新增：瞬間預算整條軌跡 ---
    // --- 🌟 階段二新增：預測專用加速度計算 ---
    private Vector3 GetPredictionAcceleration(Vector3 v, Vector3 s) 
    { 
        float v_mag = v.Length(); 
        if (v_mag < 0.1f) return GRAVITY; 
        
        Vector3 drag = -0.5f * airDensity * ballArea * Cd * v_mag * v; 
        Vector3 magnus = Vector3.Zero; 
        
        if (s.Length() > 0) 
            magnus = 0.5f * airDensity * ballArea * Cl * (v_mag * v_mag) * s.Normalized().Cross(v.Normalized()); 
            
        return (GRAVITY * ballMass + drag + magnus) / ballMass; 
    }
    // --- 🌟 階段二新增：完整推演軌跡 (直到球完全靜止) ---
    // --- 🌟 階段二與感知誤差：完整推演軌跡 ---
    private void PredictTrajectory()
    {
        PredictedPath.Clear();
        
        Vector3 simPos = GlobalPosition;
        Vector3 simVel = velocity;
        Vector3 simSpin = spin;
        bool simIsRolling = false;

        float dt = PredictionDeltaTime;
        int maxSteps = 2000; 
        int step = 0;

        // 【新增】：在預測開始前，決定這一次的「感知誤差方向與強度」
        // GD.Randf() 會產生 0.0 到 1.0 的小數，轉成 -1.0 到 1.0 的隨機方向
        float errorDirX = (float)GD.Randf() * 2f - 1f;
        float errorDirZ = (float)GD.Randf() * 2f - 1f;
        
        // 誤差係數：數值越大，高空時的誤判越嚴重 (0.1 代表 10 公尺高時，最多會有 1 公尺的偏差)
        float noiseFactor = 0.005f; 

        while (simVel.LengthSquared() > 0.001f && step < maxSteps)
        {
            // 【修改】：加入隨高度變化的干擾，算出「感知座標」
            Vector3 perceivedPos = simPos;
            
            // 只有在空中飛行時才加入高度干擾，滾動時視覺很明確所以不加
            if (!simIsRolling && simPos.Y > 0.037f)
            {
                perceivedPos.X += errorDirX * simPos.Y * noiseFactor;
                perceivedPos.Z += errorDirZ * simPos.Y * noiseFactor;
            }

            // ⚠️ 注意：我們把加上干擾的 perceivedPos 存入陣列，給守備員看
            PredictedPath.Add(perceivedPos);

            // --- 下方的物理模擬 (RK4 與碰撞) 完全維持不變，繼續用真實的 simPos 運算 ---
            if (simIsRolling)
            {
                // ... (原本的滾動邏輯) ...
                float speed = new Vector2(simVel.X, simVel.Z).Length();
                if (speed < 0.5f) simVel = Vector3.Zero;
                else
                {
                    Vector3 decelDir = -simVel.Normalized();
                    decelDir.Y = 0;
                    Vector3 deceleration = decelDir * 5.0f * dt;
                    if (deceleration.Length() > speed) simVel = Vector3.Zero;
                    else simVel += deceleration;
                    simPos += simVel * dt;
                }
            }
            else
            {
                // ... (原本的 RK4 與彈跳邏輯) ...
                Vector3 a1 = GetPredictionAcceleration(simVel, simSpin); 
                Vector3 a2 = GetPredictionAcceleration(simVel + a1 * dt * 0.5f, simSpin); 
                Vector3 a3 = GetPredictionAcceleration(simVel + a2 * dt * 0.5f, simSpin); 
                Vector3 a4 = GetPredictionAcceleration(simVel + a3 * dt, simSpin); 
                Vector3 dv = (a1 + 2 * a2 + 2 * a3 + a4) * (dt / 6.0f); 
                
                simPos += (simVel + dv * 0.5f) * dt; 
                simVel += dv;
                
                if (simPos.Y <= 0.037f)
                {
                    simPos.Y = 0.037f;
                    if (Mathf.Abs(simVel.Y) > 1.0f)
                    {
                        simVel.Y = -simVel.Y * 0.5f;
                        simVel.X *= 0.6f;
                        simVel.Z *= 0.6f;
                    }
                    else
                    {
                        simVel.Y = 0;
                        simSpin = Vector3.Zero; 
                        simIsRolling = true;
                    }
                }
            }
            
            float dist = Mathf.Sqrt(simPos.X * simPos.X + simPos.Z * simPos.Z);
            if (dist >= wallDistance && simPos.Y <= wallHeight) simVel = Vector3.Zero; 

            step++;
        }

        PredictedPath.Add(simPos); // 靜止點沒有高度誤差
    }
    // --- 核心邏輯：重設與解析 ---
    private void ResetPitch(string jsonString)
    {
        // 1. 基礎狀態重置與防守員歸位
        isSimulating = false;
        isRolling = false;
        hasBounced = false;
        hasPassedWall = false;
        pathPoints.Clear();
        if (immediateMesh != null) immediateMesh.ClearSurfaces();

        var defenseManager = GetNodeOrNull<DefenseManager>("../DefenseManager");
        if (defenseManager != null) defenseManager.ResetAllFielders();

        try
        {
            currentHitData = JsonSerializer.Deserialize<HitDataConfig>(jsonString);
            
            // 2. 位置設定 (把球放到進壘點)
            Vector3 startPos = new Vector3(currentHitData.PitchXM, currentHitData.PitchYM, 0);
            GlobalPosition = startPos;
            if (pitchMarker != null)
            {
                pitchMarker.GlobalPosition = startPos;
                pitchMarker.Visible = true;
            }

            // 🌟 關鍵修改：拔掉 if 判斷，無條件彈出選單！
            // 讓所有的球都乖乖停在本壘板前，等你點擊 UI 按鈕。
            pitchDecisionMenu?.ShowMenu();
        }
        catch (Exception e) { GD.PrintErr($"解析錯誤: {e.Message}"); }
    }

    // 🌟 【改動說明：新增的訊號接收函數】
    // 當你在 UI 點擊按鈕後，這裡會被觸發。decision 參數就是你按鈕的 ID (例如 "NOSWING")。
    // --- 核心邏輯：手動選擇結果 ---
    private void OnManualDecisionReceived(string decision)
    {
        if (currentHitData == null) return;

        string finalResult = "";
        switch (decision)
        {
            case "NOSWING":
                bool isStrike = (currentHitData.PitchXM >= -0.215f && currentHitData.PitchXM <= 0.215f) &&
                                (currentHitData.PitchYM >= 0.5f && currentHitData.PitchYM <= 1.1f);
                finalResult = isStrike ? "STRIKE" : "BALL";
                UpdateDisplays(finalResult);
                break;

            case "FOULTIP": 
                finalResult = "FOUL TIP"; 
                UpdateDisplays(finalResult);
                break;

            case "MISS": 
                finalResult = "STRIKE (MISS)"; 
                UpdateDisplays(finalResult);
                break;

            case "FOULTIPCAUGHT": 
                finalResult = "OUT (FOUL TIP CAUGHT)"; 
                UpdateDisplays(finalResult);
                break;

            // 🌟 關鍵新增：接收到「打擊出去」指令
            case "INPLAY": 
                UpdateDisplays("IN_PLAY");   // 讓大螢幕顯示擊球中 (好壞球先不動)
                StartPhysicsSimulation();    // 啟動 RK4 物理引擎與守備預測！
                return; // 直接 return，這球的生死交給守備員去判定
        }
    }

    // --- 核心邏輯：啟動物理模擬 ---
    // --- 核心邏輯：更新所有顯示器 (大螢幕與計分板) ---
    private void UpdateDisplays(string explicitResult = null)
    {
        if (currentHitData == null) return;
        GD.Print($"\n📊 [Baseball.cs UpdateDisplays] 準備更新顯示器...");
        GD.Print($"   -> explicitResult (來自選單): {explicitResult ?? "null"}");
        GD.Print($"   -> currentHitData.PitchCall (來自 JSON): {currentHitData.PitchCall}");
        // 1. 更新球場大螢幕
        if (displays != null)
        {
            displays.ResetForPitch();
            if (explicitResult != null)
            {
                if (explicitResult == "STRIKE" || explicitResult == "BALL" || explicitResult == "STRIKE (MISS)")
                    displays.ShowPitchCall(explicitResult);
                else
                    displays.ShowHitResult(explicitResult);
            }
            else if (currentHitData.PitchCall == "STRIKE" || currentHitData.PitchCall == "BALL" || currentHitData.PitchCall == "IN_PLAY")
            {
                displays.ShowPitchCall(currentHitData.PitchCall);
            }
        }

        // 2. 更新右下角轉播計分板 (ScoreBug)
        if (scoreBug != null)
        {
            if (explicitResult != null)
            {
                GD.Print($"👉 [Baseball.cs] 準備傳送 {explicitResult} 給 ScoreBug");
                if (explicitResult == "STRIKE" || explicitResult == "BALL" || explicitResult == "STRIKE (MISS)")
                    scoreBug.ShowPitchCall(explicitResult);
                else
                    scoreBug.ShowHitResult(explicitResult);
            }
            else if (currentHitData.PitchCall == "STRIKE" || currentHitData.PitchCall == "BALL")
            {
                GD.Print($"👉 [Baseball.cs] 準備傳送 {currentHitData.PitchCall} 給 ScoreBug (略過選單)");
                scoreBug.ShowPitchCall(currentHitData.PitchCall);
            }
        }
        else
        {
            GD.PrintErr("❌ [Baseball.cs] scoreBug 變數為 null，無法通知計分板！");
        }
    }

    // --- 核心邏輯：啟動物理引擎與模擬 ---
    private void StartPhysicsSimulation()
    {
        if (currentHitData == null) return;

        // 計算初速與角度
        float speedMPS = currentHitData.ExitVelocityMph * 0.44704f;
        float launchRad = Mathf.DegToRad(currentHitData.LaunchAngleDeg);
        float sprayRad = Mathf.DegToRad(currentHitData.SprayAngleDeg);
        float horizSpeed = speedMPS * Mathf.Cos(launchRad);

        // 設定 3D 速度向量
        velocity = new Vector3(
            horizSpeed * Mathf.Sin(sprayRad),
            speedMPS * Mathf.Sin(launchRad),
            -horizSpeed * Mathf.Cos(sprayRad)
        );

        // 設定馬格努斯效應的自旋向量
        float omega = currentHitData.SpinRateRpm * 2.0f * Mathf.Pi / 60.0f;
        spin = new Vector3(omega * Mathf.Cos(sprayRad), 0, omega * Mathf.Sin(sprayRad));

        pathPoints.Add(GlobalPosition);
        
        // 啟動引擎，讓 _Process 開始推動球
        isSimulating = true;
        PredictTrajectory();
        
        // 抓取 DefenseManager 節點 (確保路徑正確，根據你場景的結構可能需要調整)
        var defenseManager = GetNodeOrNull<DefenseManager>("../DefenseManager");
        if (defenseManager != null)
        {
            // 將算好的陣列與時間步長交給總管
            defenseManager.OnBallHit(PredictedPath, PredictionDeltaTime);
        }
    }

    // --- (其餘 _Process, Rk4Step, HandleCollisions, DrawTrajectory 等物理部分維持不變) ---
    public override void _Process(double delta)
    {
        while (udpReceiver != null && udpReceiver.GetAvailablePacketCount() > 0)
        {
            byte[] packet = udpReceiver.GetPacket();
            string jsonString = Encoding.UTF8.GetString(packet);
            pitchHistory.Add(jsonString);
            GD.Print($"📥 收到第 {pitchHistory.Count} 球數據。");
        }

        if (isSimulating)
        {
            if (isRolling) ProcessRolling((float)delta);
            else Rk4Step((float)delta);

            HandleCollisions();
            DrawTrajectory();

            // --- 🌟 階段四新增：每幀檢查是否被守備員接住 ---
            var defenseManager = GetNodeOrNull<DefenseManager>("../DefenseManager");
            if (defenseManager != null)
            {
                defenseManager.TrackRealBall(GlobalPosition, (float)delta);
                string catchResult = defenseManager.CheckCatch(GlobalPosition, hasBounced);
                if (catchResult != null)
                {
                    // 球被接住了！強制停止物理運算
                    isSimulating = false; 
                    velocity = Vector3.Zero;
                    
                    GD.Print($"🥊 [Baseball.cs] 球被守備員攔截！結果: {catchResult}");
                    
                    // 通知大螢幕和計分板 (ScoreBugUI)
                    UpdateDisplays(catchResult);
                }
            }
        }
    }

    public override void _Input(InputEvent @event)
    {
        if (@event is InputEventKey ek && ek.Pressed)
        {
            if (ek.Keycode == Key.N && pitchHistory.Count > 0)
            {
                currentPitchIndex = (currentPitchIndex + 1) % pitchHistory.Count;
                ResetPitch(pitchHistory[currentPitchIndex]);
            }
            else if (ek.Keycode == Key.R && currentPitchIndex >= 0)
            {
                ResetPitch(pitchHistory[currentPitchIndex]);
            }
        }
    }

    // --- 物理運算方法 ---
    private void Rk4Step(float dt) 
    { 
        Vector3 v0 = velocity;
        Vector3 a1 = GetAcceleration(v0); 
        Vector3 a2 = GetAcceleration(v0 + a1 * dt * 0.5f); 
        Vector3 a3 = GetAcceleration(v0 + a2 * dt * 0.5f); 
        Vector3 a4 = GetAcceleration(v0 + a3 * dt); 
        Vector3 dv = (a1 + 2 * a2 + 2 * a3 + a4) * (dt / 6.0f); 
        GlobalPosition += (velocity + dv * 0.5f) * dt; 
        velocity += dv; pathPoints.Add(GlobalPosition); 
    }
    private Vector3 GetAcceleration(Vector3 v) 
    { 
        float v_mag = v.Length(); 
        if (v_mag < 0.1f) 
            return GRAVITY; Vector3 drag = -0.5f * airDensity * ballArea * Cd * v_mag * v; 
        Vector3 magnus = Vector3.Zero; 
        if (spin.Length() > 0) 
            magnus = 0.5f * airDensity * ballArea * Cl * (v_mag * v_mag) * spin.Normalized().Cross(v.Normalized()); 
        return (GRAVITY * ballMass + drag + magnus) / ballMass; 
    }
    
    private void ProcessRolling(float dt) { float speed = new Vector2(velocity.X, velocity.Z).Length(); if (speed < 0.5f) { velocity = Vector3.Zero; isSimulating = false; isRolling = false; float x = GlobalPosition.X, z = GlobalPosition.Z; bool isFoul = (z > 0) || (Mathf.Abs(x) > Mathf.Abs(z)); float dist = Mathf.Sqrt(x * x + z * z); if (isFoul) displays?.ShowHitResult("FOUL BALL"); else displays?.ShowHitResult($"GROUND BALL\n{dist:F1} m"); } else { Vector3 decelDir = -velocity.Normalized(); decelDir.Y = 0; Vector3 deceleration = decelDir * 5.0f * dt; if (deceleration.Length() > speed) velocity = Vector3.Zero; else velocity += deceleration; GlobalPosition += velocity * dt; pathPoints.Add(GlobalPosition); } }
    
    private void HandleCollisions() { float x = GlobalPosition.X, y = GlobalPosition.Y, z = GlobalPosition.Z; float dist = Mathf.Sqrt(x * x + z * z); bool isFoul = (z > 0) || (Mathf.Abs(x) > Mathf.Abs(z)); if (!isFoul && dist >= wallDistance && !hasPassedWall) { if (y <= wallHeight) { isSimulating = false; displays?.ShowHitResult($"HIT WALL\n{dist:F1} m"); } else if (!hasBounced) { hasPassedWall = true; displays?.ShowHitResult($"HOME RUN!!\n{dist:F1} m"); isSimulating = false; } else { isSimulating = false; displays?.ShowHitResult($"GR DOUBLE\n{dist:F1} m"); } } if (y <= 0.037f && !isRolling) { GlobalPosition = new Vector3(x, 0.037f, z); hasBounced = true; if (Mathf.Abs(velocity.Y) > 1.0f) { velocity.Y = -velocity.Y * 0.5f; velocity.X *= 0.6f; velocity.Z *= 0.6f; } else { velocity.Y = 0; spin = Vector3.Zero; isRolling = true; } } }
    
    private float lineWidth = 0.08f; 

    private void DrawTrajectory() 
    {
        if (immediateMesh == null || pathPoints.Count < 2) return; 

        immediateMesh.ClearSurfaces(); 

        immediateMesh.SurfaceBegin(Mesh.PrimitiveType.TriangleStrip); 
        for (int i = 0; i < pathPoints.Count; i++) 
        {
            Vector3 p = pathPoints[i];
            
            Vector3 dir = (i < pathPoints.Count - 1) ? (pathPoints[i + 1] - p).Normalized() : (p - pathPoints[i - 1]).Normalized();
            
            Vector3 right = dir.Cross(Vector3.Up).Normalized();
            if (right.LengthSquared() < 0.001f) right = Vector3.Right; 
            
            Vector3 offset = right * (lineWidth * 0.5f);
            
            immediateMesh.SurfaceAddVertex(p + offset);
            immediateMesh.SurfaceAddVertex(p - offset);
        }
        immediateMesh.SurfaceEnd(); 

        immediateMesh.SurfaceBegin(Mesh.PrimitiveType.TriangleStrip); 
        for (int i = 0; i < pathPoints.Count; i++) 
        {
            Vector3 p = pathPoints[i];
            Vector3 dir = (i < pathPoints.Count - 1) ? (pathPoints[i + 1] - p).Normalized() : (p - pathPoints[i - 1]).Normalized();
            
            Vector3 right = dir.Cross(Vector3.Up).Normalized();
            if (right.LengthSquared() < 0.001f) right = Vector3.Right;
            
            Vector3 localUp = right.Cross(dir).Normalized(); 
            Vector3 offset = localUp * (lineWidth * 0.5f);
            
            immediateMesh.SurfaceAddVertex(p + offset);
            immediateMesh.SurfaceAddVertex(p - offset);
        }
        immediateMesh.SurfaceEnd(); 
    }
}