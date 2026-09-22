#!/usr/bin/env python3
"""ローカルのxlsxファイルを使ってMILPをテストするスクリプト。
使い方: python3 test_local.py [xlsxファイル名]
例:     python3 test_local.py "箱根企画_シュミレーション　田村用 のコピー.xlsx"
"""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import openpyxl
from models import Participant
from logic.milp_allocator import generate_full_plan_milp
from logic.car_pool import section_label
from validator import validate_participants, validate_transitions, count_car_changes, compute_runner_satisfaction


def _find_col(headers, keyword):
    return next((h for h in headers if h and keyword in h), "")


def _parse_sections(raw):
    result = [False] * 10
    for item in re.split(r'[,、・/\s　]+', str(raw)):
        item = item.strip().replace("区", "")
        if item.isdigit():
            idx = int(item) - 1
            if 0 <= idx < 10:
                result[idx] = True
    return result


def load_from_xlsx(path):
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        raise ValueError("データが0件です")

    headers = [str(h) if h is not None else "" for h in rows[0]]

    col_name     = _find_col(headers, "名前")
    col_grade    = _find_col(headers, "学年")
    col_sections = _find_col(headers, "走りたい区間")
    col_priority = _find_col(headers, "特に走りたい")
    col_count    = _find_col(headers, "何区間")
    col_drive    = _find_col(headers, "普通自動車")
    col_large    = _find_col(headers, "大型")
    col_mountain = _find_col(headers, "山道")
    col_stay     = _find_col(headers, "宿泊")
    col_leave    = _find_col(headers, "帰りますか")

    participants = {}
    p_index = 0
    for row_vals in rows[1:]:
        if not any(v for v in row_vals if v is not None):
            continue
        row = dict(zip(headers, row_vals))

        def get(col):
            return str(row.get(col, "") or "").strip()

        def yes(col):
            return get(col).startswith("はい")

        # 名前が空の行はスキップ
        if not get(col_name):
            continue

        preferred = _parse_sections(get(col_sections))
        priority  = _parse_sections(get(col_priority)) if col_priority else [False] * 10

        grade_match = re.match(r"(\d+)", get(col_grade))
        grade = int(grade_match.group(1)) if grade_match else 1

        count_str = get(col_count).replace(".0", "")
        remaining = int(count_str) if count_str.isdigit() else preferred.count(True)

        leave_str = get(col_leave).replace("区", "").replace(".0", "")
        leaves_after = int(leave_str) if leave_str.isdigit() else None

        is_large    = yes(col_large)
        is_mountain = yes(col_mountain)
        is_drive    = yes(col_drive) or is_large or is_mountain

        p_index += 1
        p_id = f"p{p_index}"
        participants[p_id] = Participant(
            id=p_id,
            name=get(col_name),
            preferred_sections=preferred,
            can_drive=is_drive,
            can_drive_large=is_large,
            can_drive_mountain=is_mountain,
            staying_overnight=yes(col_stay),
            grade=grade,
            remaining_sections=remaining,
            leaves_after_section=leaves_after,
            priority_sections=priority,
        )

    return participants


def main():
    xlsx = sys.argv[1] if len(sys.argv) > 1 else "箱根企画_シュミレーション　田村用 のコピー.xlsx"
    print(f"=== テスト実行: {xlsx} ===\n")

    participants = load_from_xlsx(xlsx)
    print(f"参加者 {len(participants)} 名を読み込みました\n")

    # 入力検証
    warnings = validate_participants(participants)
    if warnings:
        print("⚠️  入力警告:")
        for w in warnings:
            print(f"   {w}")
        print()

    # MILP実行
    plan = generate_full_plan_milp(participants)

    # 結果表示
    print("\n==================================================")
    print(" 🚗 配車・ランナー割り当て詳細")
    print("==================================================")
    for section in plan:
        print(f"\n【 {section_label(section.section_id)} 】")
        runners = [participants[pid].name for pid in section.runner_ids if pid in participants]
        print(f"🏃 ランナー ({len(runners)}名): {', '.join(runners)}")
        for car in section.cars:
            driver_name = participants[car.driver_id].name if car.driver_id in participants else "【エラー】"
            passengers = [participants[pid].name for pid in car.passenger_ids if pid in participants]
            car_type = "大型" if car.car_type == "large" else "普通"
            tags = ""
            if car.is_mountain_goer: tags += " ⛰️[山]"
            if car.group == "hotel":  tags += " 🏨[ホテル]"
            if car.is_advance:        tags += " 🚀[先行]"
            print(f"  🚘 {car.car_id}({car_type}){tags} 計{car.total_people}人  運転:{driver_name}  同乗:{', '.join(passengers) or 'なし'}")

    # バリデーション
    print("\n--- バリデーション ---")
    errors = validate_transitions(plan, participants)
    if errors:
        for e in errors:
            print(f"  ❌ {e}")
    else:
        print("  ✅ 遷移エラーなし")

    changes = count_car_changes(plan)
    print(f"  乗り換え回数: {changes}回")

    n_wanted, n_satisfied, total_wanted, total_ran = compute_runner_satisfaction(plan, participants)
    print(f"  希望充足: {n_satisfied}/{n_wanted}人  走行区間: {total_ran}/{total_wanted}")


if __name__ == "__main__":
    main()
