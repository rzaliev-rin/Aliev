# -*- coding: utf-8 -*-
"""
Запуск:
  python -m marketing                         # склад из API MaxPoster + Вордстат API
  python -m marketing --stock-file stock.json # склад из файла (JSON/CSV/XML-фид)
  python -m marketing --wordstat-csv ws.csv   # фразы из ручной выгрузки Вордстата
  python -m marketing --sheet                 # дополнительно записать в Google Таблицу
Результат: marketing_out/План.md и CSV по разделам.
"""
import argparse
import csv
import datetime
import os
import sys

from marketing import settings
from marketing.planner import build_plan
from marketing.report import push_to_sheet, to_markdown, write_csv
from marketing.semantics import build_clusters, build_seeds, minus_words, seasonality
from marketing.stock import fetch_stock_api, group_by_model, load_stock_file
from marketing.wordstat import Phrase, WordstatClient


def read_wordstat_csv(path: str):
    """Ручная выгрузка: колонки «фраза;частота» (заголовок необязателен)."""
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    rows = csv.reader(text.splitlines(), delimiter=";" if ";" in text else ",")
    phrases = []
    for row in rows:
        if len(row) >= 2 and row[1].replace(" ", "").isdigit():
            phrases.append(Phrase(row[0].strip(), int(row[1].replace(" ", "")), "csv", "csv"))
    return phrases


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m marketing", description="Вордстат -> маркетинговый план")
    ap.add_argument("--dealer", default=settings.MARKETING_DEALER_ID, help="ID автосалона в MaxPoster")
    ap.add_argument("--stock-file", help="склад из файла вместо API")
    ap.add_argument("--wordstat-csv", help="фразы из CSV вместо Вордстат API")
    ap.add_argument("--out", default="marketing_out")
    ap.add_argument("--sheet", action="store_true", help="записать в Google Таблицу MARKETING_SHEET_ID")
    ap.add_argument("--include-used", action="store_true", help="учитывать и авто с пробегом")
    args = ap.parse_args(argv)

    vehicles = load_stock_file(args.stock_file) if args.stock_file else fetch_stock_api(args.dealer)
    models = group_by_model(vehicles, only_new=not args.include_used)
    print(f"Склад: {len(vehicles)} авто, моделей: {len(models)} — " + ", ".join(f"{g.model} ({g.count})" for g in models))
    if not models:
        print("На складе нет новых авто — нечего планировать.", file=sys.stderr)
        return 1

    season = {}
    if args.wordstat_csv:
        phrases = read_wordstat_csv(args.wordstat_csv)
    else:
        client = WordstatClient()
        seeds = build_seeds(models)
        phrases = []
        for i, seed in enumerate(seeds, 1):
            res = client.top_requests(seed)
            phrases += res.phrases
            print(f"  [{i}/{len(seeds)}] {seed}: {res.total}")
        today = datetime.date.today()
        start = datetime.date(today.year - 2, today.month, 1)
        last_month = today.replace(day=1) - datetime.timedelta(days=1)
        season = seasonality(client.dynamics("тойота", start.isoformat() + "T00:00:00Z",
                                             last_month.isoformat() + "T00:00:00Z"))

    clusters, minus = build_clusters(phrases, [g.model for g in models])
    plan = build_plan(settings.MARKETING_DEALER_NAME, settings.CITY, models, clusters, minus_words(minus), season)

    os.makedirs(args.out, exist_ok=True)
    md_path = os.path.join(args.out, "План.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(plan))
    write_csv(plan, args.out)
    print(f"Фраз: {len(phrases)}, кластеров: {len(clusters)}, лендингов: {len(plan.landings)}, "
          f"статей: {len(plan.articles)}, хуков: {len(plan.hooks)}\nПлан: {md_path}")
    if args.sheet:
        print("Google Таблица:", push_to_sheet(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
