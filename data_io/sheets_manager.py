import re
import os
import sys
from typing import Dict, List

import gspread

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import Participant, SectionState
from logic.car_pool import section_label

CREDENTIALS_DEFAULT = "credentials.json"


def _client(credentials_path: str) -> gspread.Client:
    return gspread.service_account(filename=credentials_path)


def _sheet_id_from_url(url: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not match:
        raise ValueError(f"URLからスプレッドシートIDを取得できませんでした: {url}")
    return match.group(1)


def _flag(val) -> bool:
    return str(val).strip().replace(".0", "") == "1"


def _int_or(val, default: int) -> int:
    s = str(val).strip().replace(".0", "")
    return int(s) if s else default


def load_participants_from_sheet(url: str, credentials_path: str = CREDENTIALS_DEFAULT) -> Dict[str, Participant]:
    gc = _client(credentials_path)
    sheet = gc.open_by_key(_sheet_id_from_url(url)).sheet1
    records = sheet.get_all_records()

    participants = {}
    for index, row in enumerate(records):
        p_id = f"p{index + 1}"

        preferred = [_flag(row.get(str(s), 0)) for s in range(1, 11)]

        is_large = _flag(row.get("大", 0))
        is_mountain = _flag(row.get("山", 0))
        is_drive = _flag(row.get("運転", 0)) or is_large or is_mountain

        leaves_str = str(row.get("離脱区間", "")).strip().replace(".0", "")
        leaves_after_section = int(leaves_str) if leaves_str else None

        participants[p_id] = Participant(
            id=p_id,
            name=str(row.get("名前", "")),
            preferred_sections=preferred,
            can_drive=is_drive,
            can_drive_large=is_large,
            can_drive_mountain=is_mountain,
            staying_overnight=_flag(row.get("宿泊", 0)),
            grade=_int_or(row.get("学年", 1), 1),
            remaining_sections=_int_or(row.get("希望区間数", 0), 0),
            leaves_after_section=leaves_after_section,
        )
    return participants


def save_plan_to_sheet(
    plan: List[SectionState],
    participants: Dict[str, Participant],
    credentials_path: str = CREDENTIALS_DEFAULT,
) -> str:
    gc = _client(credentials_path)
    spreadsheet = gc.create("hakone_result")
    sheet = spreadsheet.sheet1
    sheet.update_title("配車結果")

    rows = [["区間", "ランナー", "車ID", "車種", "山行き", "運転手", "同乗者"]]
    for section in plan:
        runners = ", ".join(participants[pid].name for pid in section.runner_ids if pid in participants)
        for car in section.cars:
            driver_name = participants[car.driver_id].name if car.driver_id in participants else "エラー"
            passengers = ", ".join(participants[pid].name for pid in car.passenger_ids if pid in participants)
            car_type = "大型" if car.car_type == "large" else "普通"
            is_mt = "★山行き" if car.is_mountain_goer else ""
            rows.append([section_label(section.section_id), runners, car.car_id, car_type, is_mt, driver_name, passengers])

    sheet.update(rows, "A1")

    # サービスアカウントのDriveに作られるので、リンクを知っている全員が閲覧できるよう共有
    spreadsheet.share(None, perm_type="anyone", role="reader")

    return spreadsheet.url
