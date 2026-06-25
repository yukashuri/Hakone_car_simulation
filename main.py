from data_io.csv_manager import load_participants_from_csv
from logic.allocator import generate_full_plan
from logic.milp_allocator import generate_full_plan_milp
from logic.car_pool import section_label

USE_MILP = True  # True: PuLPによる最適化, False: 既存の貪欲ヒューリスティック

def main():
    print("=== 箱根駅伝シミュレーションを開始します ===")
    
    # 1. データの読み込み
    csv_path = "hakone_simulation_data_2025.csv"
    try:
        participants = load_participants_from_csv(csv_path)
    except FileNotFoundError:
        print(f"❌ エラー: {csv_path} が見つかりません。")
        return

    # 2. 配車アルゴリズムの実行
    print("\n--- 配車計画の計算を開始します ---")
    if USE_MILP:
        plan = generate_full_plan_milp(participants)
    else:
        plan = generate_full_plan(participants)
    
    # 3. 結果の可視化（ここを追加！）
    print("\n\n==================================================")
    print(" 🚗 暫定の配車・ランナー割り当て詳細リスト 🚗")
    print("==================================================")
    
    for section in plan:
        print(f"\n【 {section_label(section.section_id)} 】")
        
        # ランナーの名前を取得して表示
        runners = [participants[pid].name for pid in section.runner_ids if pid in participants]
        print(f"🏃 ランナー ({len(runners)}名): {', '.join(runners)}")
        
        # 車ごとの割り当てを表示
        for car in section.cars:
            driver_name = participants[car.driver_id].name if car.driver_id in participants else "【不在/エラー】"
            passengers = [participants[pid].name for pid in car.passenger_ids if pid in participants]
            
            car_type = "大型" if car.car_type == "large" else "普通"
            
            # 💡 車自身が知っている「山行きフラグ」を見てアイコンを付ける
            is_mt = " ⛰️[山行き部隊]" if car.is_mountain_goer else ""
            
            print(f"  🚘 車{car.car_id} ({car_type}){is_mt} - 乗車人数: 計{car.total_people}人")
            print(f"      👨‍✈️ 運転手: {driver_name}")
            print(f"      👥 同乗者: {', '.join(passengers) if passengers else 'なし'}")
    print("\n✅ 全区間の出力が完了しました！")
    print(f"\n✅ すべての処理が完了しました！ 'hakone_result.csv' を確認してください。")

if __name__ == "__main__":
    main()