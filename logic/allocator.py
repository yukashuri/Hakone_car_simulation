from typing import List, Dict, Optional
import sys
import os
import csv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import Participant, CarState, SectionState
from validator import validate_section
from logic.car_pool import section_label

TOTAL_CARS = 7
MOUNTAIN_SECTIONS = [9, 10]
LARGE_CARS = ["1", "2", "3", "4"]

runner_quotas = {}

def allocate_single_section(
    section_id: int, 
    participants: Dict[str, Participant], 
    previous_section: Optional[SectionState] = None
) -> SectionState:
    
    available_pids = list(participants.keys())
    assigned_pids = set()
    previous_runners = set(previous_section.runner_ids) if previous_section else set()

    # --------------------------------------------------
    # 0. 山行き車両の「台数」を自動計算
    # --------------------------------------------------
    dynamic_mountain_cars = []
    if section_id in MOUNTAIN_SECTIONS:
        # 山を走る予定の人を数える
        mt_hopefuls = [
            pid for pid in available_pids 
            if (participants[pid].preferred_sections[8] or participants[pid].preferred_sections[9]) 
            and runner_quotas.get(pid, 0) > 0
        ]
        # その人数を運ぶために、車1号車から順に「山行き」に指定していく
        needed_capacity = max(len(mt_hopefuls), 3) # 最低3人は走ると見積もる
        current_capacity = 0
        
        for i in range(TOTAL_CARS):
            car_id = str(i + 1)
            dynamic_mountain_cars.append(car_id)
            cap = 8 if car_id in LARGE_CARS else 5
            current_capacity += (cap - 1) # ドライバー以外の乗車枠
            if current_capacity >= needed_capacity:
                break # 人数が収まったら指定終了！

    # 1. ドライバー選出
    large_drivers = []
    normal_drivers = []
    drivers_pool = [pid for pid in available_pids if participants[pid].can_drive and pid not in previous_runners]

    for car_idx in range(TOTAL_CARS):
        car_id = str(car_idx + 1)
        target_pid = None
        for pid in drivers_pool:
            p = participants[pid]
            need_large = car_id in LARGE_CARS
            need_mountain = (section_id in MOUNTAIN_SECTIONS and car_id in dynamic_mountain_cars)
            
            if need_large and not p.can_drive_large: continue
            if need_mountain and not p.can_drive_mountain: continue
            target_pid = pid
            break
            
        if target_pid:
            if car_id in LARGE_CARS: large_drivers.append(target_pid)
            else: normal_drivers.append(target_pid)
            drivers_pool.remove(target_pid)
            available_pids.remove(target_pid)
            assigned_pids.add(target_pid)
        else:
            dummy = drivers_pool.pop(0) if drivers_pool else "NO_DRIVER"
            if car_id in LARGE_CARS: large_drivers.append(dummy)
            else: normal_drivers.append(dummy)
            if dummy in available_pids: available_pids.remove(dummy)

    # 2. ランナー選出（温存戦略＆お助けモード）
    selected_runners = []
    candidates = [
        pid for pid in available_pids 
        if participants[pid].preferred_sections[section_id - 1] and runner_quotas.get(pid, 0) > 0
    ]
    candidates.sort(key=lambda pid: participants[pid].preferred_sections.count(True))

    for pid in candidates:
        selected_runners.append(pid)
        available_pids.remove(pid)
        assigned_pids.add(pid)
        runner_quotas[pid] -= 1

    if len(selected_runners) == 0:
        all_hopefuls = [pid for pid in available_pids if participants[pid].preferred_sections[section_id - 1]]
        if all_hopefuls:
            all_hopefuls.sort(key=lambda pid: participants[pid].remaining_sections, reverse=True)
            extra_pid = all_hopefuls[0]
            selected_runners.append(extra_pid)
            available_pids.remove(extra_pid)
            assigned_pids.add(extra_pid)
            runner_quotas[extra_pid] -= 1
            print(f"  ℹ️ {section_id}区: 走者不在のため {participants[extra_pid].name} さんを緊急招集")

    # 3. 山行き組の確保
    mountain_group = []
    if section_id in MOUNTAIN_SECTIONS:
        for pid in list(available_pids):
            p = participants[pid]
            if (p.preferred_sections[8] or p.preferred_sections[9]) and runner_quotas.get(pid, 0) > 0:
                mountain_group.append(pid)
                available_pids.remove(pid)

    # 4. 助手席係
    selected_assistants = []
    for pid in list(available_pids):
        if len(selected_assistants) >= TOTAL_CARS: break
        if participants[pid].grade >= 2:
            selected_assistants.append(pid)
            available_pids.remove(pid)

    # 5. 車の組み立て（フラグをつける）
    cars = []
    for i in range(TOTAL_CARS):
        c_id = str(i+1)
        d_id = large_drivers[i] if i < 4 else normal_drivers[i-4]
        passengers = []
        if i < len(selected_assistants): passengers.append(selected_assistants[i])
        
        is_mt_goer = (section_id in MOUNTAIN_SECTIONS and c_id in dynamic_mountain_cars)
        if is_mt_goer:
            while len(passengers) < (7 if c_id in LARGE_CARS else 4) and mountain_group:
                passengers.append(mountain_group.pop(0))
                
        # 💡 ここで is_mountain_goer を記憶させる
        car_type = "large" if c_id in LARGE_CARS else "normal"
        cars.append(CarState(car_id=c_id, driver_id=d_id, passenger_ids=passengers, is_mountain_goer=is_mt_goer, car_type=car_type))

    # 6. 余りを詰め込む
    remaining_people = mountain_group + available_pids
    car_index = 0
    for pid in remaining_people:
        capacity = 8 if str(car_index+1) in LARGE_CARS else 5
        while cars[car_index].total_people >= capacity:
            car_index = (car_index + 1) % TOTAL_CARS
            capacity = 8 if str(car_index+1) in LARGE_CARS else 5
        cars[car_index].passenger_ids.append(pid)

    return SectionState(section_id=section_id, runner_ids=selected_runners, cars=cars)

def save_plan_to_csv(plan: List[SectionState], participants: Dict[str, Participant], output_path: str):
    with open(output_path, mode='w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['区間', 'ランナー', '車ID', '車種', '山行き', '運転手', '同乗者'])
        for section in plan:
            runners = ", ".join([participants[pid].name for pid in section.runner_ids])
            for car in section.cars:
                driver_name = participants[car.driver_id].name if car.driver_id in participants else "エラー"
                passengers = ", ".join([participants[pid].name for pid in car.passenger_ids])
                car_type = "大型" if car.car_type == "large" else "普通"
                is_mt = "★山行き" if car.is_mountain_goer else ""
                writer.writerow([section_label(section.section_id), runners, car.car_id, car_type, is_mt, driver_name, passengers])
    print(f"\n✅ CSVファイル '{output_path}' を作成しました。")

def generate_full_plan(participants: Dict[str, Participant]) -> List[SectionState]:
    global runner_quotas
    runner_quotas = {pid: p.remaining_sections for pid, p in participants.items()}

    plan = []
    previous_section = None
    for section_id in range(1, 11):
        print(f"{section_id}区の割り当てを計算中...")
        current_section = allocate_single_section(section_id, participants, previous_section)
        errors = validate_section(current_section, participants)
        if errors:
            print(f"❌ {section_id}区でエラー: " + " / ".join(errors))
        else:
            print(f"✅ {section_id}区は問題なく割り当てられました！")
        plan.append(current_section)
        previous_section = current_section
    
    save_plan_to_csv(plan, participants, "hakone_result.csv")
    return plan