import contextlib
import importlib
import io
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(__file__))

# Streamlit Cloud のホットリロード時にモジュールキャッシュが残るため明示的に再ロードする
import logic.milp_allocator as _milp_mod
importlib.reload(_milp_mod)

from data_io.sheets_manager import (
    load_participants_from_form_sheet,
    load_participants_from_sheet,
    save_plan_to_sheet,
)
from logic.milp_allocator import generate_full_plan_milp
from logic.car_pool import section_label
from validator import validate_participants, validate_transitions, count_car_changes, compute_runner_satisfaction

CREDENTIALS_PATH = "credentials.json"

APP_VERSION = "2026-06-28-mtn-fixed"

st.set_page_config(page_title="箱根駅伝配車シミュレーター", page_icon="🚗")
st.title("🚗 箱根駅伝配車シミュレーター")
st.caption(f"ver {APP_VERSION}")

input_format = st.radio(
    "入力データの形式",
    ["Googleフォームの回答", "独自フォーマット"],
    horizontal=True,
)

if input_format == "Googleフォームの回答":
    st.caption("Googleフォームの回答が集まったスプレッドシートのURLを貼り付けてください。")
else:
    st.caption("所定のフォーマット（名前・1〜10・運転・大・山・宿泊・学年・希望区間数・離脱区間）のスプレッドシートのURLを貼り付けてください。")

url = st.text_input(
    "スプレッドシートのURL",
    placeholder="https://docs.google.com/spreadsheets/d/...",
)

if st.button("シミュレーション実行", type="primary", disabled=not url):

    log_buf = io.StringIO()

    with st.spinner("データを読み込み中..."):
        try:
            with contextlib.redirect_stdout(log_buf):
                if input_format == "Googleフォームの回答":
                    participants = load_participants_from_form_sheet(url, CREDENTIALS_PATH)
                else:
                    participants = load_participants_from_sheet(url, CREDENTIALS_PATH)
        except Exception as e:
            st.error(f"スプレッドシートの読み込みに失敗しました。\n\n{e}")
            st.stop()

    st.info(f"参加者 {len(participants)} 名のデータを読み込みました。")

    input_warnings = validate_participants(participants)
    if input_warnings:
        with st.expander(f"⚠️ 入力データに {len(input_warnings)} 件の問題があります"):
            for w in input_warnings:
                st.warning(w)

    with st.spinner("配車を計算中（数十秒かかる場合があります）..."):
        try:
            with contextlib.redirect_stdout(log_buf):
                plan = generate_full_plan_milp(participants)
        except Exception as e:
            st.error(f"計算に失敗しました。\n\n{e}")
            with st.expander("詳細ログ"):
                st.text(log_buf.getvalue())
            st.stop()

    # 結果の表示
    used_cars = len({car.car_id for section in plan for car in section.cars})
    large_n = len({car.car_id for section in plan for car in section.cars if car.car_type == "large"})
    normal_n = used_cars - large_n
    st.success(f"計算完了！　大型 {large_n} 台 ＋ 普通 {normal_n} 台 ＝ 合計 {used_cars} 台")

    for section in plan:
        label = section_label(section.section_id)
        runners = [participants[pid].name for pid in section.runner_ids if pid in participants]
        runner_text = f"🏃 ランナー {len(runners)} 名: {', '.join(runners)}" if runners else "（ランナーなし）"

        with st.expander(f"【{label}】　{runner_text}"):
            for car in section.cars:
                driver_name = participants[car.driver_id].name if car.driver_id in participants else "エラー"
                passengers = [participants[pid].name for pid in car.passenger_ids if pid in participants]
                car_type = "大型" if car.car_type == "large" else "普通"
                mt_badge = "　⛰️ 山行き" if car.is_mountain_goer else ""
                hotel_badge = "　 ホテル組" if car.group == "hotel" else ""
                adv_badge = "　🚀 先行" if car.is_advance else ""

                st.markdown(f"**🚘 車 {car.car_id}**（{car_type}）{mt_badge}{hotel_badge}{adv_badge}")
                st.write(f"　👨‍✈️ 運転手: {driver_name}")
                st.write(f"　👥 同乗者: {', '.join(passengers) if passengers else 'なし'}")
                st.divider()

    # 区間をまたいだ車両引き継ぎチェック
    transition_errors = validate_transitions(plan, participants)
    if transition_errors:
        st.warning("⚠️ 車両引き継ぎに問題があります: " + " / ".join(transition_errors))

    changes = count_car_changes(plan)
    n_wanted, n_satisfied, total_wanted, total_ran = compute_runner_satisfaction(plan, participants)
    col1, col2 = st.columns(2)
    col1.metric("ランナー希望充足", f"{n_satisfied}/{n_wanted}人", help="希望区間数を達成できた人の割合")
    col2.metric("走行区間数", f"{total_ran}/{total_wanted}区間", help="実際に走れた区間 / 希望合計")
    st.caption(f"ℹ️ 区間をまたいで車を乗り換えた回数: {changes}回")

    # スプレッドシートへの書き出し
    with st.spinner("スプレッドシートに結果を書き込み中..."):
        try:
            with contextlib.redirect_stdout(log_buf):
                result_url = save_plan_to_sheet(plan, participants, url, CREDENTIALS_PATH)
            st.link_button("📄 結果のスプレッドシートを開く", result_url, type="primary")
        except Exception as e:
            st.error(f"スプレッドシートへの書き出しに失敗しました。\n\n{e}")

    with st.expander("詳細ログ"):
        st.text(log_buf.getvalue())
