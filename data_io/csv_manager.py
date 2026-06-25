import csv
import os
import sys
from typing import Dict, List

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import Participant, SectionState

def load_participants_from_csv(file_path: str) -> Dict[str, Participant]:
    participants = {}
    with open(file_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for index, row in enumerate(reader):
            p_id = f"p{index + 1}"
            
            preferred = []
            for section in range(1, 11):
                val = str(row.get(str(section), "")).strip().replace('.0', '')
                preferred.append(val == '1')
                
            val_drive = str(row.get('運転', '0')).strip().replace('.0', '')
            val_large = str(row.get('大', '0')).strip().replace('.0', '')
            val_mountain = str(row.get('山', '0')).strip().replace('.0', '')
            
            is_large = (val_large == '1')
            is_mountain = (val_mountain == '1')
            is_drive = (val_drive == '1') or is_large or is_mountain
                
            leaves_str = str(row.get('離脱区間', '')).strip().replace('.0', '')
            leaves_after_section = int(leaves_str) if leaves_str else None

            participant = Participant(
                id=p_id,
                name=str(row.get('名前', '')),
                preferred_sections=preferred,
                can_drive=is_drive,
                can_drive_large=is_large,
                can_drive_mountain=is_mountain,
                staying_overnight=(str(row.get('宿泊', '0')).strip().replace('.0', '') == '1'),
                grade=int(str(row.get('学年', '1')).strip().replace('.0', '') or '1'),
                remaining_sections=int(str(row.get('希望区間数', '0')).strip().replace('.0', '') or '0'), # ここ
                leaves_after_section=leaves_after_section,
            )
            participants[p_id] = participant
    return participants

def save_plan_to_csv(plan: List[SectionState], participants: Dict[str, Participant], output_path: str):
    """
    計算結果をCSVファイルとして保存します。
    """
    with open(output_path, mode='w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        # ヘッダー
        writer.writerow(['区間', 'ランナー', '車ID', '車種', '運転手', '同乗者'])
        
        for section in plan:
            runners = ", ".join([participants[pid].name for pid in section.runner_ids])
            for car in section.cars:
                driver_name = participants[car.driver_id].name if car.driver_id in participants else "エラー"
                passengers = ", ".join([participants[pid].name for pid in car.passenger_ids])
                car_type = "大型" if car.car_id in ["1", "2", "3", "4"] else "普通"
                
                writer.writerow([
                    f"{section.section_id}区",
                    runners,
                    car.car_id,
                    car_type,
                    driver_name,
                    passengers
                ])
    print(f"✅ CSVファイル '{output_path}' を作成しました。")