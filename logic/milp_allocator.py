"""PuLP(MILP)による配車最適化。

Block A (1〜8区): レンタル車の台数・車種、各区間のドライバー・同乗者・ランナーを
                  まとめて1つのMILPで最適化する。
Block B (9〜10区 + 分岐): Block Aで決まった車の構成を引き継ぎ、8区終了時点での
                  「ホテル組」「山組」への分割と、9・10区の割り当てを最適化する。
                  山組は1往復（行き→待機→帰り）のみという前提で、9区→10区で
                  山行き車両のドライバーは変えない。

目的関数は優先度順に: ①レンタカーのコスト最小化 ②区間をまたいだ配車の連続性
③ランナー希望の充足、という重み付けにしている。
"""

import os
import sys
from typing import Dict, List, Tuple

import pulp

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import Participant, CarState, SectionState
from validator import validate_section
from logic.car_pool import ALL_CAR_IDS, LARGE_CAR_IDS, NORMAL_CAR_IDS, CAR_TYPE, CAR_CAPACITY, CAR_COST, section_label
from logic.allocator import save_plan_to_csv

W_FLEET = 5000            # 台数コストを大幅に重く → 5台と8台の差(25000)が他の項目差(〜5000)を圧倒
W_CONTINUITY = 5
W_RUNNER_PREF = 50
W_MTN_RUNNER_DRIVE = 500  # 山行きランナーが7・8区でドライバーになることへのペナルティ
W_ADVANCE_SPREAD = 30     # 次区間ランナーが複数の先行車に分散することへのペナルティ
W_PREV_RUN_DRIVE = 200    # 前区間走者が次区間の運転手になることへのペナルティ
W_PARK = 15000            # レンタルした車が一部区間で未使用になることへのペナルティ（W_FLEET*最大車コスト=10000より大きくすること）
W_NO_GRADE2 = 100         # 2年生以上の同乗者がいない車へのペナルティ
W_NO_PASSENGER = 50       # 同乗者なし（ドライバーのみ）の車へのペナルティ

BLOCK_A_SECTIONS = list(range(1, 9))
BLOCK_B_SECTIONS = [9, 10]
RETURN_TRIP_SECTION_ID = 11  # 全員がホテル/箱根から帰る行程（CSV上は「帰路」と表示）


def _solve(prob: pulp.LpProblem, time_limit: int = 240) -> str:
    prob.solve(pulp.PULP_CBC_CMD(msg=1, timeLimit=time_limit))
    return pulp.LpStatus[prob.status]


def _val(var) -> int:
    v = var.value()
    return 1 if v is not None and v > 0.5 else 0


def _present(participants: Dict[str, Participant], p: str, s: int) -> bool:
    """その人がその区間の時点でまだ会場に残っているか（日帰りで先に離脱していないか）。"""
    leaves = participants[p].leaves_after_section
    return leaves is None or s <= leaves


