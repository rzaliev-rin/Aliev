# -*- coding: utf-8 -*-
"""
Вывод плана: Markdown-документ, CSV по разделам и (по желанию) Google
Таблица в папке «Маркетинг ПИ» — по вкладке на раздел.
"""
import csv
import os
from typing import Dict, List

from marketing import settings
from marketing.planner import Plan, demand_summary
from marketing.semantics import INTENT_TITLES

MONTHS = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def tables(plan: Plan) -> Dict[str, List[List]]:
    """Все разделы плана как таблицы (первая строка — заголовок)."""
    t: Dict[str, List[List]] = {}
    t["Спрос по моделям"] = [["Модель", "На складе", "Цена от", "Коммерческий спрос", "Информационный спрос",
                              "Всего в месяц", "Спрос на 1 авто склада"]] + [
        [r["model"], r["stock"], r["price_from"] or "", r["commercial"], r["informational"], r["total"],
         r["demand_per_car"] if r["demand_per_car"] is not None else ""] for r in demand_summary(plan)]
    t["Кластеры"] = [["Модель", "Интент", "Частота", "Фраз", "Топ-фразы"]] + [
        [c.model, INTENT_TITLES[c.intent], c.volume, len(c.phrases), "; ".join(f"{p.phrase} ({p.count})" for p in c.top[:8])]
        for c in plan.clusters]
    t["Семантика"] = [["Фраза", "Частота", "Модель", "Интент", "Сид", "Источник"]] + [
        [p.phrase, p.count, c.model, INTENT_TITLES[c.intent], p.seed, p.source]
        for c in plan.clusters for p in c.top]
    t["Лендинги"] = [["Приоритет", "Страница", "URL", "Интент", "Частота", "Блоки", "Ключи"]] + [
        [l.priority, l.title, l.url, INTENT_TITLES[l.intent], l.volume, "\n".join(l.blocks), "\n".join(l.keys)]
        for l in plan.landings]
    t["SEO и статьи"] = [["Неделя", "Тип", "Заголовок", "Модель", "Интент", "Частота", "План статьи", "Ключи", "CTA"]] + [
        [a.week, a.kind, a.title, a.model, INTENT_TITLES[a.intent], a.volume, "\n".join(a.outline),
         "\n".join(a.keys), a.cta] for a in plan.articles]
    t["Хуки"] = [["Модель", "Интент", "Частота", "Заголовок (≤56)", "Текст (≤81)", "Идея креатива"]] + [
        [h.model, INTENT_TITLES[h.intent], h.volume, h.headline, h.text, h.creative] for h in plan.hooks]
    t["Директ"] = [["Кампания", "Тип", "Группа", "Частота", "Ключи", "Комментарий"]] + [
        [c.name, c.type, g["group"], g["volume"], "\n".join(g["keys"]), c.note]
        for c in plan.campaigns for g in (c.groups or [{"group": "—", "volume": "", "keys": []}])]
    t["Минус-слова"] = [["Минус-слово"]] + [[f"-{w}"] for w in plan.minus_words]
    if plan.seasonality:
        t["Сезонность"] = [["Месяц", "Индекс (1.0 = средний месяц)"]] + [
            [MONTHS[m], v] for m, v in plan.seasonality.items()]
    return t


def write_csv(plan: Plan, out_dir: str) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for name, rows in tables(plan).items():
        path = os.path.join(out_dir, f"{name}.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh, delimiter=";").writerows(rows)
        paths.append(path)
    return paths


def to_markdown(plan: Plan) -> str:
    L = [f"# Маркетинговый план: {plan.dealer}", "",
         f"Регион Вордстата: {plan.city} · сформировано {plan.generated} · частоты — показов в месяц", ""]
    L += ["## 1. Спрос и склад", "", "| Модель | На складе | Коммерч. | Информ. | Спрос на 1 авто |", "|---|---|---|---|---|"]
    for r in demand_summary(plan):
        L.append(f"| {r['model']} | {r['stock']} | {r['commercial']} | {r['informational']} | {r['demand_per_car'] or '—'} |")
    L += ["", "Модели с высоким «спросом на 1 авто» — дефицитные: держать цену, наращивать склад. "
          "С низким — давить рекламой и офферами (кредит, trade-in), чтобы не копить сток.", ""]
    L += ["## 2. Лендинги", ""]
    for l in plan.landings:
        L += [f"**{l.priority}. {l.title}** (`{l.url}`, {l.volume}/мес)", ""] + [f"- {b}" for b in l.blocks] + [""]
    L += ["## 3. SEO-статьи и блог (по неделям)", "", "| Нед. | Тип | Заголовок | Частота |", "|---|---|---|---|"]
    for a in plan.articles:
        L.append(f"| {a.week} | {a.kind} | {a.title} | {a.volume} |")
    L += ["", "## 4. Хуки для объявлений и креативов", "", "| Модель | Интент | Заголовок | Текст |", "|---|---|---|---|"]
    for h in plan.hooks:
        L.append(f"| {h.model} | {INTENT_TITLES[h.intent]} | {h.headline} | {h.text} |")
    L += ["", "## 5. Структура Яндекс Директа", ""]
    for c in plan.campaigns:
        L.append(f"- **{c.name}** ({c.type}): {len(c.groups)} групп. {c.note}".rstrip())
    L += ["", "**Минус-слова:** " + ", ".join(f"-{w}" for w in plan.minus_words[:60]), ""]
    if plan.seasonality:
        peak = sorted(plan.seasonality.items(), key=lambda kv: -kv[1])[:3]
        L += ["## 6. Сезонность", "", "Пиковые месяцы: " + ", ".join(f"{MONTHS[m]} ({v})" for m, v in peak)
              + " — на них переносить основной бюджет и акции.", ""]
    return "\n".join(L)


def push_to_sheet(plan: Plan, sheet_id: str = settings.MARKETING_SHEET_ID) -> str:
    """Перезаписывает вкладки таблицы разделами плана. Возвращает URL таблицы."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        settings.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    book = gspread.authorize(creds).open_by_key(sheet_id)
    existing = {ws.title: ws for ws in book.worksheets()}
    for name, rows in tables(plan).items():
        ws = existing.get(name) or book.add_worksheet(name, rows=max(len(rows) + 10, 100), cols=12)
        ws.clear()
        ws.update(values=[[("" if v is None else v) for v in r] for r in rows], range_name="A1")
        ws.freeze(rows=1)
    return book.url
