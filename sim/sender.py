import argparse
import socket
import json
import numpy as np
import os

# ==========================================
# 全域 socket（由 setup_sockets / main 設定）
# ==========================================
send_sock = None
recv_sock = None
GODOT_IP = "127.0.0.1"
GODOT_PORT = 9999
RECEIVE_IP = "0.0.0.0"
RECEIVE_PORT = 8888

# ==========================================
# 核心演算法：特徵萃取 (X-Z 降維 + Z 軸反轉)
# ==========================================
def extract_hit_features(trajectory_data, k=3):
    n = len(trajectory_data)
    if n < 3:
        return {"is_hit": False}

    for i in range(1, n - 1):
        p_prev = trajectory_data[i-1]
        p_cur = trajectory_data[i]
        p_next = trajectory_data[i+1]

        # 1. 物理防呆：檢查 Z 軸速度是否反轉
        vz_before = p_cur['z'] - p_prev['z']
        vz_after = p_next['z'] - p_cur['z']
        if vz_before * vz_after >= 0:
            continue

        # 2. 幾何降維：提取 X-Z 平面向量
        v1_xz = np.array([p_cur['x'] - p_prev['x'], p_cur['z'] - p_prev['z']])
        v2_xz = np.array([p_next['x'] - p_cur['x'], p_next['z'] - p_cur['z']])

        # 3. 雜訊過濾
        if np.linalg.norm(v1_xz) < 1e-3 or np.linalg.norm(v2_xz) < 1e-3:
            continue

        # 4. 內積找折點
        cos_theta = np.dot(v1_xz, v2_xz) / (np.linalg.norm(v1_xz) * np.linalg.norm(v2_xz))
        if cos_theta < 0:
            end_idx = min(i + 1 + k, n)
            valid_points = end_idx - (i + 1)
            if valid_points <= 0: break

            vx_sum, vy_sum, vz_sum = 0, 0, 0
            for j in range(i + 1, end_idx):
                dt = trajectory_data[j]['t'] - trajectory_data[j-1]['t']
                if dt <= 0: continue
                vx_sum += (trajectory_data[j]['x'] - trajectory_data[j-1]['x']) / dt
                vy_sum += (trajectory_data[j]['y'] - trajectory_data[j-1]['y']) / dt
                vz_sum += (trajectory_data[j]['z'] - trajectory_data[j-1]['z']) / dt

            vx_avg, vy_avg, vz_avg = vx_sum/valid_points, vy_sum/valid_points, vz_sum/valid_points

            # 轉換為物理參數
            speed_ms = np.sqrt(vx_avg**2 + vy_avg**2 + vz_avg**2)
            exit_velocity_mph = speed_ms / 0.44704
            horizontal_speed = np.sqrt(vx_avg**2 + vz_avg**2)
            launch_angle_deg = np.degrees(np.arctan2(vy_avg, horizontal_speed))
            spray_angle_deg = np.degrees(np.arctan2(vx_avg, -vz_avg)) # 假設 Godot 的外野是 -Z

            return {
                "is_hit": True,
                "hit_index": i,
                "exit_velocity": exit_velocity_mph,
                "launch_angle": launch_angle_deg,
                "spray_angle": spray_angle_deg
            }
    return {"is_hit": False}

# ==========================================
# 工具函式：共用的資料處理與發送流程
# ==========================================
def process_and_send(current_trajectory, source_name="本機測試"):
    if not current_trajectory:
        print(f"⚠️ [{source_name}] 軌跡資料為空！")
        return

    # 1. 特徵萃取
    hit_features = extract_hit_features(current_trajectory)
    print(f"[sender] is_hit={hit_features['is_hit']} n_points={len(current_trajectory)}")

    # 2. 擷取進壘點 (尋找 Z 座標最接近 0 的點)
    pitch_point = min(current_trajectory, key=lambda p: abs(p['z']))

    # 3. 準備 Payload
    payload = {
        "pitch_x_m": round(pitch_point['x'], 3),
        "pitch_y_m": round(pitch_point['y'], 3),
        "call": "WAITING_UI",
        "exit_velocity_mph": 0.0,
        "launch_angle_deg": 0.0,
        "spray_angle_deg": 0.0,
        "spin_rate_rpm": 2200.0
    }

    # 4. 寫入結果
    if hit_features["is_hit"]:
        payload["exit_velocity_mph"] = round(hit_features["exit_velocity"], 2)
        payload["launch_angle_deg"] = round(hit_features["launch_angle"], 2)
        payload["spray_angle_deg"] = round(hit_features["spray_angle"], 2)
        print(f"🎯 [{source_name}] 找到擊球點！初速: {payload['exit_velocity_mph']} mph")
    else:
        print(f"💨 [{source_name}] 未偵測到擊球折點 (揮空或純投球)。")

    # 5. 發送 UDP 封包給 Godot
    json_bytes = json.dumps(payload).encode('utf-8')
    send_sock.sendto(json_bytes, (GODOT_IP, GODOT_PORT))
    print(f"🚀 [已發送至 Godot] {payload}\n")

