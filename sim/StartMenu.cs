using Godot;
using System;

public partial class StartMenu : Control
{
    public override void _Ready()
    {
        // 進入選單時，確保滑鼠鼠標是顯示出來且可以自由移動的
        Input.MouseMode = Input.MouseModeEnum.Visible;
    }

    // 將你的 Button 的 "pressed" 訊號連接到這個函式
    public void OnStartButtonPressed()
    {
        GetTree().ChangeSceneToFile("res://stadium.tscn");
    }
}