def _build_block_a(participants: Dict[str, Participant], car_ids=None):
    if car_ids is None:
        car_ids = ALL_CAR_IDS
    large_ids = [k for k in car_ids if CAR_TYPE[k] == "large"]
    normal_ids = [k for k in car_ids if CAR_TYPE[k] == "normal"]

    pids = list(participants.keys())
    sections = BLOCK_A_SECTIONS

    prob = pulp.LpProblem("hakone_block_a", pulp.LpMinimize)

    rent = {k: pulp.LpVariable(f"rent_{k}", cat="Binary") for k in car_ids}

    # 対称性の除去: 同種の車はインデックス順に借りる（L2を借りるならL1も借りる）
    # これにより等価な解の探索が大幅に減り、求解速度が向上する
    for i in range(len(large_ids) - 1):
        prob += rent[large_ids[i]] >= rent[large_ids[i + 1]]
    for i in range(len(normal_ids) - 1):
        prob += rent[normal_ids[i]] >= rent[normal_ids[i + 1]]

    runs = {}
    for p in pids:
        for s in sections:
            if participants[p].preferred_sections[s - 1] and _present(participants, p, s):
                runs[(p, s)] = pulp.LpVariable(f"runs_{p}_{s}", cat="Binary")

    drive = {}
    for p in pids:
        part = participants[p]
        if not part.can_drive:
            continue
        for k in car_ids:
            if CAR_TYPE[k] == "large" and not part.can_drive_large:
                continue
            for s in sections:
                if _present(participants, p, s):
                    drive[(p, k, s)] = pulp.LpVariable(f"drive_{p}_{k}_{s}", cat="Binary")

    ride = {
        (p, k, s): pulp.LpVariable(f"ride_{p}_{k}_{s}", cat="Binary")
        for p in pids
        for k in car_ids
        for s in sections
        if _present(participants, p, s)
    }

    usedcar = {
        (k, s): pulp.LpVariable(f"used_{k}_{s}", cat="Binary")
        for k in car_ids
        for s in sections
    }

    no_grade2_vars = []
    no_passenger_vars = []
    for s in sections:
        for k in car_ids:
            drivers_ks = [drive[(p, k, s)] for p in pids if (p, k, s) in drive]
            prob += pulp.lpSum(drivers_ks) == usedcar[(k, s)]
            prob += usedcar[(k, s)] <= rent[k]

            riders_ks = [ride[(p, k, s)] for p in pids if (p, k, s) in ride]
            prob += pulp.lpSum(riders_ks) <= (CAR_CAPACITY[k] - 1) * usedcar[(k, s)]
            # 同乗者最低1名はソフト制約に変更（ランナーが多い区間で車が消えるのを防ぐため）
            if s != 8:
                v_pass = pulp.LpVariable(f"no_pass_{k}_{s}", cat="Binary")
                prob += v_pass >= usedcar[(k, s)] - pulp.lpSum(riders_ks)
                no_passenger_vars.append(v_pass)

            grade2_riders = [ride[(p, k, s)] for p in pids if participants[p].grade >= 2 and (p, k, s) in ride]
            if s != 8:
                # ソフト制約: 2年生以上の同乗者がいることを推奨するが必須ではない
                v_g2 = pulp.LpVariable(f"no_g2_{k}_{s}", cat="Binary")
                prob += v_g2 >= usedcar[(k, s)] - pulp.lpSum(grade2_riders)
                no_grade2_vars.append(v_g2)

        for p in pids:
            if not _present(participants, p, s):
                continue  # 日帰りで既に離脱した人はこの区間の役割を持たない
            terms = []
            if (p, s) in runs:
                terms.append(runs[(p, s)])
            terms += [drive[(p, k, s)] for k in car_ids if (p, k, s) in drive]
            terms += [ride[(p, k, s)] for k in car_ids if (p, k, s) in ride]
            prob += pulp.lpSum(terms) == 1

        runners_s = [runs[(p, s)] for p in pids if (p, s) in runs]
        prob += pulp.lpSum(runners_s) >= 1  # 各区間に最低1人は走者を確保

    # レンタルした車を全区間で使うよう誘導（ソフト制約）
    # rent[k]=1 なのに usedcar[(k,s)]=0 になる（"一時駐車"）を抑制する
    park_vars = []
    for k in car_ids:
        for s in sections:
            v_park = pulp.LpVariable(f"park_{k}_{s}", cat="Binary")
            prob += v_park >= rent[k] - usedcar[(k, s)]
            park_vars.append(v_park)

    for p in pids:
        run_vars = [v for (pp, s), v in runs.items() if pp == p]
        if run_vars:
            prob += pulp.lpSum(run_vars) <= participants[p].remaining_sections

    # 帰りは最後まで残る人を同時に車で運ぶ必要があるため、レンタル車の総定員は
    # 日帰りで先に離脱する人を除いた人数以上にする
    return_trip_count = sum(1 for p in pids if participants[p].leaves_after_section is None)
    prob += pulp.lpSum(CAR_CAPACITY[k] * rent[k] for k in car_ids) >= return_trip_count

    occ = {}
    for p in pids:
        for k in car_ids:
            for s in sections:
                terms = []
                if (p, k, s) in ride:
                    terms.append(ride[(p, k, s)])
                if (p, k, s) in drive:
                    terms.append(drive[(p, k, s)])
                occ[(p, k, s)] = pulp.lpSum(terms)

    # 次区間ランナーをなるべく1台にまとめる（ソフト制約）
    # adv[(k,s)]=1 ⟺ 区間sで車kに次区間ランナーが乗っている。この台数を最小化する。
    adv_car_vars = []
    for i in range(len(sections) - 1):
        s, s_next = sections[i], sections[i + 1]
        next_runner_pids = [p for p in pids if (p, s_next) in runs]
        if not next_runner_pids:
            continue
        for k in car_ids:
            adv = pulp.LpVariable(f"adv_{k}_{s}", cat="Binary")
            for p in next_runner_pids:
                prob += adv >= runs[(p, s_next)] + occ[(p, k, s)] - 1
            adv_car_vars.append(adv)

    mountain_hopefuls = [
        p for p in pids
        if participants[p].preferred_sections[8] or participants[p].preferred_sections[9]
    ]
    # 7〜8区の山行き組 = 山行きランナー + 山道免許持ち（ドライバー候補）
    mountain_group = set(mountain_hopefuls) | {p for p in pids if participants[p].can_drive_mountain}
    non_mountain_strict = [p for p in pids if p not in mountain_group]

    # 6区: 山行き希望者の車に5区ランナーを同乗させない
    for k in car_ids:
        for p_mtn in mountain_hopefuls:
            for p_run in pids:
                if p_mtn == p_run:
                    continue
                if (p_run, 5) not in runs:
                    continue
                prob += occ[(p_mtn, k, 6)] + occ[(p_run, k, 6)] + runs[(p_run, 5)] <= 2

    # 7〜8区: 山行き組が乗る車に非山行き者を同乗客として乗せない
    for s in [7, 8]:
        for k in car_ids:
            for p_mtn in mountain_group:
                for p_other in non_mountain_strict:
                    if (p_other, k, s) in ride:
                        prob += ride[(p_other, k, s)] + occ[(p_mtn, k, s)] <= 1

    # 7区: 8区を走る人は山行き車の運転手になれない（山行き車に乗ると8区スタートに戻れない）
    # mountain_group と 山道免許持ち（can_drive_mountain）の両方を山行き車の識別子として使う
    mountain_drivers_set = {p for p in pids if participants[p].can_drive_mountain}
    mountain_car_signal = mountain_group | mountain_drivers_set
    for k in car_ids:
        for p in non_mountain_strict:
            if (p, k, 7) not in drive:
                continue
            if (p, 8) not in runs:
                continue
            for p_sig in mountain_car_signal:
                if p_sig == p:
                    continue
                prob += drive[(p, k, 7)] + occ[(p_sig, k, 7)] + runs[(p, 8)] <= 2

    # 7区→8区: 山行き車のメンバーを完全固定
    # 山グループは7区で乗った車に8区もそのまま乗り続け、8区で新たに乗ることもできない
    for p in mountain_group:
        for k in car_ids:
            prob += occ[(p, k, 7)] == occ[(p, k, 8)]

    # 7区→8区: 非山行きグループも山行き車では乗り降り禁止（運転手も含む）
    # occ[(p_mtn,k,7)]=1（山グループが乗っている車）のとき、非山行き者のoccも7区=8区に固定する
    # 線形化: occ_p8 + occ_mtn7 <= occ_p7 + 1 かつ occ_p7 + occ_mtn7 <= occ_p8 + 1
    for p in non_mountain_strict:
        for k in car_ids:
            for p_mtn in mountain_group:
                prob += occ[(p, k, 8)] + occ[(p_mtn, k, 7)] <= occ[(p, k, 7)] + 1
                prob += occ[(p, k, 7)] + occ[(p_mtn, k, 7)] <= occ[(p, k, 8)] + 1

    # 前区間を走った人は次区間の運転手をなるべく避ける（体力保護、ソフト制約）
    prev_run_drive_vars = []
    for i in range(len(sections) - 1):
        s_curr, s_next = sections[i], sections[i + 1]
        for p in pids:
            if (p, s_curr) not in runs:
                continue
            drive_next = [drive[(p, k, s_next)] for k in car_ids if (p, k, s_next) in drive]
            if not drive_next:
                continue
            v = pulp.LpVariable(f"prd_{p}_{s_curr}", cat="Binary")
            prob += v >= runs[(p, s_curr)] + pulp.lpSum(drive_next) - 1
            prev_run_drive_vars.append(v)

    # 7〜8区: 山行きランナー（山道免許なし）がドライバーになることをペナルティで抑制
    # 山道免許持ちが優先的にドライバーになるよう目的関数で誘導する（ハード制約だと詰まるため）
    mountain_runners = [p for p in mountain_hopefuls if not participants[p].can_drive_mountain]
    mtn_runner_drive_vars = []
    for s in [7, 8]:
        for k in car_ids:
            for p_run in mountain_runners:
                if (p_run, k, s) in drive:
                    mtn_runner_drive_vars.append(drive[(p_run, k, s)])

    match_vars = []
    for p in pids:
        for k in car_ids:
            for i in range(1, len(sections)):
                s_prev, s_cur = sections[i - 1], sections[i]
                m = pulp.LpVariable(f"match_{p}_{k}_{s_prev}_{s_cur}", cat="Binary")
                prob += m <= occ[(p, k, s_prev)]
                prob += m <= occ[(p, k, s_cur)]
                match_vars.append(m)

    prob += (
        W_FLEET * pulp.lpSum(CAR_COST[k] * rent[k] for k in car_ids)
        - W_CONTINUITY * pulp.lpSum(match_vars)
        - W_RUNNER_PREF * pulp.lpSum(runs.values())
        + W_MTN_RUNNER_DRIVE * pulp.lpSum(mtn_runner_drive_vars)
        + W_ADVANCE_SPREAD * pulp.lpSum(adv_car_vars)
        + W_PREV_RUN_DRIVE * pulp.lpSum(prev_run_drive_vars)
        + W_PARK * pulp.lpSum(park_vars)
        + W_NO_GRADE2 * pulp.lpSum(no_grade2_vars)
        + W_NO_PASSENGER * pulp.lpSum(no_passenger_vars)
    )

    ctx = dict(rent=rent, runs=runs, drive=drive, ride=ride, usedcar=usedcar, pids=pids, sections=sections, car_ids=car_ids)
    return prob, ctx


