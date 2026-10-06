# -*- coding: utf-8 -*-
"""Проверка расчёта на примерах из «Метрики приёма v2.0 24.09.2026 Экспокар.xlsx»
(лист «Изменения метрики») и из описания метрики (PDF, «Пример расчёта»).
Запуск: python -m pytest tests  (или python tests/test_pricing_engine.py)"""
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_FILE", "/nonexistent.json")  # таблица недоступна -> дефолты v2

import pricing_config  # noqa: E402
from pricing_engine import AppraisalInput, calc_appraisal  # noqa: E402

pricing_config._load_from_sheet = lambda: None  # тесты не ходят в Google
TODAY = datetime.date(2026, 9, 24)

# Входные данные, дающие нужную категорию (пробег до 13 000 км/год для кат. А)
CAT_A = dict(colors="Не более 1", condition="Не требует вложений", extra="Не применимо", year=2020, mileage=60_000)
CAT_B = dict(colors="2 - 3 окраса", condition="Легко устранимые тех. недостатки.", extra="Не применимо",
             year=2019, mileage=90_000)
CAT_C = dict(colors="4 и более", condition="Легко устранимые тех. недостатки.", extra="Не применимо",
             year=2019, mileage=90_000)

# Город: (TI кат.А 1 000 000, TI кат.В 1 000 000, TI кат.С 700 000, Выкуп кат.С 700 000)
EXPECTED = {
    "Волгоград": (788_000, 744_000, 434_000, 322_000),
    "Казань": (796_000, 736_000, 406_000, 294_000),
    "Краснодар": (822_000, 785_000, 492_000, 392_000),
    "Нижний Новгород": (805_000, 753_000, 427_000, 315_000),
    "Новосибирск": (780_000, 761_000, 448_000, 336_000),
    "Тюмень": (813_000, 753_000, 476_000, 371_000),
}


def _calc(city, cat, avito, reception="Trade-In", **kw):
    return calc_appraisal(AppraisalInput(avito_price=avito, reception_type=reception, city=city,
                                         refurbishment_cost=10_000, **cat, **kw), today=TODAY)


def test_change_log_examples():
    for city, (a, b, c, street_c) in EXPECTED.items():
        r = _calc(city, CAT_A, 1_000_000)
        assert r.category == "Категория А" and r.purchase_price_rop == a, (city, r)
        r = _calc(city, CAT_B, 1_000_000)
        assert r.category == "Категория В" and r.purchase_price_rop == b, (city, r)
        r = _calc(city, CAT_C, 700_000)
        assert r.category == "Категория С" and r.purchase_price_rop == c, (city, r)
        r = _calc(city, CAT_C, 700_000, reception="Выкуп с улицы")
        assert r.purchase_price_rop == street_c, (city, r)


def test_pdf_example_novosibirsk():
    r = _calc("Новосибирск", CAT_B, 1_000_000)
    assert r.mileage_per_year == 12_857 and r.score == 25 and r.category == "Категория В"
    assert round(r.planned_sale_price_rop) == 940_000 and round(r.planned_sale_price_ddc) == 970_000
    assert round(r.gross_margin_rop) == 169_200 and r.ceiling_price == 800_000
    assert r.purchase_price_rop == 761_000 and round(r.purchase_price_ddc) == 790_800
    assert round(r.gm2_rop) == 84_224  # «≈ 84 200 ₽»
    statuses = {750_000: "Согласовано", 770_000: "Согласование ДДЦ",
                795_000: "Согласование УК", 810_000: "Согласование УК (выше потолка)"}
    for price, status in statuses.items():
        assert _calc("Новосибирск", CAT_B, 1_000_000, negotiated_price=price).approval_status == status


def test_reject_when_limit_is_zero():
    r = _calc("Казань", CAT_C, 250_000, reception="Выкуп с улицы", negotiated_price=50_000)
    assert r.purchase_price_rop == 0 and not r.can_accept and r.approval_status == "Не принимать"


def test_trade_up_uses_trade_in_norms():
    ti = _calc("Казань", CAT_A, 1_000_000)
    tu = _calc("Казань", CAT_A, 1_000_000, reception="Trade-Up")
    assert ti.purchase_price_rop == tu.purchase_price_rop


def test_toyota_uses_krasnodar_coefficients():
    from pricing_engine import city_from_salon_name
    toyota = _calc("Тойота", CAT_B, 1_000_000)
    assert toyota.purchase_price_rop == _calc("Краснодар", CAT_B, 1_000_000).purchase_price_rop == 785_000
    assert city_from_salon_name("Тойота Центр Казань") == "Тойота"
    assert city_from_salon_name("3047: TOYOTA Тюмень") == "Тойота"
    assert city_from_salon_name("Экспокар Казань") == "Казань"
    assert city_from_salon_name("Экспокар Тюмени") == "Тюмень"
    assert city_from_salon_name("Какой-то салон") is None


def test_city_from_salon_name_variants():
    from pricing_engine import city_from_salon_name
    cases = {
        "Экспокар в Нижнем Новгороде": "Нижний Новгород",
        "!3051: Экспокар Н.Новгород": "Нижний Новгород",
        "Экспокар НН Мещера": "Нижний Новгород",
        "Expocar Nizhny Novgorod": "Нижний Новгород",
        "Экспокар Краснодар (Ростовское ш.)": "Краснодар",
        "Экспокар Новосиб": "Новосибирск",
        "Экспокар Волгограде": "Волгоград",
        "EXPOCAR KAZAN": "Казань",
        "Экспокар Красноярск": None,          # не путаем с Краснодаром
        "Экспокар Казань / Тюмень": None,     # два города — спросим
        "Экспокар Ганновер": None,            # «нн» внутри слова не считается
        # реальные названия салонов из вкладки «Иерархия согласования»
        "3047: АСП Краснодар Changan": "Краснодар",
        "3265: АСП Волгоград Chery": "Волгоград",
        "3515: АСП Казань Chery": "Казань",
        "3896: АСП Новосибирск Chery": "Новосибирск",
        "8991: АСП НН CHERY": "Нижний Новгород",
        "10088: АСП Тюмень Exeed": "Тюмень",
        "10089: !АСП Тюмень Toyota действующий": "Тойота",
        "10120: АСП Toyota - Екатеринбург": "Тойота",
    }
    for name, expected in cases.items():
        assert city_from_salon_name(name) == expected, (name, city_from_salon_name(name))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
