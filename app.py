"""CyberQuest — Streamlit interface. Run with: streamlit run app.py"""

from __future__ import annotations

import os
import re
from pathlib import Path

import streamlit as st

import achievements
import database as db
from game_engine import (
    AttemptState,
    Artifact,
    Content,
    ContentError,
    DecisionRecord,
    Mission,
    Reference,
    Scene,
    advance,
    current_scene,
    decision_stage,
    load_content,
    restore_attempt,
    start_attempt,
    submit_answer,
)
from game_view import load_world, render_world, validate_world
from reporting import (
    attempts_to_csv,
    build_html_report,
    decision_details,
    format_timestamp,
    mission_title,
)
from scoring import POINTS_PREFERRED, format_score

DB_PATH = Path(os.environ.get("CYBERQUEST_DB", db.DEFAULT_DB_PATH))
CONTENT_WIDTH = 1050
SCORE_DISCLAIMER = (
    "Баллы показывают результат в этой учебной игре и не являются профессиональной "
    "квалификацией в области кибербезопасности."
)
ARTIFACT_LABELS = {"email": "Письмо", "message": "Сообщение", "notice": "Системное уведомление"}

# --------------------------------------------------------------------------- #
# Safe text rendering
# --------------------------------------------------------------------------- #

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~$+\-!{}])")
_LINKISH = re.compile(
    r"(https?://\S+|www\.\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\b[\w-]+(?:\.[\w-]+)*\.(?:example|test|invalid)\b)"
)