def _extract_block_a(ctx, participants):
    pids, sections = ctx["pids"], ctx["sections"]
    runs, drive, ride, usedcar, rent = ctx["runs"], ctx["drive"], ctx["ride"], ctx["usedcar"], ctx["rent"]
    car_ids = ctx["car_ids"]
    # preferred_sectionsのパースに依存せず、Block Bで実際に山に行く人を含む集合として再計算
    mountain_group = (
        {p for p in pids if participants[p].preferred_sections[8] or participants[p].preferred_sections[9]}
        | {p for p in pids if participants[p].can_drive_mountain}
    )

    sections_state: List[SectionState] = []
    for s_idx, s in enumerate(sections):
        runner_ids = [p for p in pids if (p, s) in runs and _val(runs[(p, s)]) == 1]

        # 次走者 = この区間では走らないが次の区間で走る人
        s_next = sections[s_idx + 1] if s_idx + 1 < len(sections) else None
        if s_next:
            next_runners = {p for p in pids if (p, s_next) in runs and _val(runs[(p, s_next)]) == 1}
        else:
            # 8区→9区: preferred_sectionsで代替
            next_runners = {p for p in pids if participants[p].preferred_sections[8] and _present(participants, p, 9)}

        cars = []
        for k in car_ids:
            if _val(usedcar[(k, s)]) == 0:
                continue
            driver_id = next(
                (p for p in pids if (p, k, s) in drive and _val(drive[(p, k, s)]) == 1),
                "NO_DRIVER",
            )
            passenger_ids = [p for p in pids if (p, k, s) in ride and _val(ride[(p, k, s)]) == 1]
            car_people = {driver_id} | set(passenger_ids)
            is_adv = bool(car_people & next_runners)
            # 7・8区で山行き組が乗っている車に「山行き」ラベルを付ける
            is_mtn_car = s in (7, 8) and bool(car_people & mountain_group)
            cars.append(
                CarState(
                    car_id=k,
                    driver_id=driver_id,
                    passenger_ids=passenger_ids,
                    is_mountain_goer=is_mtn_car,
                    is_advance=is_adv,
                    car_type=CAR_TYPE[k],
                    group=None,
                )
            )
        sections_state.append(SectionState(section_id=s, runner_ids=runner_ids, cars=cars))

    rent_solution = {k: _val(rent[k]) for k in car_ids}
    runs_used_in_a = {p: 0 for p in pids}
    for (p, s), v in runs.items():
        runs_used_in_a[p] += _val(v)
    ran_section_8 = {p for p in pids if (p, 8) in runs and _val(runs[(p, 8)]) == 1}

    return sections_state, rent_solution, runs_used_in_a, ran_section_8


