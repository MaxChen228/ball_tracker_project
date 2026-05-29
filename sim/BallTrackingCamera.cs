using Godot;
using System;

public partial class BallTrackingCamera : Camera3D
{
    // 【設定】在 Godot 編輯器中，把場景裡的「baseball」節點拖進來
    [Export] public Node3D BallTarget; 
    
    // 鏡頭與球的距離（上方 2 公尺，後方 5 公尺）
    [Export] public Vector3 Offset = new Vector3(0, 2, 5); 
    
    // 跟隨的平滑度（數值越大跟得越緊）
    [Export] public float SmoothSpeed = 50.0f;

    // --- 追蹤模式下的軌道設定 ---
    private float orbitYaw = 0.0f;
    private float orbitPitch = 0.0f;
    private float orbitRadius = 8.0f;
    private float orbitSensitivity = 0.003f;
    private float minOrbitRadius = 2.0f;
    private float maxOrbitRadius = 30.0f;

    // --- 自由鏡頭設定 ---
    private float mouseSensitivity = 0.003f;
    private float moveSpeed = 15.0f;
    private float pitch = 0.0f;
    private float yaw = 0.0f;

    private bool isTracking = false;

    public override void _Ready()
    {
        Input.MouseMode = Input.MouseModeEnum.Captured;
        
        // 讀取鏡頭原本的角度
        Vector3 rot = Rotation;
        pitch = rot.X;
        yaw = rot.Y;
        
        // 確保 BallTarget 有值 — 如果 export 沒綁定，用 NodePath 補救
        if (BallTarget == null)
        {
            // 嘗試多種路徑找球
            BallTarget = GetNodeOrNull<Node3D>("Baseball");
            if (BallTarget == null)
            {
                BallTarget = GetNodeOrNull<Node3D>("/Stadium/Baseball");
            }
            if (BallTarget == null)
            {
                // 最後手段：搜尋場景中所有 Node3D
                var root = GetTree().Root;
                foreach (var node in root.GetChildren())
                {
                    foreach (var child in node.GetChildren())
                    {
                        string name = child.Name.ToString();
                        if (name.Contains("Ball", StringComparison.OrdinalIgnoreCase))
                        {
                            BallTarget = child as Node3D;
                            break;
                        }
                    }
                    if (BallTarget != null) break;
                }
            }
        }
        GD.Print($"[BallTrackingCamera] BallTarget: {(BallTarget != null ? BallTarget.Name : "NULL")}");
    }

    public override void _UnhandledInput(InputEvent @event)
    {
        // ESC 切換滑鼠游標
        if (@event is InputEventKey eventKey && eventKey.Pressed && eventKey.Keycode == Key.Escape)
        {
            Input.MouseMode = (Input.MouseMode == Input.MouseModeEnum.Captured) 
                ? Input.MouseModeEnum.Visible 
                : Input.MouseModeEnum.Captured;
        }

        // 滑鼠旋轉 (只有在非追蹤模式，或追蹤模式關閉時才允許手動旋轉)
        if (Input.MouseMode == Input.MouseModeEnum.Captured && !isTracking && @event is InputEventMouseMotion mouseMotion)
        {
            yaw -= mouseMotion.Relative.X * mouseSensitivity;
            pitch -= mouseMotion.Relative.Y * mouseSensitivity;
            pitch = Mathf.Clamp(pitch, Mathf.DegToRad(-89f), Mathf.DegToRad(89f));
            Rotation = new Vector3(pitch, yaw, 0);
        }

        // 🌟 追蹤模式下：滑鼠用於軌道旋轉（繞著球轉）
        if (Input.MouseMode == Input.MouseModeEnum.Captured && isTracking && @event is InputEventMouseMotion mouseMotion2)
        {
            orbitYaw -= mouseMotion2.Relative.X * orbitSensitivity;
            orbitPitch += mouseMotion2.Relative.Y * orbitSensitivity;
            orbitPitch = Mathf.Clamp(orbitPitch, Mathf.DegToRad(-80f), Mathf.DegToRad(80f));
        }
    }

    public override void _Process(double delta)
    {
        if (isTracking && BallTarget != null)
        {
            // --- 追蹤模式邏輯（軌道式） ---
            // 用軌道參數計算相機位置
            float x = orbitRadius * Mathf.Sin(orbitYaw) * Mathf.Cos(orbitPitch);
            float y = orbitRadius * Mathf.Sin(orbitPitch);
            float z = orbitRadius * Mathf.Cos(orbitYaw) * Mathf.Cos(orbitPitch);

            Vector3 desiredPosition = BallTarget.GlobalPosition + new Vector3(x, y, z);
            GlobalPosition = GlobalPosition.MoveToward(desiredPosition, (float)delta * SmoothSpeed);
            LookAt(BallTarget.GlobalPosition, Vector3.Up);
        }
        else if (isTracking && BallTarget == null)
        {
            // 追蹤已開但找不到球 — 印出警告
            GD.PrintErr("[BallTrackingCamera] isTracking=true but BallTarget is NULL!");
        }
        else
        {
            // --- 自由移動模式 (WASD) ---
            if (Input.MouseMode == Input.MouseModeEnum.Captured)
            {
                Vector3 direction = Vector3.Zero;
                if (Input.IsKeyPressed(Key.W)) direction -= Transform.Basis.Z;
                if (Input.IsKeyPressed(Key.S)) direction += Transform.Basis.Z;
                if (Input.IsKeyPressed(Key.A)) direction -= Transform.Basis.X;
                if (Input.IsKeyPressed(Key.D)) direction += Transform.Basis.X;
                if (Input.IsKeyPressed(Key.E)) direction += Vector3.Up;
                if (Input.IsKeyPressed(Key.Q)) direction += Vector3.Down;

                if (direction != Vector3.Zero)
                {
                    direction = direction.Normalized();
                    GlobalPosition += direction * moveSpeed * (float)delta;
                }
            }
        }
    }

    public override void _Input(InputEvent @event)
    {
        // 按下 'C' 鍵切換追蹤模式 (不受滑鼠模式影響)
        if (@event is InputEventKey key && key.Pressed && key.Keycode == Key.C)
        {
            isTracking = !isTracking;
            GD.Print($"[BallTrackingCamera] 模式切換: {(isTracking ? "追蹤球" : "WASD自由移動")}, BallTarget={BallTarget?.Name ?? "NULL"}");
            
            if (isTracking)
            {
                // 進入追蹤時鎖定滑鼠
                Input.MouseMode = Input.MouseModeEnum.Captured;
                // 初始化軌道角度（從球後方 8 公尺處開始）
                orbitYaw = 0.0f;
                orbitPitch = Mathf.DegToRad(15f);
                orbitRadius = 8.0f;
            }
            // 離開追蹤時保持 Captured 模式，讓 WASD + 滑鼠旋轉繼續運作
        }

        // 追蹤模式下：滾輪調整距離（縮放）
        if (isTracking && @event is InputEventMouseButton mouseBtn)
        {
            if (mouseBtn.Pressed && mouseBtn.ButtonIndex == MouseButton.WheelUp)
            {
                orbitRadius = Mathf.Max(minOrbitRadius, orbitRadius - 1.0f);
                GD.Print($"[BallTrackingCamera] 軌道距離: {orbitRadius:F1}");
            }
            if (mouseBtn.Pressed && mouseBtn.ButtonIndex == MouseButton.WheelDown)
            {
                orbitRadius = Mathf.Min(maxOrbitRadius, orbitRadius + 1.0f);
                GD.Print($"[BallTrackingCamera] 軌道距離: {orbitRadius:F1}");
            }
        }
    }
}
