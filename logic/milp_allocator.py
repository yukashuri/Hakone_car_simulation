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
from logic.car_pool import ALL_CAR_IDS, CAR_TYPE, CAR_CAPACITY, CAR_COST, section_label
from logic.allocator import save_plan_to_csv

W_FLEET = 1000
W_CONTINUITY = 5
W_RUNNER_PREF = 50
W_ADVANCE = 2

BLOCK_A_SECTIONS = list(range(1, 9))
BLOCK_B_SECTIONS = [9, 10]
RETURN_TRIP_SECTION_ID = 11  # 全員がホテル/箱根から帰る行程（CSV上は「帰路」と表示）


def _solve(prob: pulp.LpProblem, time_limit: int = 90) -> str:
    prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit))
    return pulp.LpStatus[prob.status]


def _val(var) -> int:
    v = var.value()
    return 1 if v is not None and v > 0.5 else 0


def _present(participants: Dict[str, Participant], p: str, s: int) -> bool:
    """その人がその区間の時点でまだ会場に残っているか（日帰りで先に離脱していないか）。"""
    leaves = participants[p].leaves_after_section
    return leaves is None or s <= leaves


def _build_block_a(participants: Dict[str, Participant]):
    pids = list(participants.keys())
    sections = BLOCK_A_SECTIONS

    prob = pulp.LpProblem("hakone_block_a", pulp.LpMinimize)

    rent = {k: pulp.LpVariable(f"rent_{k}", cat="Binary") for k in ALL_CAR_IDS}

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
        for k in ALL_CAR_IDS:
            if CAR_TYPE[k] == "large" and not part.can_drive_large:
                continue
            for s in sections:
                if _present(participants, p, s):
                    drive[(p, k, s)] = pulp.LpVariable(f"drive_{p}_{k}_{s}", cat="Binary")

    ride = {
        (p, k, s): pulp.LpVariable(f"ride_{p}_{k}_{s}", cat="Binary")
        for p in pids
        for k in ALL_CAR_IDS
        for s in sections
        if _present(participants, p, s)
    }

    usedcar = {
        (k, s): pulp.LpVariable(f"used_{k}_{s}", cat="Binary")
        for k in ALL_CAR_IDS
        for s in sections
    }

    for s in sections:
        for k in ALL_CAR_IDS:
            drivers_ks = [drive[(p, k, s)] for p in pids if (p, k, s) in drive]
            prob += pulp.lpSum(drivers_ks) == usedcar[(k, s)]
            prob += usedcar[(k, s)] <= rent[k]

            riders_ks = [ride[(p, k, s)] for p in pids if (p, k, s) in ride]
            prob += pulp.lpSum(riders_ks) <= (CAR_CAPACITY[k] - 1) * usedcar[(k, s)]
            prob += pulp.lpSum(riders_ks) >= usedcar[(k, s)]

            grade2_riders = [ride[(p, k, s)] for p in pids if participants[p].grade >= 2 and (p, k, s) in ride]
            prob += pulp.lpSum(grade2_riders) >= usedcar[(k, s)]

        for p in pids:
            if not _present(participants, p, s):
                continue  # 日帰りで既に離脱した人はこの区間の役割を持たない
            terms = []
            if (p, s) in runs:
                terms.append(runs[(p, s)])
            terms += [drive[(p, k, s)] for k in ALL_CAR_IDS if (p, k, s) in drive]
            terms += [ride[(p, k, s)] for k in ALL_CAR_IDS if (p, k, s) in ride]
            prob += pulp.lpSum(terms) == 1

        runners_s = [runs[(p, s)] for p in pids if (p, s) in runs]
        prob += pulp.lpSum(runners_s) >= 1  # 各区間に最低1人は走者を確保

    for p in pids:
        run_vars = [v for (pp, s), v in runs.items() if pp == p]
        if run_vars:
            prob += pulp.lpSum(run_vars) <= participants[p].remaining_sections

    # 帰りは最後まで残る人を同時に車で運ぶ必要があるため、レンタル車の総定員は
    # 日帰りで先に離脱する人を除いた人数以上にする
    return_trip_count = sum(1 for p in pids if participants[p].leaves_after_section is None)
    prob += pulp.lpSum(CAR_CAPACITY[k] * rent[k] for k in ALL_CAR_IDS) >= return_trip_count

    occ = {}
    for p in pids:
        for k in ALL_CAR_IDS:
            for s in sections:
                terms = []
                if (p, k, s) in ride:
                    terms.append(ride[(p, k, s)])
                if (p, k, s) in drive:
                    terms.append(drive[(p, k, s)])
                occ[(p, k, s)] = pulp.lpSum(terms)

    match_vars = []
    for p in pids:
        for k in ALL_CAR_IDS:
            for i in range(1, len(sections)):
                s_prev, s_cur = sections[i - 1], sections[i]
                m = pulp.LpVariable(f"match_{p}_{k}_{s_prev}_{s_cur}", cat="Binary")
                prob += m <= occ[(p, k, s_prev)]
                prob += m <= occ[(p, k, s_cur)]
                match_vars.append(m)

    # 先行車変数: 次走者を乗せた車は先行車として次の中継所へ早めに向かう
    advance = {
        (k, s): pulp.LpVariable(f"adv_{k}_{s}", cat="Binary")
        for k in ALL_CAR_IDS
        for s in sections
    }
    for s_idx, s in enumerate(sections):
        if s_idx + 1 < len(sections):
            s_next = sections[s_idx + 1]
            for p in pids:
                if (p, s_next) in runs:
                    for k in ALL_CAR_IDS:
                        # occ[p,k,s]=1 かつ runs[p,s_next]=1 → advance[k,s]=1
                        prob += advance[(k, s)] >= occ[(p, k, s)] + runs[(p, s_next)] - 1
        else:
            # 8区→9区はBlock B管轄のため preferred_sections で代替判断
            for p in pids:
                if participants[p].preferred_sections[8] and _present(participants, p, 9):
                    for k in ALL_CAR_IDS:
                        prob += advance[(k, s)] >= occ[(p, k, s)]

    prob += (
        W_FLEET * pulp.lpSum(CAR_COST[k] * rent[k] for k in ALL_CAR_IDS)
        - W_CONTINUITY * pulp.lpSum(match_vars)
        - W_RUNNER_PREF * pulp.lpSum(runs.values())
        + W_ADVANCE * pulp.lpSum(advance.values())
    )

    ctx = dict(rent=rent, runs=runs, drive=drive, ride=ride, usedcar=usedcar, advance=advance, pids=pids, sections=sections)
    return prob, ctx


