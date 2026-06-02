"""
app.py — Streamlit-интерфейс аналитического ассистента ПСС МЧС.

Запуск:
    streamlit run app.py
"""

import logging

import pandas as pd
import streamlit as st

from assistant import AskResult, Turn, ask
from config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

st.set_page_config(
    page_title="Ассистент ПСС МЧС",
    page_icon="🚒",
    layout="wide",
)


st.title("🚒 Аналитический ассистент ПСС МЧС")
st.caption("Вопросы о выездах и занятиях поисково-спасательной службы — на русском языке")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "history" not in st.session_state:
    st.session_state.history = []

with st.sidebar:
    st.header("Настройки")
    show_sql = st.toggle(
        "Показывать SQL-запрос",
        value=True,
        help="Ассистент может ошибиться в интерпретации вопроса. "
             "Видимый SQL — единственный способ это заметить.",
    )
    show_table = st.toggle("Показывать таблицу данных", value=True)
    show_debug = st.toggle("Технические детали", value=False)

    st.divider()
    st.subheader("Примеры вопросов")
    for example in (
        "Сколько всего выездов в базе?",
        "Сколько было выездов на ДТП по районам?",
        "Какой тип происшествий встречается чаще всего?",
        "Сколько выездов было с пострадавшими?",
        "Покажи количество выездов по месяцам",
        "Среднее время выезда по подразделениям",
    ):
        if st.button(example, width='stretch'):
            st.session_state.pending_question = example

    st.divider()
    if st.button("🗑️ Очистить историю", width='stretch'):
        st.session_state.messages = []
        st.session_state.history = []
        st.rerun()


def render_result(result: AskResult) -> None:
    """Отрисовывает один ответ ассистента."""
    st.markdown(result.answer)

    if show_sql and result.sql:
        with st.expander("SQL-запрос"):
            st.code(result.sql, language="sql")

    if show_table and result.rows and not result.not_applicable:
        df = pd.DataFrame(result.rows, columns=result.columns or None)
        st.dataframe(df, width='stretch', hide_index=True)
        if len(result.rows) >= settings.max_rows:
            st.caption(
                f"Показаны первые {settings.max_rows} строк — на выдачу наложено ограничение."
            )

    if show_debug:
        details = [
            f"время: {result.elapsed_ms} мс",
            f"попыток сгенерировать SQL: {result.attempts}",
            f"строк получено: {len(result.rows)}",
        ]
        if result.examples_used:
            details.append("похожие примеры из банка: " + "; ".join(result.examples_used))
        st.caption(" · ".join(details))

    if result.error:
        st.error(result.error)


def process_question(user_input: str) -> None:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Обрабатываю запрос..."):
            result = ask(user_input, history=st.session_state.history)
        render_result(result)

    st.session_state.messages.append({"role": "assistant", "result": result})
    # В историю для модели уходит только пара «вопрос → SQL»: этого хватает
    # для уточняющих вопросов, а строки данных туда тащить не нужно.
    st.session_state.history.append(Turn(question=user_input, sql=result.sql))


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.markdown(message["content"])
        else:
            render_result(message["result"])

if "pending_question" in st.session_state:
    process_question(st.session_state.pop("pending_question"))
    st.rerun()

if user_input := st.chat_input("Задайте вопрос о выездах или занятиях ПСС..."):
    process_question(user_input)
