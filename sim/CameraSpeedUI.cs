using Godot;
using System;

public partial class CameraSpeedUI : CanvasLayer
{
    [Export] public NodePath CameraNodePath;

    private BallTrackingCamera camera;
    private bool isVisible = false;
    private HSlider speedSlider;
    private Label speedLabel;
    private PanelContainer panel;

    public override void _Ready()
    {
        camera = GetNodeOrNull<BallTrackingCamera>(CameraNodePath);
        if (camera == null)
        {
            // Fallback: search for Camera3D in the scene tree
            var root = GetTree().Root;
            foreach (var node in root.GetChildren())
            {
                foreach (var child in node.GetChildren())
                {
                    if (child.Name == "Camera3D")
                    {
                        camera = child as BallTrackingCamera;
                        break;
                    }
                }
                if (camera != null) break;
            }
        }
        GD.Print($"[CameraSpeedUI] camera: {(camera != null ? "FOUND" : "NULL")}");
        BuildUI();
        Visible = true;
    }

    private void BuildUI()
    {
        var vp = GetViewport().GetVisibleRect().Size;
        float panelW = Math.Max(280f, vp.X * 0.15f);
        float panelH = Math.Max(120f, vp.Y * 0.15f);
        float fontSize = Math.Max(18f, vp.Y * 0.025f);
        
        panel = new PanelContainer();
        panel.AddThemeStyleboxOverride("panel", new StyleBoxFlat());
        panel.Size = new Vector2(panelW, panelH);
        panel.Position = new Vector2(vp.X - panelW - 20, 20);

        var vbox = new VBoxContainer();
        vbox.AddThemeConstantOverride("separation", 12);
        panel.AddChild(vbox);

        var title = new Label { Text = "Camera Speed" };
        title.AddThemeFontSizeOverride("font_size", (int)fontSize);
        title.AddThemeColorOverride("font_color", Colors.White);
        vbox.AddChild(title);

        speedSlider = new HSlider
        {
            MinValue = 10,
            MaxValue = 100,
            Step = 1,
            Value = 50,
            CustomMinimumSize = new Vector2(panelW - 40, 30)
        };
        speedSlider.ValueChanged += OnSliderChanged;
        vbox.AddChild(speedSlider);

        speedLabel = new Label { Text = "Speed: 50" };
        speedLabel.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.8f));
        speedLabel.AddThemeColorOverride("font_color", Colors.White);
        vbox.AddChild(speedLabel);

        AddChild(panel);
    }

    private void OnSliderChanged(double value)
    {
        if (camera != null)
        {
            camera.SmoothSpeed = (float)value;
            GD.Print($"[CameraSpeedUI] SmoothSpeed changed to: {camera.SmoothSpeed}");
        }
        else
        {
            GD.PrintErr("[CameraSpeedUI] camera is NULL! Cannot update speed.");
        }
        speedLabel.Text = $"Speed: {((int)value)}";
    }

    public override void _Input(InputEvent @event)
    {
        if (@event is InputEventKey key && key.Pressed && key.Keycode == Key.U)
        {
            isVisible = !isVisible;
            Visible = isVisible;
            if (isVisible)
            {
                Input.MouseMode = Input.MouseModeEnum.Visible;
            }
            else
            {
                Input.MouseMode = Input.MouseModeEnum.Hidden;
            }
        }
    }
}