def _build_block_b(participants: Dict[str, Participant], rent_solution: Dict[str, int], runs_used_in_a: Dict[str, int], ran_section_8: set = None):
    # 9区・10区は1往復で完結するため、両方とも会場に残っている人だけが対象
    pids = [p for p in participants if _present(participants, p, 9) and _present(participants, p, 10)]
    sections = BLOCK_B_SECTIONS
    rented_cars = [k for k in ALL_CAR_IDS if rent_solution.get(k, 0) == 1]
    mountain_capable = [p for p in pids if participants[p].can_drive_mountain]

    if not mountain_capable:
        raise RuntimeError(
            "山道運転可の参加者が見つかりません。"
            "9・10区の山行き車を運転できる人を少なくとも1人登録してください。"
        )

    prob = pulp.LpProblem("hakone_block_b", pulp.LpMinimize)

    mtn = {p: pulp.LpVariable(f"mtn_{p}", cat="Binary") for p in pids}
    mtn_car = {k: pulp.LpVariable(f"mtncar_{k}", cat="Binary") for k in rented_cars}

    remaining_budget = {
        p: max(participants[p].remaining_sections - runs_used_in_a.get(p, 0), 0) for p in pids
    }

    runs = {}
    for p in pids:
        if remaining_budget[p] <= 0:
            continue
        for s in sections:
            if participants[p].preferred_sections[s - 1]:
                runs[(p, s)] = pulp.LpVariable(f"runsB_{p}_{s}", cat="Binary")

    drive = {
        (p, k, s): pulp.LpVariable(f"driveB_{p}_{k}_{s}", cat="Binary")
        for p in mountain_capable
        for k in rented_cars
        for s in sections
    }
    ride = {
        (p, k, s): pulp.LpVariable(f"rideB_{p}_{k}_{s}", cat="Binary")
        for p in pids
        for k in rented_cars
        for s in sections
    }
    usedcar = {
        (k, s): pulp.LpVariable(f"usedB_{k}_{s}", cat="Binary") for k in rented_cars for s in sections
    }

    for p in pids:
        run_vars = [v for (pp, s), v in runs.items() if pp == p]
        if run_vars:
            prob += pulp.lpSum(run_vars) <= remaining_budget[p]

        for s in sections:
            terms = []
            if (p, s) in runs:
                terms.append(runs[(p, s)])
                prob += runs[(p, s)] <= mtn[p]
            terms += [drive[(p, k, s)] for k in rented_cars if (p, k, s) in drive]
            terms += [ride[(p, k, s)] for k in rented_cars if (p, k, s) in ride]
            prob += pulp.lpSum(terms) == mtn[p]

    b_no_passenger_vars = []
    b_no_grade2_vars = []
    for k in rented_cars:
        for s in sections:
            drivers_ks = [drive[(p, k, s)] for p in mountain_capable if (p, k, s) in drive]
            prob += pulp.lpSum(drivers_ks) == usedcar[(k, s)]
            prob += usedcar[(k, s)] <= mtn_car[k]

            riders_ks = [ride[(p, k, s)] for p in pids if (p, k, s) in ride]
            prob += pulp.lpSum(riders_ks) <= (CAR_CAPACITY[k] - 1) * usedcar[(k, s)]
            v_pass = pulp.LpVariable(f"bno_pass_{k}_{s}", cat="Binary")
            prob += v_pass >= usedcar[(k, s)] - pulp.lpSum(riders_ks)
            b_no_passenger_vars.append(v_pass)

            grade2_riders = [
                ride[(p, k, s)] for p in pids if participants[p].grade >= 2 and (p, k, s) in ride
            ]
            v_g2 = pulp.LpVariable(f"bno_g2_{k}_{s}", cat="Binary")
            prob += v_g2 >= usedcar[(k, s)] - pulp.lpSum(grade2_riders)
            b_no_grade2_vars.append(v_g2)

        # 山行き車両は9区→10区でドライバーを変えない（1往復のみという前提）
        for p in mountain_capable:
            if (p, k, 9) in drive and (p, k, 10) in drive:
                prob += drive[(p, k, 9)] == drive[(p, k, 10)]

    for s in sections:
        runners_s = [runs[(p, s)] for p in pids if (p, s) in runs]
        if runners_s:  # runs変数がゼロだとinfeasibleになるためスキップ
            prob += pulp.lpSum(runners_s) >= 1  # 9区・10区も最低1人は走者を確保

    # 山グループ全員が山行き車に収まること（行きの最大乗車時 = 10区終了後の帰り）
    prob += (
        pulp.lpSum(mtn[p] for p in pids)
        <= pulp.lpSum(CAR_CAPACITY[k] * mtn_car[k] for k in rented_cars)
    )

    # ホテルグループ（非山行き）が非山行き車に収まること
    total_rented_cap = sum(CAR_CAPACITY[k] for k in rented_cars)
    prob += (
        len(pids) - pulp.lpSum(mtn[p] for p in pids)
        <= total_rented_cap - pulp.lpSum(CAR_CAPACITY[k] * mtn_car[k] for k in rented_cars)
    )

    # 8区を走った人は9区の山行き車の運転をなるべく避ける（体力保護、ソフト制約）
    prev_run_drive_b_vars = []
    if ran_section_8:
        for p in mountain_capable:
            if p not in ran_section_8:
                continue
            for k in rented_cars:
                if (p, k, 9) not in drive:
                    continue
                v = pulp.LpVariable(f"prd_b_{p}_{k}", cat="Binary")
                prob += v >= drive[(p, k, 9)]
                prev_run_drive_b_vars.append(v)

    # 山行き車のparkペナルティ: mtn_car[k]=1なのにusedcar[(k,s)]=0になるケース
    b_park_vars = []
    for k in rented_cars:
        for s in sections:
            v = pulp.LpVariable(f"bpark_{k}_{s}", cat="Binary")
            prob += v >= mtn_car[k] - usedcar[(k, s)]
            b_park_vars.append(v)

    prob += (
        0.1 * pulp.lpSum(mtn_car.values())  # 山行きに使う車はできるだけ少なく
        - W_RUNNER_PREF * pulp.lpSum(runs.values())
        + W_PREV_RUN_DRIVE * pulp.lpSum(prev_run_drive_b_vars)
        + W_NO_PASSENGER * pulp.lpSum(b_no_passenger_vars)
        + W_NO_GRADE2 * pulp.lpSum(b_no_grade2_vars)
        + W_PARK * pulp.lpSum(b_park_vars)
    )

    ctx = dict(
        mtn=mtn, mtn_car=mtn_car, runs=runs, drive=drive, ride=ride, usedcar=usedcar,
        pids=pids, sections=sections, rented_cars=rented_cars,
    )
    return prob, ctx


