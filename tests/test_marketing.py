# -*- coding: utf-8 -*-
"""Проверка маркетингового модуля на синтетических данных (без сети).
Запуск: python -m pytest tests  (или python tests/test_marketing.py)"""
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from marketing.__main__ import main  # noqa: E402
from marketing.planner import build_plan  # noqa: E402
from marketing.semantics import build_clusters, build_seeds, classify, detect_model, minus_words, seasonality  # noqa: E402
from marketing.stock import group_by_model, load_stock_file, vehicle_from_dict  # noqa: E402
from marketing.wordstat import Phrase  # noqa: E402

STOCK = [
    {"brand": {"name": "Toyota"}, "model": {"name": "Camry"}, "year": 2025, "price": 4200000, "mileage": 0},
    {"brand": {"name": "Toyota"}, "model": {"name": "Camry"}, "year": 2025, "price": 3900000, "mileage": 5},
    {"brand": "Toyota", "model": "LAND CRUISER PRADO 250", "year": 2025, "price": 9500000},
    {"brand": "Toyota", "model": "rav 4", "year": 2024, "price": 3600000, "condition": "new"},
    {"brand": "Toyota", "model": "Corolla", "year": 2018, "price": 1500000, "mileage": 90000, "condition": "used"},
]
MODELS = ["Camry", "Land Cruiser Prado 250", "RAV4"]
PHRASES = [Phrase(p, c, "seed") for p, c in [
    ("тойота камри купить", 900), ("камри цена екатеринбург", 400), ("камри отзывы владельцев", 300),
    ("toyota camry 2025 комплектации", 200), ("камри в кредит", 250), ("камри или аккорд", 120),
    ("прадо 250 купить", 700), ("ленд крузер прадо 250 характеристики", 260), ("рав 4 цена", 500),
    ("рав4 из китая", 160), ("тойота трейд ин", 180), ("тойота параллельный импорт гарантия", 220),
    ("камри б/у", 2000), ("шины на камри", 300), ("тойота камри купить", 850),
]]


def test_stock_grouping():
    vehicles = [vehicle_from_dict(i) for i in STOCK]
    groups = group_by_model(vehicles)
    assert [(g.model, g.count) for g in groups] == [("Camry", 2), ("Land Cruiser Prado 250", 1), ("RAV4", 1)]
    assert groups[0].price_min == 3900000 and groups[0].price_max == 4200000


def test_stock_file_formats():
    with tempfile.TemporaryDirectory() as d:
        js = os.path.join(d, "s.json")
        with open(js, "w", encoding="utf-8") as fh:
            json.dump({"data": {"vehicles": STOCK}}, fh)
        assert len(load_stock_file(js)) == 5
        cs = os.path.join(d, "s.csv")
        with open(cs, "w", encoding="utf-8") as fh:
            fh.write("brand;model;year;price\nToyota;Camry;2025;4 200 000\n")
        assert load_stock_file(cs)[0].price == 4200000
        xml = os.path.join(d, "s.xml")
        with open(xml, "w", encoding="utf-8") as fh:
            fh.write("<cars><car><mark>Toyota</mark><model>Hilux</model><year>2025</year></car></cars>")
        assert load_stock_file(xml)[0].model == "Hilux"


def test_intents_and_models():
    assert classify("камри б/у") == "minus"
    assert classify("шины на камри") == "minus"
    assert classify("камри в кредит") == "credit"
    assert classify("камри или аккорд") == "compare"
    assert classify("рав4 из китая") == "pi"
    assert classify("тойота камри купить") == "buy"
    assert detect_model("ленд крузер прадо 250 характеристики", MODELS) == "Land Cruiser Prado 250"
    assert detect_model("рав 4 цена", MODELS) == "RAV4"
    assert detect_model("тойота трейд ин", MODELS) is None
    assert detect_model("ленд крузер 300 купить", MODELS) is None  # LC300 нет на складе — не Prado


def test_clusters_dedup_and_minus():
    clusters, minus = build_clusters(PHRASES, MODELS)
    camry_buy = next(c for c in clusters if c.model == "Camry" and c.intent == "buy")
    assert camry_buy.volume == 900 + 400  # дубль «тойота камри купить» учтён один раз (максимум)
    assert {p.phrase for p in minus} == {"камри б/у", "шины на камри"}
    assert "шины" in minus_words(minus)


def test_seeds_cover_stock():
    groups = group_by_model([vehicle_from_dict(i) for i in STOCK])
    seeds = build_seeds(groups)
    assert "toyota camry" in seeds and "камри купить" in seeds and "тойота параллельный импорт" in seeds
    assert len(seeds) == len(set(seeds))


def test_seasonality():
    dyn = [{"date": f"2025-{m:02d}-01", "count": 100 if m != 3 else 200} for m in range(1, 13)]
    s = seasonality(dyn)
    assert s[3] > 1.5 and s[1] < 1


def test_plan_sections():
    groups = group_by_model([vehicle_from_dict(i) for i in STOCK])
    clusters, minus = build_clusters(PHRASES, MODELS)
    plan = build_plan("ПИ Toyota Екатеринбург", "Екатеринбург", groups, clusters, minus_words(minus), {},
                      today=datetime.date(2026, 10, 10))
    urls = [l.url for l in plan.landings]
    assert "/toyota/camry/" in urls and "/toyota/camry/kredit/" in urls and "/toyota/" in urls
    assert any("отзывы" in a.title for a in plan.articles)
    assert all(len(h.headline) <= 56 and len(h.text) <= 81 for h in plan.hooks)
    assert any(h.model == "Camry" and h.intent == "credit" for h in plan.hooks)


def test_cli_offline():
    with tempfile.TemporaryDirectory() as d:
        stock, ws, out = (os.path.join(d, n) for n in ("stock.json", "ws.csv", "out"))
        with open(stock, "w", encoding="utf-8") as fh:
            json.dump(STOCK, fh)
        with open(ws, "w", encoding="utf-8") as fh:
            fh.write("Фраза;Частота\n" + "\n".join(f"{p.phrase};{p.count}" for p in PHRASES))
        assert main(["--stock-file", stock, "--wordstat-csv", ws, "--out", out]) == 0
        files = set(os.listdir(out))
        assert {"План.md", "Лендинги.csv", "Хуки.csv", "SEO и статьи.csv", "Директ.csv"} <= files


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