def _extract_block_a(ctx, participants):
    pids, sections = ctx["pids"], ctx["sections"]
    runs, drive, ride, usedcar, rent, advance = ctx["runs"], ctx["drive"], ctx["ride"], ctx["usedcar"], ctx["rent"], ctx["advance"]

    sections_state: List[SectionState] = []
    for s in sections:
        runner_ids = [p for p in pids if (p, s) in runs and _val(runs[(p, s)]) == 1]
        cars = []
        for k in ALL_CAR_IDS:
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
                    is_mountain_goer=False,
                    is_advance=_val(advance[(k, s)]) == 1,
                    car_type=CAR_TYPE[k],
                    group=None,
                )
            )
        sections_state.append(SectionState(section_id=s, runner_ids=runner_ids, cars=cars))

    rent_solution = {k: _val(rent[k]) for k in ALL_CAR_IDS}
    runs_used_in_a = {p: 0 for p in pids}
    for (p, s), v in runs.items():
        runs_used_in_a[p] += _val(v)

    return sections_state, rent_solution, runs_used_in_a


def _build_block_b(participants: Dict[str, Participant], rent_solution: Dict[str, int], runs_used_in_a: Dict[str, int]):
    # 9区・10区は1往復で完結するため、両方とも会場に残っている人だけが対象
    pids = [p for p in participants if _present(participants, p, 9) and _present(participants, p, 10)]
    sections = BLOCK_B_SECTIONS
    rented_cars = [k for k in ALL_CAR_IDS if rent_solution.get(k, 0) == 1]
    mountain_capable = [p for p in pids if participants[p].can_drive_mountain]

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

    for k in rented_cars:
        for s in sections:
            drivers_ks = [drive[(p, k, s)] for p in mountain_capable if (p, k, s) in drive]
            prob += pulp.lpSum(drivers_ks) == usedcar[(k, s)]
            prob += usedcar[(k, s)] <= mtn_car[k]

            riders_ks = [ride[(p, k, s)] for p in pids if (p, k, s) in ride]
            prob += pulp.lpSum(riders_ks) <= (CAR_CAPACITY[k] - 1) * usedcar[(k, s)]
            prob += pulp.lpSum(riders_ks) >= usedcar[(k, s)]

            grade2_riders = [
                ride[(p, k, s)] for p in pids if participants[p].grade >= 2 and (p, k, s) in ride
            ]
            prob += pulp.lpSum(grade2_riders) >= usedcar[(k, s)]

        # 山行き車両は9区→10区でドライバーを変えない（1往復のみという前提）
        for p in mountain_capable:
            if (p, k, 9) in drive and (p, k, 10) in drive:
                prob += drive[(p, k, 9)] == drive[(p, k, 10)]

    for s in sections:
        runners_s = [runs[(p, s)] for p in pids if (p, s) in runs]
        prob += pulp.lpSum(runners_s) >= 1  # 9区・10区も最低1人は走者を確保

    prob += (
        0.1 * pulp.lpSum(mtn_car.values())  # 山行きに使う車はできるだけ少なく
        - W_RUNNER_PREF * pulp.lpSum(runs.values())
    )

    ctx = dict(
        mtn=mtn, mtn_car=mtn_car, runs=runs, drive=drive, ride=ride, usedcar=usedcar,
        pids=pids, sections=sections, rented_cars=rented_cars,
    )
    return prob, ctx


