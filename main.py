import argparse

from data_io.csv_manager import load_participants_from_csv
from data_io.sheets_manager import load_participants_from_sheet, save_plan_to_sheet
from logic.allocator import generate_full_plan
from logic.milp_allocator import generate_full_plan_milp
from logic.car_pool import section_label
from validator import validate_participants, validate_transitions, count_car_changes, compute_runner_satisfaction

USE_MILP = True  # True: PuLPによる最適化, False: 既存の貪欲ヒューリスティック

def main():
    parser = argparse.ArgumentParser(description="箱根駅伝配車シミュレーション")
    parser.add_argument("--url", help="入力データのGoogle SpreadsheetのURL")
    parser.add_argument("--credentials", default="credentials.json", help="サービスアカウントのJSONキーのパス")
    args = parser.parse_args()

    print("=== 箱根駅伝シミュレーションを開始します ===")

    # 1. データの読み込み
    if args.url:
        print(f"Google Spreadsheetからデータを読み込み中...")
        try:
            participants = load_participants_from_sheet(args.url, args.credentials)
        except Exception as e:
            print(f"❌ エラー: スプレッドシートの読み込みに失敗しました。\n  {e}")
            return
    else:
        csv_path = "hakone_simulation_data_2025.csv"
        try:
            participants = load_participants_from_csv(csv_path)
        except FileNotFoundError:
            print(f"❌ エラー: {csv_path} が見つかりません。")
            return

    # 1.5 入力データの検証
    input_warnings = validate_participants(participants)
    if input_warnings:
        print("\n⚠️ 入力データに以下の問題があります:")
        for w in input_warnings:
            print(f"  • {w}")

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
            is_mt = " ⛰️[山行き部隊]" if car.is_mountain_goer else ""
            is_hotel = " [ホテル組]" if car.group == "hotel" else ""
            is_adv = " 🚀[先行]" if car.is_advance else ""

            print(f"  🚘 車{car.car_id} ({car_type}){is_mt}{is_hotel}{is_adv} - 乗車人数: 計{car.total_people}人")
            print(f"      👨‍✈️ 運転手: {driver_name}")
            print(f"      👥 同乗者: {', '.join(passengers) if passengers else 'なし'}")
    print("\n✅ 全区間の出力が完了しました！")

    # 区間をまたいだ車両引き継ぎチェック
    transition_errors = validate_transitions(plan, participants)
    if transition_errors:
        print("\n⚠️ 車両引き継ぎに問題があります:")
        for err in transition_errors:
            print(f"  • {err}")
    changes = count_car_changes(plan)
    print(f"ℹ️ 区間をまたいで車を乗り換えた回数: {changes}回")

    # ランナー希望充足率
    n_wanted, n_satisfied, total_wanted, total_ran = compute_runner_satisfaction(plan, participants)
    print(f"\n📊 ランナー希望充足率: {n_satisfied}/{n_wanted}人が希望区間数を達成")
    print(f"   走行総区間数: {total_ran}/{total_wanted}区間")

    if args.url:
        print("\n--- 結果をGoogle Spreadsheetに書き出し中 ---")
        try:
            result_url = save_plan_to_sheet(plan, participants, args.url, args.credentials)
            print(f"\n✅ すべての処理が完了しました！ 結果のスプレッドシート:\n  {result_url}")
        except Exception as e:
            print(f"❌ エラー: スプレッドシートへの書き出しに失敗しました。\n  {e}")
    else:
        print(f"\n✅ すべての処理が完了しました！ 'hakone_result.csv' を確認してください。")

if __name__ == "__main__":
    main()