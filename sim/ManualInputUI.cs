using Godot;
using System;
using System.Text.Json;

public partial class ManualInputUI : CanvasLayer
{
	[Export] public NodePath BaseballNodePath;

	private bool isVisible = false;
	private Baseball baseball;

	private PanelContainer panel;
	private VBoxContainer vbox;
	private LineEdit[] lineEdits;
	private Button submitButton;

	public override void _Ready()
	{
		baseball = GetNodeOrNull<Baseball>(BaseballNodePath);
		if (baseball == null)
		{
			// Fallback: use absolute path
			baseball = GetNodeOrNull<Baseball>("/Baseball");
		}
		GD.Print($"[ManualInputUI] baseball node: {(baseball != null ? "FOUND" : "NULL")}");
		BuildUI();
		Visible = false;
	}

	private void BuildUI()
	{
		var vp = GetViewport().GetVisibleRect().Size;
		float panelW = Math.Max(320f, vp.X * 0.2f);
		float panelH = Math.Max(320f, vp.Y * 0.4f);
		float fontSize = Math.Max(16f, vp.Y * 0.03f);
		
		// 1. 背景面板 (使用預設樣式，不設背景色以避開 API 差異)
		panel = new PanelContainer();
		panel.AddThemeStyleboxOverride("panel", new StyleBoxFlat());
		panel.Size = new Vector2(panelW, panelH);
		// 修正：Godot 4 使用 GetViewport().GetVisibleRect().Size
		panel.Position = new Vector2(20, vp.Y - panelH - 20);

		// 2. 垂直排列容器
		vbox = new VBoxContainer();
		vbox.AddThemeConstantOverride("separation", 10);
		panel.AddChild(vbox);

		// 3. 標題
		var title = new Label { Text = "Manual Hit Data" };
		title.AddThemeFontSizeOverride("font_size", (int)(fontSize * 1.5f));
		// 修正：Godot 4 使用 AddThemeColorOverride
		title.AddThemeColorOverride("font_color", Colors.White);
		vbox.AddChild(title);

		// 4. 輸入欄位
		string[] labels = { "Exit Velocity (mph)", "Launch Angle (deg)", "Spray Angle (deg)", "Spin Rate (rpm)" };
		lineEdits = new LineEdit[4];
		float inputW = panelW - 40;
		for (int i = 0; i < 4; i++)
		{
			var hBox = new HBoxContainer();
			var lbl = new Label { Text = labels[i] };
			lbl.CustomMinimumSize = new Vector2(inputW * 0.45f, 30);
			lbl.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.85f));
			hBox.AddChild(lbl);
			
			var line = new LineEdit { PlaceholderText = "0.0" };
			line.CustomMinimumSize = new Vector2(inputW * 0.5f, 30);
			line.AddThemeFontSizeOverride("font_size", (int)(fontSize * 0.85f));
			hBox.AddChild(line);
			lineEdits[i] = line;
			vbox.AddChild(hBox);
		}

		// 5. 提交按鈕
		submitButton = new Button { Text = "Simulate Hit" };
		submitButton.AddThemeFontOverride("font", title.GetThemeFont("font"));
		submitButton.AddThemeColorOverride("font_color", Colors.White);
		submitButton.AddThemeFontSizeOverride("font_size", (int)(fontSize * 1.1f));
		// 修正：Godot 4 使用 += 訂閱信號
		submitButton.Pressed += OnSubmit;
		vbox.AddChild(submitButton);

		// 6. 關閉按鈕
		var closeBtn = new Button { Text = "Close" };
		closeBtn.AddThemeFontOverride("font", title.GetThemeFont("font"));
		closeBtn.AddThemeColorOverride("font_color", Colors.White);
		closeBtn.AddThemeFontSizeOverride("font_size", (int)(fontSize * 1.1f));
		closeBtn.Pressed += () => { Visible = false; isVisible = false; Input.MouseMode = Input.MouseModeEnum.Hidden; };
		vbox.AddChild(closeBtn);

		AddChild(panel);
	}

	private void OnSubmit()
	{
		if (baseball == null)
		{
			GD.PrintErr("❌ [ManualInputUI] baseball is NULL! Check NodePath export.");
			return;
		}

		try
		{
			// 讀取輸入並轉換為 JSON
			float ev = float.Parse(lineEdits[0].Text);
			float la = float.Parse(lineEdits[1].Text);
			float sa = float.Parse(lineEdits[2].Text);
			float sr = float.Parse(lineEdits[3].Text);

			// 補上 PitchDecisionMenu 需要的額外欄位
			var data = new {
				exit_velocity_mph = ev,
				launch_angle_deg = la,
				spray_angle_deg = sa,
				spin_rate_rpm = sr,
				pitch_x_m = 0.0f,
				pitch_y_m = 1.0f,
				call = "IN_PLAY"
			};

			string json = JsonSerializer.Serialize(data);
			GD.Print($"[ManualInputUI] Sent JSON: {json}");
			
			// 呼叫 Baseball.cs 處理
			baseball.ManualPitchData(json);
			
			// 關閉視窗
			Visible = false;
			isVisible = false;
			Input.MouseMode = Input.MouseModeEnum.Hidden;
		}
		catch (Exception e)
		{
			GD.PrintErr($"Input Error: {e.Message}");
		}
	}

	public override void _Input(InputEvent @event)
	{
		// 按下 'I' 鍵切換顯示
		if (@event is InputEventKey key && key.Pressed && key.Keycode == Key.I)
		{
			isVisible = !isVisible;
			Visible = isVisible;
			
			if (isVisible)
			{
				Input.MouseMode = Input.MouseModeEnum.Visible; // 允許點擊輸入框
				panel.GrabFocus();
			}
		else
		{
			Input.MouseMode = Input.MouseModeEnum.Captured; // 恢復游標控制
		}
		}
	}
}
