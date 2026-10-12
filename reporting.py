"""CSV and standalone HTML exports. Every displayed value is escaped."""

from __future__ import annotations

import csv
import html
import io
from dataclasses import dataclass
from datetime import datetime

from database import AttemptSummary, DecisionRow
from game_engine import Content, Reference

CSV_HEADERS = [
    "ID попытки",
    "Миссия",
    "Версия содержимого",
    "Начало",
    "Окончание",
    "Статус",
    "Баллы",
    "Максимум",
    "Верных решений",
    "Оценённых решений",
    "Процент",
]

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class DecisionDetail:
    scene_id: str
    situation: str
    chosen_text: str
    preferred_text: str
    consequence: str
    is_correct: bool
    points: int
    explanation: str
    recommendation: str
    reference: Reference | None


def format_timestamp(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def mission_title(content: Content, mission_id: str) -> str:
    mission = content.missions.get(mission_id)
    return mission.title if mission else f"Неизвестная миссия ({mission_id})"


def decision_details(content: Content, mission_id: str, decisions: list[DecisionRow]) -> list[DecisionDetail]:
    """Join stored decisions with mission content. Unknown IDs are kept, not dropped."""
    mission = content.missions.get(mission_id)
    details: list[DecisionDetail] = []
    for row in decisions:
        scene = mission.scenes.get(row.scene_id) if mission else None
        if scene is None or not scene.is_decision:
            details.append(
                DecisionDetail(
                    scene_id=row.scene_id,
                    situation=f"Сцена {row.scene_id} отсутствует в текущей версии содержимого.",
                    chosen_text=f"Вариант {row.option_id}",
                    preferred_text="—",
                    consequence="",
                    is_correct=row.is_correct,
                    points=row.points,
                    explanation="",
                    recommendation="",
                    reference=None,
                )
            )
            continue
        chosen = next((o for o in scene.options if o.id == row.option_id), None)
        details.append(
            DecisionDetail(
                scene_id=scene.id,
                situation=scene.situation,
                chosen_text=chosen.text if chosen else f"Вариант {row.option_id}",
                preferred_text=scene.preferred_option.text,
                consequence=chosen.consequence if chosen else "",
                is_correct=row.is_correct,
                points=row.points,
                explanation=scene.explanation,
                recommendation=scene.recommendation,
                reference=content.references.get(scene.reference_id),
            )
        )
    return details


def _csv_safe(value: object) -> str:
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def attempts_to_csv(attempts: list[AttemptSummary], content: Content) -> bytes:
    """CSV encoded as UTF-8 with BOM so spreadsheet apps read Cyrillic correctly."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(CSV_HEADERS)
    for a in attempts:
        score = a.score
        writer.writerow(
            [
                _csv_safe(v)
                for v in (
                    a.id,
                    mission_title(content, a.mission_id),
                    a.content_version,
                    format_timestamp(a.started_at),
                    format_timestamp(a.finished_at),
                    a.status_label,
                    score.earned,
                    score.maximum,
                    score.correct,
                    score.scored,
                    f"{score.percent:g}",
                )
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


_REPORT_CSS = """
body{background:#10151D;color:#E8EDF4;font-family:"Segoe UI",Arial,sans-serif;line-height:1.5;margin:0}
main{max-width:1000px;margin:0 auto;padding:32px 24px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:20px;margin:32px 0 8px}
.muted{color:#A2ADBD}.panel{background:#18212D;border:1px solid #2B3645;border-radius:6px;padding:16px;margin:12px 0}
.ok{color:#4BAE7C}.bad{color:#E46B6B}.warn{color:#D7A64B}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #2B3645;padding:6px 8px;text-align:left;vertical-align:top}
th{color:#A2ADBD;font-weight:600}code{color:#A2ADBD}
@media print{body{background:#fff;color:#000}.panel{background:#fff}.muted,th,code{color:#444}}
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _attempt_section(attempt: AttemptSummary, details: list[DecisionDetail], content: Content) -> str:
    score = attempt.score
    status_class = "ok" if attempt.is_completed else "warn"
    parts = [
        f"<h2>{_e(mission_title(content, attempt.mission_id))}</h2>",
        '<div class="panel"><table>',
        f"<tr><th>Попытка</th><td>№ {_e(attempt.id)}{' (демонстрация)' if attempt.is_demo else ''}</td></tr>",
        f"<tr><th>Дата</th><td>{_e(format_timestamp(attempt.started_at))}</td></tr>",
        f'<tr><th>Статус</th><td class="{status_class}">{_e(attempt.status_label)}</td></tr>',
        f"<tr><th>Результат</th><td>{_e(score.earned)} из {_e(score.maximum)} баллов "
        f"({_e(f'{score.percent:g}')}%), верных решений: {_e(score.correct)} из {_e(score.scored)}</td></tr>",
        f"<tr><th>Версия содержимого</th><td>{_e(attempt.content_version)}</td></tr>",
        "</table></div>",
    ]
    if not details:
        parts.append('<p class="muted">В этой попытке нет записанных решений.</p>')
    for i, d in enumerate(details, start=1):
        verdict = '<span class="ok">Предпочтительный ответ</span>' if d.is_correct else '<span class="bad">Требует повторения</span>'
        ref = (
            f"{_e(d.reference.title)} — <code>{_e(d.reference.url)}</code>"
            if d.reference
            else "—"
        )
        parts.append(
            '<div class="panel">'
            f"<p><strong>Решение {i}.</strong> {verdict} · {_e(d.points)} балл.</p>"
            f'<p class="muted">{_e(d.situation)}</p>'
            f"<p><strong>Выбранный ответ:</strong> {_e(d.chosen_text)}</p>"
            + (f"<p><strong>Последствие:</strong> {_e(d.consequence)}</p>" if d.consequence else "")
            + (f"<p><strong>Предпочтительное действие:</strong> {_e(d.preferred_text)}</p>" if not d.is_correct else "")
            + (f"<p><strong>Объяснение:</strong> {_e(d.explanation)}</p>" if d.explanation else "")
            + (f"<p><strong>Рекомендация:</strong> {_e(d.recommendation)}</p>" if d.recommendation else "")
            + f'<p class="muted">Источник: {ref}</p>'
            "</div>"
        )
    return "\n".join(parts)


def build_html_report(
    attempts: list[tuple[AttemptSummary, list[DecisionDetail]]],
    content: Content,
    title: str = "Отчёт об обучении CyberQuest",
) -> str:
    """A self-contained HTML document (inline CSS, no scripts, no external assets)."""
    used_refs: dict[str, Reference] = {}
    for _, details in attempts:
        for d in details:
            if d.reference:
                used_refs[d.reference.id] = d.reference
    sections = [_attempt_section(a, d, content) for a, d in attempts]
    unverified = ' — <span class="warn">не проверено</span>'
    refs_html = "".join(
        f"<li>{_e(r.title)} ({_e(r.publisher)}): <code>{_e(r.url)}</code>"
        f"{'' if r.verified else unverified}</li>"
        for r in used_refs.values()
    )
    body = "\n".join(sections) if sections else '<p class="muted">Нет попыток для отчёта.</p>'
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><style>{_REPORT_CSS}</style></head>
<body><main>
<h1>{_e(title)}</h1>
<p class="muted">Сформирован {_e(datetime.now().strftime('%d.%m.%Y %H:%M'))}. Версия содержимого: {_e(content.version)}.</p>
<p class="muted">Баллы отражают результат в этой учебной игре и не являются профессиональной квалификацией в области кибербезопасности. Все ситуации вымышлены.</p>
{body}
<h2>Образовательные источники</h2>
<ul>{refs_html or '<li class="muted">—</li>'}</ul>
</main></body></html>
"""
