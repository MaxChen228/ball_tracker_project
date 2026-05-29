using Godot;
using System;
using System.Text.Json;
using System.Text.Json.Serialization;

// 定義與 Ball Tracker 輸出 JSON 完全對應的資料結構
// 格式: {"pitch_x_m": 0.012, "pitch_y_m": 0.75, "call": "WAITING_UI", "exit_velocity_mph": 95.3, "launch_angle_deg": 18.5, "spray_angle_deg": -12.3, "spin_rate_rpm": 2200.0}
public class PitchData
{
    [JsonPropertyName("pitch_x_m")]
    public float PitchXM { get; set; }

    [JsonPropertyName("pitch_y_m")]
    public float PitchYM { get; set; }

    [JsonPropertyName("call")]
    public string Call { get; set; }

    [JsonPropertyName("exit_velocity_mph")]
    public float ExitVelocityMph { get; set; }

    [JsonPropertyName("launch_angle_deg")]
    public float LaunchAngleDeg { get; set; }

    [JsonPropertyName("spray_angle_deg")]
    public float SprayAngleDeg { get; set; }

    [JsonPropertyName("spin_rate_rpm")]
    public float SpinRateRpm { get; set; }
}

public partial class StrikeZoneVisualizer : Node3D
{
    public override void _Ready()
    {
        // 模擬接收到的進壘數據 JSON (Ball Tracker 格式)
        string jsonString = @"{
            ""pitch_x_m"": 0.012,
            ""pitch_y_m"": 0.75,
            ""call"": ""WAITING_UI"",
            ""exit_velocity_mph"": 95.3,
            ""launch_angle_deg"": 18.5,
            ""spray_angle_deg"": -12.3,
            ""spin_rate_rpm"": 2200.0
        }";

        ParseAndShowPitch(jsonString);
    }

    private void ParseAndShowPitch(string json)
    {
        try
        {
            PitchData pitch = JsonSerializer.Deserialize<PitchData>(json);

            // --- 座標系映射邏輯 ---
            // 1. 本壘板位於原點 (0, 0, 0)
            // 2. 投手丘位於 -Z 方向 (-18.44m)
            // 3. 外野位於 +Z 方向
            
            // 轉換對應：
            // Godot X = JSON pitch_x_m (左右偏移)
            // Godot Y = JSON pitch_y_m (進壘高度)
            // Godot Z = 0 (球剛好通過本壘板前緣的瞬間)
            // 打擊參數：初速、發射角度、打線角度、旋轉轉速

            Vector3 crossingPoint = new Vector3(
                pitch.PitchXM, 
                pitch.PitchYM, 
                0f
            );

            // 將此節點（例如一個代表球的 Mesh）移動到該進壘位置
            this.GlobalPosition = crossingPoint;

            // 輸出調試資訊
            GD.Print($"--- 進壘數據解析 ---");
            GD.Print($"判定: {pitch.Call}");
            GD.Print($"座標: X(左右)={pitch.PitchXM}m, Y(高度)={pitch.PitchYM}m, Z(深度)=0m");
            GD.Print($"初速: {pitch.ExitVelocityMph} mph, 發射角: {pitch.LaunchAngleDeg}°, 打線角: {pitch.SprayAngleDeg}°, 轉速: {pitch.SpinRateRpm} rpm");
        }
        catch (Exception e)
        {
            GD.PrintErr($"JSON 解析錯誤: {e.Message}");
        }
    }
}