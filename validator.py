from typing import List, Dict
from models import Participant, SectionState
from logic.car_pool import MOUNTAIN_SECTIONS, LARGE_CAPACITY, NORMAL_CAPACITY

def validate_section(state: SectionState, participants: Dict[str, Participant]) -> List[str]:
    errors = []
    all_runners = set(state.runner_ids)
    all_drivers = set()

    for car in state.cars:
        all_drivers.add(car.driver_id)
        driver = participants.get(car.driver_id)

        if not driver:
            errors.append(f"車{car.car_id}のドライバーが不在です")
            continue

        if car.car_type == "large":
            if car.total_people > LARGE_CAPACITY: errors.append(f"車{car.car_id}(大型)定員オーバー")
            if not driver.can_drive_large: errors.append(f"車{car.car_id}ですが{driver.name}は大型免許なし")
        else:
            if car.total_people > NORMAL_CAPACITY: errors.append(f"車{car.car_id}(普通)定員オーバー")

        if not driver.can_drive: errors.append(f"車{car.car_id}の{driver.name}は免許なし")

        # 💡【修正】フラグがついている車だけ山道免許をチェック
        if state.section_id in MOUNTAIN_SECTIONS and car.is_mountain_goer:
            if not driver.can_drive_mountain:
                errors.append(f"{state.section_id}区の山行き車両({car.car_id})ですが、{driver.name}は山道免許なし")

        if len(car.passenger_ids) == 0: errors.append(f"車{car.car_id}に同乗者なし")
        else:
            if not any(participants[pid].grade >= 2 for pid in car.passenger_ids if pid in participants):
                errors.append(f"車{car.car_id}に2年生以上なし")

    return errors

def _car_of(state: SectionState, person_id: str) -> str:
    """ある区間でperson_idがどの車にいるか(運転/同乗どちらでも)を返す。乗っていなければ空文字。"""
    for car in state.cars:
        if person_id == car.driver_id or person_id in car.passenger_ids:
            return car.car_id
    return ""

def validate_transitions(sections: List[SectionState], participants: Dict[str, Participant]) -> List[str]:
    """区間をまたいだ配車の継続性をチェックする。

    - 一般の区間間: ランナーでない限り、同じ車に乗り続けているかを確認する（情報用、エラーにはしない）。
    - 8区→9区（山行き開始）: 山行き車両(is_mountain_goer)のドライバーが
      9区→10区で変わっていないかをチェックする（山組は1往復のみという前提のため、ここはエラーとする）。
    """
    errors = []
    by_section = {s.section_id: s for s in sections}

    for s_id in range(9, 11):
        prev = by_section.get(s_id - 1)
        cur = by_section.get(s_id)
        if not prev or not cur:
            continue
        prev_mt_drivers = {c.car_id: c.driver_id for c in prev.cars if c.is_mountain_goer}
        cur_mt_drivers = {c.car_id: c.driver_id for c in cur.cars if c.is_mountain_goer}
        for car_id, driver_id in cur_mt_drivers.items():
            if car_id in prev_mt_drivers and prev_mt_drivers[car_id] != driver_id:
                errors.append(
                    f"{s_id - 1}区→{s_id}区: 山行き車両{car_id}のドライバーが"
                    f"{participants.get(prev_mt_drivers[car_id], 'unknown').name}から"
                    f"{participants.get(driver_id, 'unknown').name}に変わっています"
                )

    return errors

def count_car_changes(sections: List[SectionState]) -> int:
    """区間をまたいでいて車を乗り換えた(ランナーでもなく同じ車でもない)人数の合計。連続性の指標として使う。"""
    changes = 0
    sections_sorted = sorted(sections, key=lambda s: s.section_id)
    for prev, cur in zip(sections_sorted, sections_sorted[1:]):
        all_people = set()
        for car in prev.cars:
            all_people.add(car.driver_id)
            all_people.update(car.passenger_ids)
        for car in cur.cars:
            all_people.add(car.driver_id)
            all_people.update(car.passenger_ids)
        for pid in all_people:
            if pid in prev.runner_ids or pid in cur.runner_ids:
                continue
            prev_car = _car_of(prev, pid)
            cur_car = _car_of(cur, pid)
            if prev_car and cur_car and prev_car != cur_car:
                changes += 1
    return changes