def _extract_block_b(ctx, participants):
    pids, sections = ctx["pids"], ctx["sections"]
    runs, drive, ride, usedcar, mtn, mtn_car = (
        ctx["runs"], ctx["drive"], ctx["ride"], ctx["usedcar"], ctx["mtn"], ctx["mtn_car"]
    )
    rented_cars = ctx["rented_cars"]

    mountain_group = [p for p in pids if _val(mtn[p]) == 1]
    mountain_cars_used = [k for k in rented_cars if _val(mtn_car[k]) == 1]
    total_capacity = sum(CAR_CAPACITY[k] for k in mountain_cars_used)
    names = ", ".join(participants[p].name for p in mountain_group)
    print(
        f"  ⛰️ 山グループ: {len(mountain_group)}名 / 山行き車定員合計: {total_capacity}名"
        f"  ({names})"
    )

    sections_state: List[SectionState] = []
    for s in sections:
        runner_ids = [p for p in pids if (p, s) in runs and _val(runs[(p, s)]) == 1]
        cars = []
        for k in rented_cars:
            if _val(usedcar[(k, s)]) == 0:
                continue
            driver_id = next(
                (p for p in pids if (p, k, s) in drive and _val(drive[(p, k, s)]) == 1),
                "NO_DRIVER",
            )
            passenger_ids = [p for p in pids if (p, k, s) in ride and _val(ride[(p, k, s)]) == 1]
            cars.append(
                CarState(
                    car_id=k,
                    driver_id=driver_id,
                    passenger_ids=passenger_ids,
                    is_mountain_goer=True,
                    car_type=CAR_TYPE[k],
                    group="mountain",
                )
            )
        sections_state.append(SectionState(section_id=s, runner_ids=runner_ids, cars=cars))

    return sections_state


