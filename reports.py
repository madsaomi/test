"""Периодические отчеты для администраторов: месячные и годовой.

Статистика собирается из test_results. created_at в БД хранится в UTC
(SQLite CURRENT_TIMESTAMP), поэтому границы периода считаем по Ташкенту
(UTC+5, без перехода на летнее время) и фильтруем в UTC.
"""
from datetime import datetime, timedelta, timezone

import database

TZ_TASHKENT = timezone(timedelta(hours=5))

LEVEL_ORDER = [
    "Beginner",
    "Elementary",
    "Pre-Intermediate",
    "Intermediate",
    "Upper-Intermediate",
    "Upper-Intermediate+",
]

MONTH_SHORT = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]

MONTH_FULL = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _period_bounds_utc(year, month=None):
    """Границы периода [начало, конец) в UTC по календарю Ташкента."""
    tz = TZ_TASHKENT
    if month is not None:
        start_local = datetime(year, month, 1, tzinfo=tz)
        end_local = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=tz)
    else:
        start_local = datetime(year, 1, 1, tzinfo=tz)
        end_local = datetime(year + 1, 1, 1, tzinfo=tz)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start_local.astimezone(timezone.utc).strftime(fmt), end_local.astimezone(timezone.utc).strftime(fmt)


def _load_rows(year, month=None):
    start, end = _period_bounds_utc(year, month)
    rows = database.get_results_in_range(start, end)
    # created_at строкой в UTC -> приводим к Ташкенту для точной фильтрации
    out = []
    for r in rows:
        dt_utc = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        r = dict(r)
        r["local_dt"] = dt_utc.astimezone(TZ_TASHKENT)
        out.append(r)
    return out


def _aggregate(rows):
    """Сводные цифры по списку результатов"""
    n = len(rows)
    if n:
        avg_score = sum(r["score"] for r in rows) / n
        avg_pct = sum(r["percentage"] for r in rows) / n
        best = max(rows, key=lambda r: r["score"])
        flags = sum(1 for r in rows if (r["flags"] or 0) > 0)
        levels = {lv: 0 for lv in LEVEL_ORDER}
        branches = {}
        for r in rows:
            levels[r["level"]] = levels.get(r["level"], 0) + 1
            b = r["branch"] or "—"
            branches[b] = branches.get(b, 0) + 1
    else:
        avg_score = avg_pct = 0
        best = None
        flags = 0
        levels = {lv: 0 for lv in LEVEL_ORDER}
        branches = {}
    return {
        "n": n,
        "unique_phones": len({r["phone"] for r in rows}),
        "avg_score": avg_score,
        "avg_pct": avg_pct,
        "best": best,
        "flags": flags,
        "levels": levels,
        "branches": branches,
    }


def _levels_line(levels):
    non_zero = [f"{lv} — {cnt}" for lv, cnt in levels.items() if cnt]
    return "\n".join(f"  • {line}" for line in non_zero) if non_zero else "  • нет данных"


def build_report(year, month=None):
    """Текст отчета в Telegram HTML. month=None — годовой отчет за год."""
    rows = _load_rows(year, month)
    agg = _aggregate(rows)
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if month is not None:
        parts = [f"📊 <b>Отчет за {MONTH_FULL[month - 1]} {year} года</b>", ""]
    else:
        parts = [f"📈 <b>Итоги {year} года</b>", ""]

    parts.append(f"📝 <b>Тестов:</b> {agg['n']} (уникальных номеров: {agg['unique_phones']})")
    if agg["n"]:
        parts.append(f"🎯 <b>Средний балл:</b> {agg['avg_score']:.1f} / 50 ({agg['avg_pct']:.0f}%)")
        best = agg["best"]
        if best:
            parts.append(f"🏆 <b>Лучший:</b> {best['score']}/50 — {esc(best['fio'])}")
    parts.append("🎓 <b>Уровни:</b>")
    parts.append(_levels_line(agg["levels"]))
    if agg["branches"]:
        br = " · ".join(f"{esc(k)}: {v}" for k, v in sorted(agg["branches"].items(), key=lambda kv: -kv[1]))
        parts.append(f"🏢 <b>Филиалы:</b> {br}")
    parts.append(f"🚩 <b>Подозрительных:</b> {agg['flags']}")

    # Годовой: разбивка по месяцам
    if month is None:
        parts.append("")
        parts.append("📅 <b>По месяцам:</b>")
        lines = []
        for m in range(1, 13):
            m_rows = [r for r in rows if r["local_dt"].month == m]
            if m_rows:
                a = sum(r["score"] for r in m_rows) / len(m_rows)
                lines.append(f"{MONTH_SHORT[m - 1]}  {len(m_rows):3d}  ср. {a:.1f}")
            else:
                lines.append(f"{MONTH_SHORT[m - 1]}    0      —")
        parts.append("<code>" + "\n".join(lines) + "</code>")

    parts.append("")
    stamp = datetime.now(TZ_TASHKENT).strftime("%d.%m.%Y %H:%M")
    parts.append(f"🕐 Ташкент, {stamp}")
    return "\n".join(parts)
