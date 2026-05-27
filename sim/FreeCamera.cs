using Godot;
using System;

public partial class FreeCamera : Camera3D
{
    // --- 攝影機設定 ---
    private float mouseSensitivity = 0.003f; // 滑鼠靈敏度
    private float moveSpeed = 15.0f;         // 飛行速度

    // --- 旋轉角度變數 ---
    private float pitch = 0.0f; // 仰角 (上下)
    private float yaw = 0.0f;   // 偏航角 (左右)

    public override void _Ready()
    {
        // 啟動時，將滑鼠游標鎖定在視窗內並隱藏
        Input.MouseMode = Input.MouseModeEnum.Captured;

        // 讀取攝影機原本在場景中設定的角度，避免啟動時視角突然跳動
        Vector3 rot = Rotation;
        pitch = rot.X;
        yaw = rot.Y;
    }

    public override void _UnhandledInput(InputEvent @event)
    {
        // 按下 ESC 鍵可以釋放滑鼠，再次按下可重新鎖定
        if (@event is InputEventKey eventKey && eventKey.Pressed && eventKey.Keycode == Key.Escape)
        {
            if (Input.MouseMode == Input.MouseModeEnum.Captured)
                Input.MouseMode = Input.MouseModeEnum.Visible;
            else
                Input.MouseMode = Input.MouseModeEnum.Captured;
        }

        // 處理滑鼠移動 (只有當滑鼠被鎖定時才轉動視角)
        if (Input.MouseMode == Input.MouseModeEnum.Captured && @event is InputEventMouseMotion mouseMotion)
        {
            // 根據滑鼠移動量更新角度
            yaw -= mouseMotion.Relative.X * mouseSensitivity;
            pitch -= mouseMotion.Relative.Y * mouseSensitivity;

            // 限制上下看角度，避免「翻過去」(-89度到89度)
            pitch = Mathf.Clamp(pitch, Mathf.DegToRad(-89f), Mathf.DegToRad(89f));

            // 套用旋轉到攝影機 (Z 軸設為 0，避免畫面歪斜)
            Rotation = new Vector3(pitch, yaw, 0);
        }
    }

    public override void _Process(double delta)
    {
        // 如果滑鼠沒有被鎖定，就不處理移動 (代表你可能想點擊其他視窗或 UI)
        if (Input.MouseMode != Input.MouseModeEnum.Captured)
            return;

        Vector3 direction = Vector3.Zero;

        // 讀取 WASD 按鍵狀態。
        // Transform.Basis 可以取得物件「當前的朝向」，Z 軸負方向為前方。
        if (Input.IsKeyPressed(Key.W)) direction -= Transform.Basis.Z; // 往前前進
        if (Input.IsKeyPressed(Key.S)) direction += Transform.Basis.Z; // 往後退
        if (Input.IsKeyPressed(Key.A)) direction -= Transform.Basis.X; // 往左移
        if (Input.IsKeyPressed(Key.D)) direction += Transform.Basis.X; // 往右移

        // Q/E 控制絕對高度的升降
        if (Input.IsKeyPressed(Key.E)) direction += Vector3.Up;   // 上升
        if (Input.IsKeyPressed(Key.Q)) direction += Vector3.Down; // 下降

        // 正規化方向向量，避免斜走(如同時按 W 和 D)時速度變快
        if (direction != Vector3.Zero)
        {
            direction = direction.Normalized();
        }

        // 根據速度與時間差更新位置
        GlobalPosition += direction * moveSpeed * (float)delta;
    }
}