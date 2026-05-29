using Godot;
using System;

public partial class SettingsUI : CanvasLayer
{
    private bool isVisible = false;
    private PanelContainer panel;
    private VBoxContainer vbox;
    private Label resolutionLabel;
    private HSlider resolutionSlider;
    private Button closeButton;
    private Button applyButton;
    
    // 可用的解析度選項
    private int[] resolutions = { 1280, 1920, 2560, 3840 };
    private int currentResolutionIndex = 1; // 預設 1920

    public override void _Ready()
    {
        BuildUI();
        Visible = false;
    }

    private void BuildUI()
    {
        var vp = GetViewport().GetVisibleRect().Size;
        float panelW = Math.Max(500f, vp.X * 0.3f);
        float panelH = Math.Max(400f, vp.Y * 0.5f);
        float fontSize = Math.Max(16f, vp.Y * 0.03f);
        
        // 背景面板
        panel = new PanelContainer();
        panel.AddThemeStyleboxOverride("panel", new StyleBoxFlat());
        panel.Size = new Vector2(panelW, panelH);
        panel.Position = new Vector2(
            vp.X / 2 - panelW / 2,
            vp.Y / 2 - panelH / 2
        );

        vbox = new VBoxContainer();
        vbox.AddThemeConstantOverride("separation", 20);
        panel.AddChild(vbox);

        // 標題
        var title = new Label { Text = "Settings" };
        title.AddThemeFontSizeOverride("font_size", (int)(fontSize * 2));
        title.AddThemeColorOverride("font_color", Colors.White);
        vbox.AddChild(title);

        // 解析度設定
        var resSection = new VBoxContainer();
        resSection.AddThemeConstantOverride("separation", 10);
        var resTitle = new Label { Text = "Resolution" };
        resTitle.AddThemeFontSizeOverride("font_size", (int)fontSize);
        resTitle.AddThemeColorOverride("font_color", Colors.White);
        resSection.AddChild(resTitle);

        resolutionLabel = new Label { Text = "1920 x 1080", HorizontalAlignment = HorizontalAlignment.Center };
        resolutionLabel.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.9f));
        resolutionLabel.AddThemeColorOverride("font_color", Colors.CornflowerBlue);
        resSection.AddChild(resolutionLabel);

        resolutionSlider = new HSlider
        {
            MinValue = 0,
            MaxValue = resolutions.Length - 1,
            Step = 1,
            Value = currentResolutionIndex,
            CustomMinimumSize = new Vector2(panelW - 80, 30)
        };
        resolutionSlider.ValueChanged += OnResolutionChanged;
        resSection.AddChild(resolutionSlider);

        var resInfo = new Label { Text = "(Drag to change width)", HorizontalAlignment = HorizontalAlignment.Center };
        resInfo.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.7f));
        resInfo.AddThemeColorOverride("font_color", new Godot.Color(0.7f, 0.7f, 0.7f, 1f));
        resSection.AddChild(resInfo);

        vbox.AddChild(resSection);

        // 分隔線
        var hSeparator = new HSeparator();
        vbox.AddChild(hSeparator);

        // 按鈕區
        var btnContainer = new HBoxContainer();
        btnContainer.AddThemeConstantOverride("separation", 20);

        applyButton = new Button { Text = "Apply" };
        applyButton.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.9f));
        applyButton.Pressed += OnApply;
        btnContainer.AddChild(applyButton);

        closeButton = new Button { Text = "Close" };
        closeButton.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.9f));
        closeButton.Pressed += OnClose;
        btnContainer.AddChild(closeButton);

        vbox.AddChild(btnContainer);

        AddChild(panel);
    }

    private void OnResolutionChanged(double value)
    {
        currentResolutionIndex = (int)value;
        int width = resolutions[currentResolutionIndex];
        int height = width * 9 / 16;
        resolutionLabel.Text = $"{width} x {height}";
    }

    private void OnApply()
    {
        int width = resolutions[currentResolutionIndex];
        int height = width * 9 / 16;
        
        // 透過 ProjectSettings 設定 (Godot 4 正確 API)
        try
        {
            ProjectSettings.SetSetting("display/window/size/viewport_width", (Variant)width);
            ProjectSettings.SetSetting("display/window/size/viewport_height", (Variant)height);
            ProjectSettings.Save();
            
            // 即時套用
            DisplayServer.WindowSetSize(new Vector2I(width, height));
            
            GD.Print($"[SettingsUI] Resolution changed to {width} x {height}");
            OnClose();
            return;
        }
        catch
        {
            // 如果 ProjectSettings 不行，寫入 project.godot
        }
        
        // 寫入 project.godot 檔案 (Godot 4 C# 用 FileAccess)
        string projectPath = "res://project.godot";
        
        // 讀取
        FileAccess fileRead = FileAccess.Open(projectPath, FileAccess.ModeFlags.Read);
        string content = fileRead.GetAsText();
        fileRead.Close();
        
        // 替換 viewport_width
        content = System.Text.RegularExpressions.Regex.Replace(content, 
            @"window/size/viewport_width=\d+", 
            $"window/size/viewport_width={width}");
        
        // 替換 viewport_height
        content = System.Text.RegularExpressions.Regex.Replace(content, 
            @"window/size/viewport_height=\d+", 
            $"window/size/viewport_height={height}");
        
        // 寫入
        FileAccess fileWrite = FileAccess.Open(projectPath, FileAccess.ModeFlags.Write);
        fileWrite.StoreString(content);
        fileWrite.Close();
        
        GD.Print($"[SettingsUI] Resolution saved to project.godot: {width} x {height}");
        GD.Print("Please restart the project for changes to take effect.");
        
        OnClose();
    }

    private void OnClose()
    {
        Visible = false;
        isVisible = false;
        Input.MouseMode = Input.MouseModeEnum.Captured;
    }

    public override void _Input(InputEvent @event)
    {
        // 按下 'X' 鍵切換顯示
        if (@event is InputEventKey key && key.Pressed && key.Keycode == Key.X)
        {
            isVisible = !isVisible;
            Visible = isVisible;
            
            if (isVisible)
            {
                Input.MouseMode = Input.MouseModeEnum.Visible;
            }
            else
            {
                Input.MouseMode = Input.MouseModeEnum.Captured;
            }
        }
    }
}