def _escape_md(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def md_text(text: str) -> str:
    """Escape markdown and render addresses/URLs as inert inline code (never as links)."""
    parts: list[str] = []
    pos = 0
    for match in _LINKISH.finditer(text):
        parts.append(_escape_md(text[pos : match.start()]))
        parts.append("`" + match.group(0).replace("`", "") + "`")
        pos = match.end()
    parts.append(_escape_md(text[pos:]))
    return "".join(parts).replace("\n", "  \n")


# --------------------------------------------------------------------------- #
# Resources and session state
# --------------------------------------------------------------------------- #

@st.cache_resource
def get_content() -> Content:
    content = load_content()
    world = load_world()
    errors = validate_world(world, set(content.missions))
    if errors:
        raise ContentError("Ошибки карты мира:\n" + "\n".join(errors))
    return content


@st.cache_resource
def get_world():
    return load_world()


@st.cache_resource
def ensure_db() -> bool:
    db.init_db(DB_PATH)
    return True


def init_session() -> None:
    st.session_state.setdefault("attempt", None)
    st.session_state.setdefault("result_attempt_id", None)
    st.session_state.setdefault("demo", False)
    st.session_state.setdefault("world_spawn", None)
    st.session_state.setdefault("last_interact_sig", None)


def active_attempt() -> AttemptState | None:
    return st.session_state.get("attempt")


def is_demo() -> bool:
    return bool(st.session_state.get("demo"))


def sync_demo_toggle() -> None:
    st.session_state.demo = bool(st.session_state.get("demo_toggle"))
    st.session_state.result_attempt_id = None


# --------------------------------------------------------------------------- #
# Actions (callbacks run before the rerun, so each click is handled once)
# --------------------------------------------------------------------------- #

def action_start(mission_id: str, demo: bool) -> None:
    content = get_content()
    mission = content.mission(mission_id)
    previous = db.find_in_progress(DB_PATH, mission_id, demo)
    if previous is not None:
        db.finish_attempt(DB_PATH, previous.id, db.STATUS_STOPPED)
    attempt_id = db.create_attempt(DB_PATH, mission.id, content.version, mission.start_scene, demo)
    st.session_state.attempt = start_attempt(mission, attempt_id)
    st.session_state.demo = demo
    st.session_state.result_attempt_id = None
    st.session_state.world_spawn = get_world().spawn_near(mission_id)


def action_resume(attempt_id: int) -> None:
    summary = db.get_attempt(DB_PATH, attempt_id)
    if summary is None or summary.status != db.STATUS_IN_PROGRESS:
        return
    mission = get_content().mission(summary.mission_id)
    records = [
        DecisionRecord(d.scene_id, d.option_id, d.is_correct, d.points) for d in db.get_decisions(DB_PATH, attempt_id)
    ]
    st.session_state.attempt = restore_attempt(mission, attempt_id, summary.visited, records, finished=False)
    st.session_state.demo = summary.is_demo
    st.session_state.result_attempt_id = None


def action_submit(choice_key: str) -> None:
    state = active_attempt()
    option_id = st.session_state.get(choice_key)
    if state is None or option_id is None:
        return
    mission = get_content().mission(state.mission_id)
    result = submit_answer(state, mission, option_id, POINTS_PREFERRED)
    if result.newly_recorded and state.attempt_id is not None:
        db.record_decision(
            DB_PATH,
            state.attempt_id,
            result.record.scene_id,
            result.record.option_id,
            result.record.is_correct,
            result.record.points,
        )


def action_continue() -> None:
    state = active_attempt()
    if state is None or state.finished:
        return
    mission = get_content().mission(state.mission_id)
    scene = current_scene(state, mission)
    if scene is None or (scene.is_decision and scene.id not in state.decisions):
        return
    advance(state, mission)
    if state.attempt_id is None:
        return
    if state.finished:
        db.finish_attempt(DB_PATH, state.attempt_id, db.STATUS_COMPLETED)
        st.session_state.result_attempt_id = state.attempt_id
        st.session_state.attempt = None
    else:
        db.update_visited(DB_PATH, state.attempt_id, state.visited)


def action_stop() -> None:
    state = active_attempt()
    if state is None or state.attempt_id is None:
        return
    db.finish_attempt(DB_PATH, state.attempt_id, db.STATUS_STOPPED)
    st.session_state.result_attempt_id = state.attempt_id
    st.session_state.attempt = None


def action_to_list() -> None:
    st.session_state.result_attempt_id = None
    if st.session_state.get("world_spawn") is None:
        st.session_state.world_spawn = get_world().spawn


def action_delete_demo() -> None:
    state = active_attempt()
    if state is not None and is_demo():
        st.session_state.attempt = None
    st.session_state.result_attempt_id = None
    db.delete_demo_attempts(DB_PATH)


# --------------------------------------------------------------------------- #
# Shared UI pieces
# --------------------------------------------------------------------------- #

def render_reference(ref: Reference | None) -> None:
    if ref is None:
        return
    status = "" if ref.verified else " (не проверено)"
    st.caption(f"Источник: {_escape_md(ref.title)}{status} — `{ref.url}`")


def render_artifact(artifact: Artifact) -> None:
    with st.container(border=True):
        st.caption(f"{ARTIFACT_LABELS.get(artifact.kind, 'Материал')} · учебная имитация")
        for label, value in artifact.fields:
            st.markdown(f"**{_escape_md(label)}:** {md_text(value)}")
        if artifact.body:
            st.divider()
            st.markdown(md_text(artifact.body))
        if artifact.url:
            st.caption("Адрес ссылки (показан как текст, ссылка неактивна):")
            st.code(artifact.url, language=None, wrap_lines=True)


def render_demo_hint(scene: Scene, mission: Mission) -> None:
    with st.expander("Подсказка ведущему (только в демонстрационном режиме)"):
        for option in scene.options:
            notes = []
            if option.preferred:
                notes.append("предпочтительный ответ")
            target = mission.scenes.get(option.next) if option.next else None
            if target is not None and target.type == "recovery":
                notes.append("ведёт к сцене восстановления")
            elif target is not None and target.type == "verification":
                notes.append("ведёт к сцене проверки")
            elif option.next is None:
                notes.append("завершает миссию")
            st.markdown(f"- **{option.id.upper()}.** {md_text(option.text)} — _{', '.join(notes) or 'риск'}_")


# --------------------------------------------------------------------------- #
# Mission play
# --------------------------------------------------------------------------- #

def render_decision(state: AttemptState, mission: Mission, scene: Scene, content: Content) -> None:
    st.markdown("**Ситуация**")
    st.markdown(md_text(scene.situation))
    if scene.artifact:
        render_artifact(scene.artifact)
    if is_demo():
        render_demo_hint(scene, mission)

    record = state.decisions.get(scene.id)
    option_ids = [o.id for o in scene.options]
    labels = {o.id: o.text for o in scene.options}
    choice_key = f"choice_{state.attempt_id}_{scene.id}"
    if record is None:
        choice = st.radio(
            "Ваше решение",
            option_ids,
            index=None,
            format_func=lambda oid: labels[oid],
            key=choice_key,
        )
    else:
        choice = st.radio(
            "Ваше решение (зафиксировано)",
            option_ids,
            index=option_ids.index(record.option_id),
            format_func=lambda oid: labels[oid],
            key=f"locked_{choice_key}",
            disabled=True,
        )

    if record is None:
        st.button(
            "Ответить",
            type="primary",
            disabled=choice is None,
            on_click=action_submit,
            args=(choice_key,),
            key=f"submit_{state.attempt_id}_{scene.id}",
        )
        if choice is None:
            st.caption("Выберите один вариант, затем нажмите «Ответить». Ответ фиксируется после отправки.")
        return

    chosen = scene.option(record.option_id)
    with st.container(border=True):
        if record.is_correct:
            st.success(f"Предпочтительное решение · +{record.points} баллов")
        else:
            st.warning("Это решение не является предпочтительным · 0 баллов")
        st.markdown(f"**Последствие (симуляция):** {md_text(chosen.consequence)}")
        if not record.is_correct:
            st.markdown(f"**Предпочтительное действие:** {md_text(scene.preferred_option.text)}")
        st.markdown(f"**Объяснение:** {md_text(scene.explanation)}")
        st.markdown(f"**Рекомендация:** {md_text(scene.recommendation)}")
        render_reference(content.references.get(scene.reference_id))
    st.button("Продолжить", type="primary", on_click=action_continue, key=f"continue_{state.attempt_id}_{scene.id}")


def render_branch_scene(state: AttemptState, scene: Scene, content: Content) -> None:
    if scene.type == "recovery":
        st.badge("Сцена восстановления · без оценки", color="orange")
    else:
        st.badge("Сцена проверки · без оценки", color="green")
    st.markdown(f"**{md_text(scene.title)}**")
    st.markdown(md_text(scene.situation))
    if scene.artifact:
        render_artifact(scene.artifact)
    if scene.steps:
        st.markdown("**Что важно сделать**")
        st.markdown("\n".join(f"{i}. {md_text(step)}" for i, step in enumerate(scene.steps, start=1)))
    st.markdown(f"**Рекомендация:** {md_text(scene.recommendation)}")
    render_reference(content.references.get(scene.reference_id))
    st.button("Продолжить", type="primary", on_click=action_continue, key=f"continue_{state.attempt_id}_{scene.id}")


def render_play(state: AttemptState, content: Content) -> None:
    mission = content.mission(state.mission_id)
    scene = current_scene(state, mission)
    if scene is None:
        st.session_state.attempt = None
        st.rerun()
        return

    if is_demo():
        st.info("Демонстрационный режим: эта попытка не учитывается в статистике учащегося.")
    st.subheader(mission.title)
    number, total = decision_stage(state, mission)
    if scene.is_decision:
        st.caption(f"Этап: решение {number} из {total}")
    else:
        st.caption(f"Этап: разбор после решения {number} из {total}")
    st.progress(min(len(state.decisions) / total, 1.0) if total else 0.0)

    with st.container(border=True):
        if scene.is_decision:
            render_decision(state, mission, scene, content)
        else:
            render_branch_scene(state, scene, content)

    st.button(
        "Завершить досрочно",
        on_click=action_stop,
        key=f"stop_{state.attempt_id}",
        help="Попытка будет сохранена как незавершённая. Учитываются только уже принятые решения.",
    )


# --------------------------------------------------------------------------- #
# Results and review
# --------------------------------------------------------------------------- #

def render_attempt_review(attempt_id: int, content: Content, key_prefix: str) -> None:
    summary = db.get_attempt(DB_PATH, attempt_id)
    if summary is None:
        st.warning("Попытка не найдена.")
        return
    details = decision_details(content, summary.mission_id, db.get_decisions(DB_PATH, attempt_id))
    score = summary.score

    badges = st.container(horizontal=True)
    with badges:
        st.badge(summary.status_label, color="green" if summary.is_completed else "orange")
        if summary.is_demo:
            st.badge("Демонстрация", color="gray")
        st.badge(f"Версия содержимого {summary.content_version}", color="gray")
    st.caption(f"Попытка № {summary.id} · начата {format_timestamp(summary.started_at)}")

    cols = st.columns(3)
    cols[0].metric("Баллы", f"{score.earned} / {score.maximum}")
    cols[1].metric("Верные решения", f"{score.correct} из {score.scored}")
    cols[2].metric("Результат", f"{score.percent:g}%")
    st.caption(
        "Максимум считается только по решениям, которые были в этой попытке "
        f"(по {POINTS_PREFERRED} баллов за предпочтительный ответ). {SCORE_DISCLAIMER}"
    )

    history = db.list_attempts(DB_PATH, demo=summary.is_demo, mission_id=summary.mission_id)
    latest, best = db.latest_and_best(history)
    if latest and best:
        line = f"Последняя завершённая попытка: № {latest.id} — {format_score(latest.score)}."
        if best.id != latest.id:
            line += f" Лучшая: № {best.id} — {format_score(best.score)}."
        else:
            line += " Она же лучшая."
        st.caption(line)

    if not details:
        st.info("В этой попытке не было принято ни одного решения.")
        return

    mistakes = [d for d in details if not d.is_correct]
    if mistakes:
        st.markdown("**Решения, требующие повторения**")
        for d in mistakes:
            with st.container(border=True):
                st.markdown(md_text(d.situation))
                st.markdown(f"**Ваш ответ:** {md_text(d.chosen_text)}")
                if d.consequence:
                    st.markdown(f"**Последствие:** {md_text(d.consequence)}")
                st.markdown(f"**Предпочтительное действие:** {md_text(d.preferred_text)}")
                if d.explanation:
                    st.markdown(f"**Объяснение:** {md_text(d.explanation)}")
                if d.recommendation:
                    st.markdown(f"**Рекомендация:** {md_text(d.recommendation)}")
                render_reference(d.reference)
    else:
        st.success("Во всех решениях этой попытки выбраны предпочтительные действия.")

    correct = [d for d in details if d.is_correct]
    if correct:
        with st.expander(f"Верные решения ({len(correct)})"):
            for d in correct:
                st.markdown(f"- {md_text(d.chosen_text)}  \n  :gray[{md_text(d.explanation)}]")

    report = build_html_report([(summary, details)], content, title=f"Отчёт CyberQuest — попытка № {summary.id}")
    st.download_button(
        "Скачать отчёт об этой попытке (HTML)",
        data=report.encode("utf-8"),
        file_name=f"cyberquest_attempt_{summary.id}.html",
        mime="text/html",
        key=f"{key_prefix}_html_{summary.id}",
    )


def render_result(attempt_id: int, content: Content) -> None:
    summary = db.get_attempt(DB_PATH, attempt_id)
    if summary is None:
        st.session_state.result_attempt_id = None
        st.rerun()
        return
    st.subheader(f"Итоги миссии «{mission_title(content, summary.mission_id)}»")
    if summary.is_demo:
        st.info("Демонстрационная попытка: результат не входит в статистику учащегося.")
    render_attempt_review(attempt_id, content, key_prefix="result")
    actions = st.container(horizontal=True)
    with actions:
        st.button(
            "Пройти миссию снова",
            type="primary",
            on_click=action_start,
            args=(summary.mission_id, summary.is_demo),
            key=f"replay_{attempt_id}",
        )
        st.button("Вернуться на карту", on_click=action_to_list, key=f"tolist_{attempt_id}")


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #

def mission_progress_maps(demo: bool) -> tuple[list[str], dict[str, float]]:
    completed: list[str] = []
    best_scores: dict[str, float] = {}
    for mission_id in get_content().missions:
        _latest, best = db.latest_and_best(db.list_attempts(DB_PATH, demo=demo, mission_id=mission_id))
        if best is not None:
            completed.append(mission_id)
            best_scores[mission_id] = best.score.percent
    return completed, best_scores


def handle_world_interact(result: object) -> None:
    """Start a mission when the player interacts with a terminal in the 2D world."""
    if active_attempt() is not None or st.session_state.result_attempt_id is not None:
        return
    payload = getattr(result, "interact", None)
    if not payload:
        return
    if isinstance(payload, str):
        try:
            import json as _json

            payload = _json.loads(payload)
        except (TypeError, ValueError):
            return
    if not isinstance(payload, dict):
        return
    mission_id = payload.get("mission_id")
    if not mission_id or mission_id not in get_content().missions:
        return
    signature = f"{mission_id}:{payload.get('t')}:{payload.get('terminal')}"
    if st.session_state.get("last_interact_sig") == signature:
        return
    st.session_state.last_interact_sig = signature
    if payload.get("x") is not None and payload.get("y") is not None:
        st.session_state.world_spawn = (float(payload["x"]), float(payload["y"]))
    existing = db.find_in_progress(DB_PATH, mission_id, is_demo())
    if existing is not None:
        action_resume(existing.id)
    else:
        action_start(mission_id, is_demo())
    st.rerun()


def render_world_panel(*, locked: bool) -> None:
    world = get_world()
    completed, best_scores = mission_progress_maps(is_demo())
    spawn = st.session_state.get("world_spawn") or world.spawn
    result = render_world(
        world,
        completed=completed,
        best_scores=best_scores,
        spawn=spawn,
        locked=locked,
        key="cq_world_main",
    )
    if not locked:
        handle_world_interact(result)


def render_demo_controls() -> None:
    st.toggle(
        "Демонстрационный режим",
        value=is_demo(),
        key="demo_toggle",
        on_change=sync_demo_toggle,
        help="Попытки в этом режиме помечаются как демонстрационные и не входят в статистику учащегося.",
    )
    if is_demo():
        with st.container(border=True):
            st.markdown(
                "**Демонстрационный режим включён.** Новые попытки не повлияют на прогресс учащегося. "
                "Во время миссии доступна подсказка ведущему: какой ответ предпочтительный и какой ведёт "
                "к сцене восстановления."
            )
            st.button("Удалить демонстрационные попытки", on_click=action_delete_demo, key="delete_demo")


def render_mission_fallback_list(content: Content) -> None:
    """Accessible fallback if the canvas is inconvenient during a talk."""
    demo = is_demo()
    columns = st.columns(2)
    for index, mission in enumerate(content.missions.values()):
        if index and index % 2 == 0:
            columns = st.columns(2)
        attempts = db.list_attempts(DB_PATH, demo=demo, mission_id=mission.id)
        latest, best = db.latest_and_best(attempts)
        in_progress = next((a for a in reversed(attempts) if a.status == db.STATUS_IN_PROGRESS), None)
        with columns[index % 2].container(border=True, height="stretch"):
            st.caption(f"Терминал {index + 1}")
            st.markdown(f"**{_escape_md(mission.title)}**")
            st.markdown(f":gray[{_escape_md(mission.summary)}]")
            if latest and best:
                st.caption(
                    f"Последний результат: {latest.score.percent:g}% · Лучший: {best.score.percent:g}%"
                )
            else:
                st.caption("Ещё не пройдена" + (" в демонстрационном режиме" if demo else ""))
            buttons = st.container(horizontal=True)
            with buttons:
                if in_progress is not None:
                    st.button(
                        "Продолжить попытку",
                        type="primary",
                        on_click=action_resume,
                        args=(in_progress.id,),
                        key=f"resume_{mission.id}_{demo}",
                    )
                    st.button(
                        "Начать заново",
                        on_click=action_start,
                        args=(mission.id, demo),
                        key=f"restart_{mission.id}_{demo}",
                    )
                else:
                    st.button(
                        "Пройти снова" if latest else "Открыть терминал",
                        type="primary",
                        on_click=action_start,
                        args=(mission.id, demo),
                        key=f"start_{mission.id}_{demo}",
                    )


def render_hub(content: Content) -> None:
    render_demo_controls()
    st.caption(
        "2D-карта учебного центра: дойдите до терминала (WASD или клик), затем нажмите E "
        "или кнопку «Взаимодействовать». Все ситуации вымышлены; ссылки неактивны."
    )
    render_world_panel(locked=False)
    with st.expander("Список терминалов без карты"):
        render_mission_fallback_list(content)


def render_missions_tab(content: Content) -> None:
    state = active_attempt()
    if state is not None and not state.finished:
        render_demo_controls()
        st.caption("Персонаж у терминала. Ответы принимаются в панели ниже.")
        render_world_panel(locked=True)
        render_play(state, content)
    elif st.session_state.result_attempt_id is not None:
        render_result(st.session_state.result_attempt_id, content)
    else:
        render_hub(content)


def render_progress_tab(content: Content) -> None:
    attempts = db.list_attempts(DB_PATH, demo=False)
    stats = db.progress_stats(attempts)

    cols = st.columns(4)
    cols[0].metric("Завершённые миссии", f"{stats.completed_missions} из {len(content.missions)}")
    cols[1].metric("Завершённые попытки", stats.completed_attempts)
    cols[2].metric("Оценённые решения", stats.scored_decisions)
    cols[3].metric("Общая точность", f"{stats.accuracy:g}%" if stats.scored_decisions else "—")
    st.caption(
        "Все значения рассчитаны по записанным решениям учащегося, включая незавершённые попытки. "
        "Повторные прохождения одной миссии считаются одной завершённой миссией. "
        "Демонстрационные попытки не учитываются. " + SCORE_DISCLAIMER
    )

    st.markdown("**Достижения**")
    earned = achievements.evaluate(attempts, content.missions.keys())
    ach_cols = st.columns(len(earned))
    for col, ach in zip(ach_cols, earned):
        with col.container(border=True):
            st.badge(ach.name, color="green" if ach.earned else "gray")
            st.caption(ach.description)
            st.caption("Получено" if ach.earned else "Пока не получено")

    if not attempts:
        st.info("История пока пуста. Пройдите любую миссию на вкладке «Миссии», и здесь появятся результаты.")
    else:
        st.markdown("**Результаты по миссиям**")
        rows = []
        for mission in content.missions.values():
            mission_attempts = [a for a in attempts if a.mission_id == mission.id]
            latest, best = db.latest_and_best(mission_attempts)
            rows.append(
                {
                    "Миссия": mission.title,
                    "Завершённых попыток": sum(a.is_completed for a in mission_attempts),
                    "Последний результат": format_score(latest.score) if latest else "—",
                    "Лучший результат": format_score(best.score) if best else "—",
                }
            )
        st.dataframe(rows, hide_index=True, width="stretch")

        st.markdown("**История попыток**")
        ordered = list(reversed(attempts))
        history_rows = [
            {
                "№": a.id,
                "Миссия": mission_title(content, a.mission_id),
                "Дата": format_timestamp(a.started_at),
                "Статус": a.status_label,
                "Результат": format_score(a.score),
                "Версия": a.content_version,
            }
            for a in ordered
        ]
        st.dataframe(history_rows, hide_index=True, width="stretch")

        selected = st.selectbox(
            "Разбор попытки",
            [a.id for a in ordered],
            format_func=lambda aid: next(
                f"№ {a.id} · {mission_title(content, a.mission_id)} · {format_timestamp(a.started_at)}"
                for a in ordered
                if a.id == aid
            ),
            key="review_select",
        )
        if selected is not None:
            with st.container(border=True):
                render_attempt_review(selected, content, key_prefix="progress")

        st.markdown("**Экспорт**")
        report_items = [
            (a, decision_details(content, a.mission_id, db.get_decisions(DB_PATH, a.id))) for a in attempts
        ]
        export = st.container(horizontal=True)
        with export:
            st.download_button(
                "Скачать сводку попыток (CSV)",
                data=attempts_to_csv(attempts, content),
                file_name="cyberquest_attempts.csv",
                mime="text/csv",
                key="export_csv",
            )
            st.download_button(
                "Скачать полный отчёт (HTML)",
                data=build_html_report(report_items, content).encode("utf-8"),
                file_name="cyberquest_report.html",
                mime="text/html",
                key="export_html",
            )

    demo_attempts = db.list_attempts(DB_PATH, demo=True)
    if demo_attempts:
        with st.expander(f"Демонстрационные попытки ({len(demo_attempts)}) — не входят в статистику"):
            st.dataframe(
                [
                    {
                        "№": a.id,
                        "Миссия": mission_title(content, a.mission_id),
                        "Дата": format_timestamp(a.started_at),
                        "Статус": a.status_label,
                        "Результат": format_score(a.score),
                    }
                    for a in reversed(demo_attempts)
                ],
                hide_index=True,
                width="stretch",
            )


def render_about_tab(content: Content) -> None:
    st.markdown(
        """
**Назначение.** CyberQuest — учебная 2D-игра, которая помогает отрабатывать безопасные действия
в типичных цифровых ситуациях: фишинг, защита аккаунтов, вложения, обновления, резервное копирование
и работа с данными.

**Аудитория.** Учащиеся и студенты; специальная подготовка не требуется.

**Подход.** Игрок перемещается по карте учебного центра, подходит к терминалам миссий и проходит
ситуационные задания: читает вымышленную ситуацию (письмо, сообщение, системное уведомление),
принимает решение, видит смоделированное последствие и получает объяснение с практической
рекомендацией и ссылкой на образовательный источник.

**Ветвление.** В каждой миссии есть развилка. Рискованное решение ведёт к сцене восстановления, где
разбираются шаги по устранению последствий. Предпочтительное решение ведёт к сцене проверки или
предотвращения. Затем ветви сходятся, и миссия продолжается. Ошибка не прерывает игру.

**Правила оценки.** Предпочтительный ответ — 10 баллов, любой другой — 0 баллов, штрафов нет.
Максимум считается только по решениям, которые встретились в попытке. Сцены восстановления и проверки
не оцениваются, поэтому ветвление не меняет число оцениваемых решений. Скорость ответа не учитывается.
Результат — показатель в этой игре, а не профессиональная квалификация.

**Технологии.** Python, Streamlit (включая canvas-компонент 2D-мира), SQLite, стандартная библиотека
Python, pytest. Содержимое миссий и карта мира хранятся в читаемом JSON; игровой движок отделён от
интерфейса. Работает локально, без сети и внешних сервисов.

**Практическая польза.** Безопасная отработка решений без реальных рисков; разбор ошибок с объяснениями;
история попыток, отчёты в CSV и HTML для преподавателя.

**Ограничения.** Сценарии упрощены и вымышлены; в реальных ситуациях бывает больше контекста. Выбор из
готовых вариантов проще, чем самостоятельное распознавание угрозы. Результаты в игре не доказывают
изменения реального поведения. Один локальный профиль без регистрации.
"""
    )
    with st.container(border=True):
        st.markdown("**Исследовательская гипотеза**")
        st.markdown(
            "«Ситуационные задания с объяснением последствий могут помочь учащимся выбирать более "
            "безопасные действия в типичных цифровых ситуациях»."
        )
        st.caption(
            "Гипотеза пока не проверялась. План возможного исследования описан в README. "
            "Повторные результаты в миссиях сами по себе не доказывают улучшения реального поведения."
        )
    st.markdown("**Образовательные источники**")
    for ref in content.references.values():
        status = "доступность проверена 12.10.2026" if ref.verified else "не проверено"
        st.markdown(f"- {_escape_md(ref.title)} — `{ref.url}` :gray[({status})]")
    st.caption(f"Версия содержимого миссий: {content.version}. Все персонажи, организации и адреса вымышлены.")


def main() -> None:
    st.set_page_config(page_title="CyberQuest", layout="wide")
    init_session()
    try:
        content = get_content()
        ensure_db()
    except ContentError as exc:
        st.error(f"Не удалось загрузить миссии: {exc}")
        st.stop()
    except Exception as exc:  # noqa: BLE001 - show a readable message instead of a traceback
        st.error(f"Не удалось подготовить базу данных: {exc}")
        st.stop()

    outer = st.container(horizontal_alignment="center")
    page = outer.container(width=CONTENT_WIDTH)
    with page:
        st.title("CyberQuest")
        st.markdown(":gray[Учись кибербезопасности через практические ситуации]")
        missions_tab, progress_tab, about_tab = st.tabs(["Миссии", "Прогресс", "О проекте"])
        with missions_tab:
            render_missions_tab(content)
        with progress_tab:
            render_progress_tab(content)
        with about_tab:
            render_about_tab(content)


main()