def _extract_block_b(ctx, participants):
    pids, sections = ctx["pids"], ctx["sections"]
    runs, drive, ride, usedcar = ctx["runs"], ctx["drive"], ctx["ride"], ctx["usedcar"]
    rented_cars = ctx["rented_cars"]

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

    for k in rented_cars:
        drivers_k = [drive[(p, k)] for p in pids if (p, k) in drive]
        prob += pulp.lpSum(drivers_k) == usedcar[k]

        riders_k = [ride[(p, k)] for p in pids]
        prob += pulp.lpSum(riders_k) <= (CAR_CAPACITY[k] - 1) * usedcar[k]
        prob += pulp.lpSum(riders_k) >= usedcar[k]

        grade2_riders = [ride[(p, k)] for p in pids if participants[p].grade >= 2]
        prob += pulp.lpSum(grade2_riders) >= usedcar[k]

    prob += pulp.lpSum(usedcar.values())  # 使う車自体はできるだけ少なく(コストは既に確定済み)

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


def generate_full_plan_milp(participants: Dict[str, Participant]) -> List[SectionState]:
    prob_a, ctx_a = _build_block_a(participants)
    status_a = _solve(prob_a)
    print(f"Block A (1〜8区) 最適化ステータス: {status_a}")
    if status_a not in ("Optimal", "Feasible"):
        raise RuntimeError(f"Block A(1〜8区)が解けませんでした: {status_a}")
    if status_a == "Feasible":
        print("  ⚠️ 制限時間内に最適性は証明できませんでしたが、見つかった解を使用します。")

    sections_a, rent_solution, runs_used_in_a = _extract_block_a(ctx_a, participants)

    prob_b, ctx_b = _build_block_b(participants, rent_solution, runs_used_in_a)
    status_b = _solve(prob_b)
    print(f"Block B (9〜10区) 最適化ステータス: {status_b}")
    if status_b not in ("Optimal", "Feasible"):
        raise RuntimeError(f"Block B(9〜10区)が解けませんでした: {status_b}")
    if status_b == "Feasible":
        print("  ⚠️ 制限時間内に最適性は証明できませんでしたが、見つかった解を使用します。")

    sections_b = _extract_block_b(ctx_b, participants)

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