def _assign_hotel_group(ctx_b, participants, ran_section_8: set = None) -> List[CarState]:
    """Block Bで山グループに入らなかった人（ホテルグループ）を非山行き車に割り当てる。
    9区・10区は同じ配車で固定（ホテル滞在中のため移動なし）。"""
    pids = ctx_b["pids"]
    rented_cars = ctx_b["rented_cars"]
    mtn = ctx_b["mtn"]
    mtn_car = ctx_b["mtn_car"]

    hotel_pids = [p for p in pids if _val(mtn[p]) == 0]
    hotel_cars = [k for k in rented_cars if _val(mtn_car[k]) == 0]

    if not hotel_pids or not hotel_cars:
        return []

    drive = {
        (p, k): pulp.LpVariable(f"driveH_{p}_{k}", cat="Binary")
        for p in hotel_pids
        for k in hotel_cars
        if participants[p].can_drive
        and not (CAR_TYPE[k] == "large" and not participants[p].can_drive_large)
        and p not in (ran_section_8 or set())
    }
    if not drive:
        print("  ⚠️ ホテルグループに運転できる人がいないため、ホテル組の配車をスキップします。")
        return []

    prob = pulp.LpProblem("hakone_hotel", pulp.LpMinimize)
    ride = {
        (p, k): pulp.LpVariable(f"rideH_{p}_{k}", cat="Binary")
        for p in hotel_pids
        for k in hotel_cars
    }
    usedcar = {k: pulp.LpVariable(f"usedH_{k}", cat="Binary") for k in hotel_cars}

    for p in hotel_pids:
        terms = [drive[(p, k)] for k in hotel_cars if (p, k) in drive]
        terms += [ride[(p, k)] for k in hotel_cars]
        prob += pulp.lpSum(terms) == 1

    h_no_passenger_vars = []
    h_no_grade2_vars = []
    for k in hotel_cars:
        drivers_k = [drive[(p, k)] for p in hotel_pids if (p, k) in drive]
        prob += pulp.lpSum(drivers_k) == usedcar[k]

        riders_k = [ride[(p, k)] for p in hotel_pids]
        prob += pulp.lpSum(riders_k) <= (CAR_CAPACITY[k] - 1) * usedcar[k]
        v_pass = pulp.LpVariable(f"hno_pass_{k}", cat="Binary")
        prob += v_pass >= usedcar[k] - pulp.lpSum(riders_k)
        h_no_passenger_vars.append(v_pass)

        grade2 = [ride[(p, k)] for p in hotel_pids if participants[p].grade >= 2]
        v_g2 = pulp.LpVariable(f"hno_g2_{k}", cat="Binary")
        prob += v_g2 >= usedcar[k] - pulp.lpSum(grade2)
        h_no_grade2_vars.append(v_g2)

    # レンタル済みの全車両を使うよう強く誘導（W_PARK > W_FLEET*コストなので借りた車は全台使う方が得）
    prob += (
        -W_PARK * pulp.lpSum(usedcar.values())
        + W_NO_PASSENGER * pulp.lpSum(h_no_passenger_vars)
        + W_NO_GRADE2 * pulp.lpSum(h_no_grade2_vars)
    )
    _solve(prob)

    cars = []
    for k in hotel_cars:
        if _val(usedcar[k]) == 0:
            continue
        driver_id = next(
            (p for p in hotel_pids if (p, k) in drive and _val(drive[(p, k)]) == 1),
            "NO_DRIVER",
        )
        passenger_ids = [p for p in hotel_pids if _val(ride[(p, k)]) == 1]
        cars.append(CarState(
            car_id=k,
            driver_id=driver_id,
            passenger_ids=passenger_ids,
            is_mountain_goer=False,
            car_type=CAR_TYPE[k],
            group="hotel",
        ))
    return cars


