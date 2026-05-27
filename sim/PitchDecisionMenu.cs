using Godot;
using System;

public partial class PitchDecisionMenu : PanelContainer
{
    // ==========================================
    // 1. 訊號 (Signal) 定義區
    // ==========================================
    // [Signal] 是 Godot 特有的機制，類似於廣播系統。
    // 這裡我們定義了一個名為 "DecisionSelected" 的廣播頻道，並且規定廣播時必須夾帶一個字串 (string decision) 作為訊息內容。
    // Baseball.cs 那邊就是透過訂閱這個頻道，來知道玩家選了什麼。
    [Signal] public delegate void DecisionSelectedEventHandler(string decision);

    // ==========================================
    // 2. 初始化函數 _Ready()
    // ==========================================
    // 就像 C++ 的 main() 或是物件導向的建構子，當這個 UI 節點第一次出現在遊戲場景中時，Godot 會自動執行一次這個函數。
    public override void _Ready()
    {
        // 嘗試在子節點中尋找名為 "VBoxContainer" 的排版容器。
        // 使用 GetNodeOrNull 是一種防呆機制，如果找不到節點會回傳 null，而不會直接讓程式崩潰。
        var container = GetNodeOrNull<VBoxContainer>("VBoxContainer");
        if (container == null) return; // 如果沒找到容器，就直接結束初始化，避免後續出錯。

        // 取得該容器底下的所有子節點，並使用 foreach 迴圈一一檢查。
        foreach (var child in container.GetChildren())
        {
            // 類型檢查 (Type Checking)：確認這個子節點是不是「按鈕 (Button)」。
            // 如果是，就把他當作按鈕 (命名為 btn) 來操作；如果不是 (例如只是一段文字 Label)，就跳過。
            if (child is Button btn)
            {
                // 【核心巧思：動態生成 ID】
                // 讀取按鈕的節點名稱 (例如 "BtnNoSwing")。
                // 將 "Btn" 這個字眼替換成空白 (變成 "NoSwing")，然後全部轉大寫 (變成 "NOSWING")。
                // 這樣我們就有了一個乾淨的識別碼 (ID)，未來在編輯器新增按鈕時，程式都能自動辨識。
                string id = btn.Name.ToString().Replace("Btn", "").ToUpper();
                
                // 【事件綁定】
                // btn.Pressed 是按鈕被點擊時會觸發的內建事件。
                // += 代表「註冊」一個動作。
                // () => OnButtonPressed(id) 是一個 C# 的 Lambda 匿名函式。
                // 白話文：「當這個按鈕被按下去時，請幫我執行 OnButtonPressed 函數，並把剛剛算出來的 id 傳進去。」
                btn.Pressed += () => OnButtonPressed(id);
            }
        }
    }

    // ==========================================
    // 3. 按鈕點擊後的處理邏輯
    // ==========================================
    // 這是我們自訂的函數，當任何一個按鈕被點擊時，都會呼叫這裡。
    private void OnButtonPressed(string decision)
    {
        // 發射訊號 (EmitSignal)：對全遊戲廣播 "DecisionSelected" 事件，並把玩家選的 ID (decision) 廣播出去。
        // 這樣 Baseball.cs 就能立刻接收到指令。
        EmitSignal(SignalName.DecisionSelected, decision);
        
        // 點擊完成後，自動把這個 UI 選單隱藏起來。
        this.Visible = false; 
        
        // 將滑鼠游標「鎖定 (Captured)」回遊戲視窗中心，並且隱藏游標。
        // 這是 3D 遊戲常用的設定，讓玩家可以繼續用滑鼠轉動視角，而不會讓游標跑出遊戲視窗外。
        Input.MouseMode = Input.MouseModeEnum.Captured; 
    }

    // ==========================================
    // 4. 顯示選單的外部呼叫接口
    // ==========================================
    // 這個函數是開放給外部 (例如 Baseball.cs) 呼叫的。當球的判決為 0 時，Baseball 會呼叫這裡來叫出選單。
    public void ShowMenu()
    {
        // 讓原本隱藏的 UI 選單顯示出來。
        this.Visible = true;
        
        // 將滑鼠游標狀態設為「可見且自由移動 (Visible)」。
        // 這樣玩家才能移動游標去點擊螢幕上的 UI 按鈕。
        Input.MouseMode = Input.MouseModeEnum.Visible; 
    }
}