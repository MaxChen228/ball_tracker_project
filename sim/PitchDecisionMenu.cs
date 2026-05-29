using Godot;
using System;

public partial class PitchDecisionMenu : PanelContainer
{
    // ==========================================
    // 1. 訊號 (Signal) 定義區
    // ==========================================
    [Signal] public delegate void DecisionSelectedEventHandler(string decision);

    // ==========================================
    // 2. 內部變數
    // ==========================================
    private Button[] buttons;
    private int selectedIndex = 0;
    private VBoxContainer vboxContainer;
    private PanelContainer panelContainer;
    
    // 選中視覺反饋
    private Color normalFontColor = new Godot.Color(1f, 1f, 1f, 1f);
    private Color selectedFontColor = new Godot.Color(1f, 0.85f, 0.1f, 1f); // 黃色
    
    // 按鈕原始文字
    private string[] originalTexts;

    public override void _Ready()
    {
        panelContainer = this;
        vboxContainer = GetNodeOrNull<VBoxContainer>("VBoxContainer");
        if (vboxContainer == null) return;

        // 收集所有按鈕
        var btnList = new System.Collections.Generic.List<Button>();
        foreach (var child in vboxContainer.GetChildren())
        {
            if (child is Button btn)
            {
                string id = btn.Name.ToString().Replace("Btn", "").ToUpper();
                btn.Pressed += () => OnButtonPressed(id);
                btnList.Add(btn);
            }
        }
        buttons = btnList.ToArray();
        selectedIndex = 0;
        
        // 儲存原始文字
        originalTexts = new string[buttons.Length];
        for (int i = 0; i < buttons.Length; i++)
        {
            originalTexts[i] = buttons[i].Text;
            buttons[i].AddThemeColorOverride("font_color", normalFontColor);
        }

        UpdateSelection();
        UpdateSize();
        
        // 監聽視窗大小變化
        GetViewport().SizeChanged += OnSizeChanged;
    }

    private void OnSizeChanged()
    {
        UpdateSize();
    }

    private void UpdateSize()
    {
        var vp = GetViewport().GetVisibleRect().Size;
        float panelW = Math.Max(300f, vp.X * 0.25f);
        float panelH = Math.Max(200f, vp.Y * 0.3f);
        
        // 居中定位
        panelContainer.Position = new Vector2(vp.X / 2 - panelW / 2, vp.Y / 2 - panelH / 2);
        panelContainer.Size = new Vector2(panelW, panelH);
        
        // 更新按鈕寬度
        float btnW = panelW - 40;
        foreach (var btn in buttons)
        {
            if (btn != null)
            {
                float btnH = Math.Max(35f, vp.Y * 0.06f);
                btn.CustomMinimumSize = new Vector2(btnW, btnH);
                btn.AddThemeFontSizeOverride("font_size", (int)(btnH * 0.6f));
            }
        }
    }

    public override void _Input(InputEvent @event)
    {
        if (!Visible) return;

        // Up/Down 鍵切換選中
        if (@event is InputEventKey key && key.Pressed)
        {
            if (key.Keycode == Key.Up || key.Keycode == Key.W)
            {
                selectedIndex = (selectedIndex - 1 + buttons.Length) % buttons.Length;
                UpdateSelection();
                GetViewport().SetInputAsHandled();
            }
            else if (key.Keycode == Key.Down || key.Keycode == Key.S)
            {
                selectedIndex = (selectedIndex + 1) % buttons.Length;
                UpdateSelection();
                GetViewport().SetInputAsHandled();
            }
            else if (key.Keycode == Key.Enter || key.Keycode == Key.Space)
            {
                // Enter/Space 確認選中
                if (selectedIndex >= 0 && selectedIndex < buttons.Length && buttons[selectedIndex] != null)
                {
                    string id = buttons[selectedIndex].Name.ToString().Replace("Btn", "").ToUpper();
                    OnButtonPressed(id);
                    GetViewport().SetInputAsHandled();
                }
            }
        }
    }

    private void UpdateSelection()
    {
        for (int i = 0; i < buttons.Length; i++)
        {
            if (buttons[i] == null) continue;
            if (i == selectedIndex)
            {
                // 選中：黃色文字 + 箭頭標記
                buttons[i].AddThemeColorOverride("font_color", selectedFontColor);
                buttons[i].Text = "► " + originalTexts[i];
            }
            else
            {
                // 未選中：白色文字
                buttons[i].RemoveThemeStyleboxOverride("normal");
                buttons[i].RemoveThemeColorOverride("font_color");
                buttons[i].Text = originalTexts[i];
            }
        }
    }

    // ==========================================
    // 3. 按鈕點擊後的處理邏輯
    // ==========================================
    private void OnButtonPressed(string decision)
    {
        EmitSignal(SignalName.DecisionSelected, decision);
        this.Visible = false; 
        Input.MouseMode = Input.MouseModeEnum.Captured; 
    }

    // ==========================================
    // 4. 顯示選單的外部呼叫接口
    // ==========================================
    public void ShowMenu()
    {
        CallDeferred("set_visible", true);
        selectedIndex = 0;
        UpdateSelection();
        Input.MouseMode = Input.MouseModeEnum.Captured;
    }
}
