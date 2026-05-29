using Godot;
using System;

public partial class BallTrackingCam : Camera3D
{
    // Target ball node
    [Export] public Node3D BallTarget;

    // Camera offset from ball (behind and above)
    [Export] public Vector3 Offset = new Vector3(0, 3, 8);

    // Smoothness (higher = snaps faster)
    [Export] public float SmoothSpeed = 20.0f;

    // Mouse sensitivity for panning
    [Export] public float MouseSensitivity = 0.004f;

    // Orbit radius multiplier
    [Export] public float OrbitRadius = 8.0f;

    private float yaw = 0.0f;
    private float pitch = 0.0f;
    private bool isTracking = false;
    private float currentOrbitRadius;

    public override void _Ready()
    {
        Input.MouseMode = Input.MouseModeEnum.Captured;

        // Find ball target
        if (BallTarget == null)
        {
            BallTarget = GetNodeOrNull<Node3D>("Baseball");
            if (BallTarget == null)
            {
                BallTarget = GetNodeOrNull<Node3D>("/Stadium/Baseball");
            }
        }
        GD.Print($"[BallTrackingCam] BallTarget: {(BallTarget != null ? BallTarget.Name : "NULL")}");

        // Initialize orbit radius from offset
        currentOrbitRadius = Offset.Length();
    }

    public override void _UnhandledInput(InputEvent @event)
    {
        // ESC toggles mouse cursor
        if (@event is InputEventKey eventKey && eventKey.Pressed && eventKey.Keycode == Key.Escape)
        {
            Input.MouseMode = (Input.MouseMode == Input.MouseModeEnum.Captured)
                ? Input.MouseModeEnum.Visible
                : Input.MouseModeEnum.Captured;
        }

        // Cursor panning (only when tracking)
        if (Input.MouseMode == Input.MouseModeEnum.Captured && isTracking && @event is InputEventMouseMotion mouseMotion)
        {
            yaw -= mouseMotion.Relative.X * MouseSensitivity;
            pitch -= mouseMotion.Relative.Y * MouseSensitivity;
            pitch = Mathf.Clamp(pitch, Mathf.DegToRad(-80f), Mathf.DegToRad(80f));
        }
    }

    public override void _Process(double delta)
    {
        if (isTracking && BallTarget != null)
        {
            // Calculate desired position based on orbit
            float x = currentOrbitRadius * Mathf.Sin(yaw) * Mathf.Cos(pitch);
            float y = currentOrbitRadius * Mathf.Sin(pitch);
            float z = currentOrbitRadius * Mathf.Cos(yaw) * Mathf.Cos(pitch);

            Vector3 desiredPosition = BallTarget.GlobalPosition + new Vector3(x, y, z);
            GlobalPosition = GlobalPosition.MoveToward(desiredPosition, (float)delta * SmoothSpeed);
            LookAt(BallTarget.GlobalPosition, Vector3.Up);
        }
    }

    public override void _Input(InputEvent @event)
    {
        // 'T' key toggles tracking
        if (@event is InputEventKey key && key.Pressed && key.Keycode == Key.T)
        {
            isTracking = !isTracking;
            GD.Print($"[BallTrackingCam] 模式: {(isTracking ? "追蹤球" : "自由移動")}");

            if (isTracking)
            {
                Input.MouseMode = Input.MouseModeEnum.Captured;
            }
        }

        // Scroll wheel changes orbit radius
        if (@event is InputEventMouseButton mouseBtn && isTracking)
        {
            if (mouseBtn.Pressed && mouseBtn.ButtonIndex == MouseButton.WheelUp)
            {
                currentOrbitRadius = Mathf.Max(2.0f, currentOrbitRadius - 1.0f);
                GD.Print($"[BallTrackingCam] 距離: {currentOrbitRadius:F1}");
            }
            if (mouseBtn.Pressed && mouseBtn.ButtonIndex == MouseButton.WheelDown)
            {
                currentOrbitRadius = Mathf.Min(50.0f, currentOrbitRadius + 1.0f);
                GD.Print($"[BallTrackingCam] 距離: {currentOrbitRadius:F1}");
            }
        }
    }
}