def _build_return_trip(participants: Dict[str, Participant], rent_solution: Dict[str, int]):
    """最後まで残った人がホテル/箱根から帰る行程。ランナーや山道免許は関係なく、
    レンタルした車(rent_solutionで1のもの)だけを使って運ぶ。日帰りで先に
    離脱した人(leaves_after_sectionが設定されている人)はここには含めない。"""
    pids = [p for p in participants if participants[p].leaves_after_section is None]
    rented_cars = [k for k in ALL_CAR_IDS if rent_solution.get(k, 0) == 1]

    prob = pulp.LpProblem("hakone_return_trip", pulp.LpMinimize)

    drive = {
        (p, k): pulp.LpVariable(f"driveR_{p}_{k}", cat="Binary")
        for p in pids
        if participants[p].can_drive
        for k in rented_cars
        if not (CAR_TYPE[k] == "large" and not participants[p].can_drive_large)
    }
    ride = {
        (p, k): pulp.LpVariable(f"rideR_{p}_{k}", cat="Binary")
        for p in pids
        for k in rented_cars
    }
    usedcar = {k: pulp.LpVariable(f"usedR_{k}", cat="Binary") for k in rented_cars}

    for p in pids:
        terms = [drive[(p, k)] for k in rented_cars if (p, k) in drive]
        terms += [ride[(p, k)] for k in rented_cars]
        prob += pulp.lpSum(terms) == 1  # 全員が必ずどれかの車に乗る

    r_no_passenger_vars = []
    r_no_grade2_vars = []
    for k in rented_cars:
        drivers_k = [drive[(p, k)] for p in pids if (p, k) in drive]
        prob += pulp.lpSum(drivers_k) == usedcar[k]

        riders_k = [ride[(p, k)] for p in pids]
        prob += pulp.lpSum(riders_k) <= (CAR_CAPACITY[k] - 1) * usedcar[k]
        v_pass = pulp.LpVariable(f"rno_pass_{k}", cat="Binary")
        prob += v_pass >= usedcar[k] - pulp.lpSum(riders_k)
        r_no_passenger_vars.append(v_pass)

        grade2_riders = [ride[(p, k)] for p in pids if participants[p].grade >= 2]
        v_g2 = pulp.LpVariable(f"rno_g2_{k}", cat="Binary")
        prob += v_g2 >= usedcar[k] - pulp.lpSum(grade2_riders)
        r_no_grade2_vars.append(v_g2)

    # レンタル済みの全車両を使うよう強く誘導
    prob += (
        -W_PARK * pulp.lpSum(usedcar.values())
        + W_NO_PASSENGER * pulp.lpSum(r_no_passenger_vars)
        + W_NO_GRADE2 * pulp.lpSum(r_no_grade2_vars)
    )

    ctx = dict(drive=drive, ride=ride, usedcar=usedcar, pids=pids, rented_cars=rented_cars)
    return prob, ctx


def _extract_return_trip(ctx, participants):
    pids, rented_cars = ctx["pids"], ctx["rented_cars"]
    drive, ride, usedcar = ctx["drive"], ctx["ride"], ctx["usedcar"]

    cars = []
    for k in rented_cars:
        if _val(usedcar[k]) == 0:
            continue
        driver_id = next(
            (p for p in pids if (p, k) in drive and _val(drive[(p, k)]) == 1),
            "NO_DRIVER",
        )
        passenger_ids = [p for p in pids if _val(ride[(p, k)]) == 1]
        cars.append(
            CarState(
                car_id=k,
                driver_id=driver_id,
                passenger_ids=passenger_ids,
                is_mountain_goer=False,
                car_type=CAR_TYPE[k],
                group="return",
            )
        )
    return SectionState(section_id=RETURN_TRIP_SECTION_ID, runner_ids=[], cars=cars)


def _renumber_cars(plan: List[SectionState], rent_solution: Dict[str, int]) -> None:
    """候補車スロットのIDは全て対等(同種なら容量・コストが同じ)なので、
    実際にレンタルした車だけを連番(L1,L2,...,N1,N2,...)に振り直して表示をわかりやすくする。"""
    used = [k for k in ALL_CAR_IDS if rent_solution.get(k, 0) == 1]
    large_used = [k for k in used if CAR_TYPE[k] == "large"]
    normal_used = [k for k in used if CAR_TYPE[k] == "normal"]
    mapping = {k: f"L{i}" for i, k in enumerate(large_used, start=1)}
    mapping.update({k: f"N{i}" for i, k in enumerate(normal_used, start=1)})
    for section in plan:
        for car in section.cars:
            car.car_id = mapping.get(car.car_id, car.car_id)


