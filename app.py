import contextlib
import io
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(__file__))

from data_io.sheets_manager import load_participants_from_sheet, save_plan_to_sheet
from logic.milp_allocator import generate_full_plan_milp
from logic.car_pool import section_label

CREDENTIALS_PATH = "credentials.json"

st.set_page_config(page_title="箱根駅伝配車シミュレーター", page_icon="🚗")
st.title("🚗 箱根駅伝配車シミュレーター")

url = st.text_input(
    "スプレッドシートのURL",
    placeholder="https://docs.google.com/spreadsheets/d/...",
)

if st.button("シミュレーション実行", type="primary", disabled=not url):

    log_buf = io.StringIO()

    with st.spinner("データを読み込み中..."):
        try:
            with contextlib.redirect_stdout(log_buf):
                participants = load_participants_from_sheet(url, CREDENTIALS_PATH)
        except Exception as e:
            st.error(f"スプレッドシートの読み込みに失敗しました。\n\n{e}")
            st.stop()

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

                st.markdown(f"**🚘 車 {car.car_id}**（{car_type}）{mt_badge}")
                st.write(f"　👨‍✈️ 運転手: {driver_name}")
                st.write(f"　👥 同乗者: {', '.join(passengers) if passengers else 'なし'}")
                st.divider()

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