def load_trajectory_from_json(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f).get("trajectory", [])
    except Exception as e:
        print(f"❌ 讀取失敗: {e}")
        return []

# ==========================================
# 模式 4：網路即時監聽模式 (Server Mode)
# ==========================================
def listen_for_live_data():
    print(f"\n📡 [伺服器模式啟動] 正在監聽 {RECEIVE_IP}:{RECEIVE_PORT} → Godot {GODOT_IP}:{GODOT_PORT} (Ctrl+C 結束)")
    while True:
        try:
            data, addr = recv_sock.recvfrom(65535)
            print(f"\n📥 收到來自 {addr} 的 UDP 封包")
            raw_data = json.loads(data.decode('utf-8'))
            trajectory = raw_data.get("trajectory", [])
            process_and_send(trajectory, source_name=f"網路來源 {addr[0]}")
        except KeyboardInterrupt:
            print("\n🛑 結束監聽。")
            break
        except Exception as e:
            print(f"❌ 封包解析失敗: {e}")

# ==========================================
# 主程式 UI
# ==========================================
def interactive_main():
    print("=====================================================")
    print(" ⚾ 棒球軌跡特徵萃取發送器 (中介軟體版)")
    print("    (ball_tracker_sim bridge)")
    print("=====================================================")
    print(" [1] 傳送內建「模擬擊球」")
    print(" [0] 傳送內建「模擬揮空」")
    print(" [4] 進入「網路伺服器監聽模式」(接收外部即時數據)")
    print(" [輸入檔名] 解析外部 JSON (如 data.json)")
    print(" [q] 離開程式")
    print("=====================================================\n")

    mock_hit = [
        {"t": 0.0, "x": 0.0, "y": 1.5, "z": -18.44},
        {"t": 0.2, "x": 0.04, "y": 1.2, "z": -9.0},
        {"t": 0.4, "x": 0.08, "y": 0.82, "z": 0.0},
        {"t": 0.433, "x": -1.2, "y": 1.5, "z": -3.5},
        {"t": 0.466, "x": -2.4, "y": 2.2, "z": -7.0},
        {"t": 0.500, "x": -3.6, "y": 2.9, "z": -10.5},
    ]

    mock_miss = [
        {"t": 0.0, "x": 0.0, "y": 1.5, "z": -18.44},
        {"t": 0.2, "x": 0.04, "y": 1.2, "z": -9.0},
        {"t": 0.4, "x": 0.08, "y": 0.82, "z": 0.0},
        {"t": 0.433, "x": 0.09, "y": 0.75, "z": 1.5},
        {"t": 0.466, "x": 0.1, "y": 0.70, "z": 3.0},
    ]

    while True:
        user_input = input("👉 請輸入指令: ").strip()

        if user_input.lower() == 'q':
            break

        if user_input == '1':
            process_and_send(mock_hit, "內建模擬擊球")
        elif user_input == '0':
            process_and_send(mock_miss, "內建模擬揮空")
        elif user_input == '4':
            listen_for_live_data()
            break
        else:
            if not user_input.endswith('.json'):
                user_input += '.json'
            if os.path.exists(user_input):
                traj = load_trajectory_from_json(user_input)
                process_and_send(traj, f"檔案 {user_input}")
            else:
                print(f"⚠️ 找不到檔案 {user_input}")


def setup_sockets(listen_port, godot_host, godot_port):
    global send_sock, recv_sock, GODOT_IP, GODOT_PORT, RECEIVE_PORT
    GODOT_IP = godot_host
    GODOT_PORT = godot_port
    RECEIVE_PORT = listen_port
    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.bind((RECEIVE_IP, RECEIVE_PORT))


def main():
    parser = argparse.ArgumentParser(description="ball_tracker_sim sender bridge")
    parser.add_argument("--daemon", action="store_true",
                        help="跳過互動式選單，直接進入監聽模式")
    parser.add_argument("--listen-port", type=int, default=8888,
                        help="接收 ball_tracker bridge 的 UDP port (預設 8888)")
    parser.add_argument("--godot-host", default="127.0.0.1",
                        help="Godot 端 host (預設 127.0.0.1)")
    parser.add_argument("--godot-port", type=int, default=9999,
                        help="Godot 端 UDP port (預設 9999)")
    args = parser.parse_args()

    setup_sockets(args.listen_port, args.godot_host, args.godot_port)

    if args.daemon:
        listen_for_live_data()
    else:
        interactive_main()


if __name__ == "__main__":
    main()