def generate_full_plan_milp(participants: Dict[str, Participant], use_large_cars: bool = True) -> List[SectionState]:
    # 使用する車種に応じて候補車IDを絞る
    active_car_ids = ALL_CAR_IDS if use_large_cars else NORMAL_CAR_IDS
    mode_label = "大型車 + 小型車" if use_large_cars else "小型車のみ"

    # 診断ログ: ローカルとアプリの差異を特定するため参加者データを出力
    n_drive = sum(1 for p in participants.values() if p.can_drive)
    n_large = sum(1 for p in participants.values() if p.can_drive_large)
    n_mtn   = sum(1 for p in participants.values() if p.can_drive_mountain)
    n_stay  = sum(1 for p in participants.values() if p.leaves_after_section is None)
    total_pref = sum(sum(p.preferred_sections) for p in participants.values())
    print(f"🚗 車種モード: {mode_label}")
    print(f"📋 参加者データ: 計{len(participants)}人 / 運転可={n_drive} / 大型可={n_large} / 山道可={n_mtn} / 宿泊(帰路対象)={n_stay} / 希望延べ区間数={total_pref}")
    for pid, p in participants.items():
        secs = [i+1 for i,v in enumerate(p.preferred_sections) if v]
        print(f"  {p.name}: 希望{secs} 走行上限{p.remaining_sections} 運転{'○' if p.can_drive else '×'} 大型{'○' if p.can_drive_large else '×'} 宿泊{'○' if p.leaves_after_section is None else f'×(〜{p.leaves_after_section}区)'}")

    prob_a, ctx_a = _build_block_a(participants, car_ids=active_car_ids)
    status_a = _solve(prob_a)
    obj_a = pulp.value(prob_a.objective)
    print(f"Block A (1〜8区) 最適化ステータス: {status_a}  目的関数値={obj_a:.2f}")
    if status_a not in ("Optimal", "Feasible"):
        raise RuntimeError(f"Block A(1〜8区)が解けませんでした: {status_a}")
    if status_a == "Feasible":
        print("  ⚠️ 制限時間内に最適性は証明できませんでしたが、見つかった解を使用します。")

    sections_a, rent_solution, runs_used_in_a, ran_section_8 = _extract_block_a(ctx_a, participants)
    if ran_section_8:
        names_s8 = ", ".join(participants[p].name for p in ran_section_8)
        print(f"  🏃 8区走者（次区間の運転除外）: {names_s8}")

    prob_b, ctx_b = _build_block_b(participants, rent_solution, runs_used_in_a, ran_section_8)
    status_b = _solve(prob_b)
    print(f"Block B (9〜10区) 最適化ステータス: {status_b}")
    if status_b not in ("Optimal", "Feasible"):
        raise RuntimeError(f"Block B(9〜10区)が解けませんでした: {status_b}")
    if status_b == "Feasible":
        print("  ⚠️ 制限時間内に最適性は証明できませんでしたが、見つかった解を使用します。")

    sections_b = _extract_block_b(ctx_b, participants)

    # 7・8区の山行きラベルをBlock Bの実際の山グループ（mtn変数）で確定する。
    # preferred_sectionsのパース精度に依存しないため、Googleフォーム形式でも確実に動く。
    mountain_group_b = {p for p in ctx_b["pids"] if _val(ctx_b["mtn"][p]) == 1}
    names_b = ", ".join(participants[p].name for p in mountain_group_b)
    print(f"  🏔️ Block B 確定山グループ: {names_b if names_b else '（なし）'}")
    for section in sections_a:
        if section.section_id in (7, 8):
            for car in section.cars:
                car_people = {car.driver_id} | set(car.passenger_ids)
                car.is_mountain_goer = bool(car_people & mountain_group_b)

    hotel_cars = _assign_hotel_group(ctx_b, participants, ran_section_8)
    if hotel_cars:
        hotel_total = sum(c.total_people for c in hotel_cars)
        names = ", ".join(
            participants[p].name
            for c in hotel_cars
            for p in [c.driver_id] + c.passenger_ids
            if p in participants
        )
        print(f"  🏨 ホテルグループ: {hotel_total}名 (車{len(hotel_cars)}台)  ({names})")
        for sec in sections_b:
            sec.cars.extend(hotel_cars)

    prob_c, ctx_c = _build_return_trip(participants, rent_solution)
    status_c = _solve(prob_c)
    print(f"帰路 最適化ステータス: {status_c}")
    if status_c not in ("Optimal", "Feasible"):
        raise RuntimeError(f"帰路の割り当てが解けませんでした: {status_c}")
    if status_c == "Feasible":
        print("  ⚠️ 制限時間内に最適性は証明できませんでしたが、見つかった解を使用します。")
    section_c = _extract_return_trip(ctx_c, participants)

    plan = sections_a + sections_b + [section_c]
    _renumber_cars(plan, rent_solution)
    for section in plan:
        errors = validate_section(section, participants)
        label = section_label(section.section_id)
        if errors:
            print(f"❌ {label}でエラー: " + " / ".join(errors))
        else:
            print(f"✅ {label}は問題なく割り当てられました！")

    used = [k for k, v in rent_solution.items() if v == 1]
    large_n = sum(1 for k in used if CAR_TYPE[k] == "large")
    normal_n = sum(1 for k in used if CAR_TYPE[k] == "normal")
    print(f"\n🚗 レンタルした車: 大型{large_n}台 + 普通{normal_n}台 = 合計{len(used)}台")

    save_plan_to_csv(plan, participants, "hakone_result.csv")
    return plan
