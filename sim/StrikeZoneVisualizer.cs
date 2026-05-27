using Godot;
using System;
using System.Text.Json;
using System.Text.Json.Serialization;

// 定義與 JSON 完全對應的資料結構
public class PitchData
{
    [JsonPropertyName("call")]
    public string Call { get; set; }

    [JsonPropertyName("x_cross_m")]
    public float XCrossM { get; set; } // 水平偏移 (左/右)

    [JsonPropertyName("z_cross_m")]
    public float ZCrossM { get; set; } // 進壘高度 (在 JSON 中稱為 Z)

    [JsonPropertyName("confidence")]
    public string Confidence { get; set; }

    [JsonPropertyName("frames_with_both_cameras")]
    public int FramesWithBothCameras { get; set; }

    [JsonPropertyName("trajectory_points")]
    public int TrajectoryPoints { get; set; }

    [JsonPropertyName("notes")]
    public string Notes { get; set; }
}

public partial class StrikeZoneVisualizer : Node3D
{
    public override void _Ready()
    {
        // 模擬接收到的進壘數據 JSON
        string jsonString = @"{
            ""call"": ""STRIKE"",
            ""x_cross_m"": 0.08,
            ""z_cross_m"": 0.82,
            ""confidence"": ""high"",
            ""frames_with_both_cameras"": 42,
            ""trajectory_points"": 58,
            ""notes"": """"
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
            // Godot X = JSON x_cross_m (左右移動)
            // Godot Y = JSON z_cross_m (垂直高度)
            // Godot Z = 0 (球剛好通過本壘板前緣的瞬間)

            Vector3 crossingPoint = new Vector3(
                pitch.XCrossM, 
                pitch.ZCrossM, 
                0f
            );

            // 將此節點（例如一個代表球的 Mesh）移動到該進壘位置
            this.GlobalPosition = crossingPoint;

            // 輸出調試資訊
            GD.Print($"--- 進壘數據解析 ---");
            GD.Print($"判定: {pitch.Call} (信心度: {pitch.Confidence})");
            GD.Print($"座標: X(左右)={pitch.XCrossM}m, Y(高度)={pitch.ZCrossM}m, Z(深度)=0m");
        }
        catch (Exception e)
        {
            GD.PrintErr($"JSON 解析錯誤: {e.Message}");
        }
    }
}