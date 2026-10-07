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

_REAL_LOAD_FROM_SHEET = pricing_config._load_from_sheet
pricing_config._load_from_sheet = lambda: None  # тесты не ходят в Google
TODAY = datetime.date(2026, 9, 24)

# Входные данные, дающие нужную категорию (пробег до 13 000 км/год для кат. А)
CAT_A = dict(colors="Не более 1", condition="Не требует вложений", extra="Не применимо", year=2020, mileage=60_000)
CAT_B = dict(colors="2 - 3 окраса", condition="Легко устранимые тех. недостатки.", extra="Не применимо",
             year=2019, mileage=90_000)
CAT_C = dict(colors="4 и более", condition="Легко устранимые тех. недостатки.", extra="Не применимо",
             year=2019, mileage=90_000)

# Салон (= город по метрике): (TI кат.А 1 000 000, TI кат.В 1 000 000, TI кат.С 700 000, Выкуп кат.С 700 000)
EXPECTED = {
    "3265: АСП Волгоград Chery": (788_000, 744_000, 434_000, 322_000),
    "3515: АСП Казань Chery": (796_000, 736_000, 406_000, 294_000),
    "3047: АСП Краснодар Changan": (822_000, 785_000, 492_000, 392_000),
    "8991: АСП НН CHERY": (805_000, 753_000, 427_000, 315_000),
    "3896: АСП Новосибирск Chery": (780_000, 761_000, 448_000, 336_000),
    "10088: АСП Тюмень Exeed": (813_000, 753_000, 476_000, 371_000),
}
NOVOSIB = "3896: АСП Новосибирск Chery"
KAZAN = "3515: АСП Казань Chery"


def _calc(salon, cat, avito, reception="Trade-In", **kw):
    return calc_appraisal(AppraisalInput(avito_price=avito, reception_type=reception, salon=salon,
                                         refurbishment_cost=10_000, **cat, **kw), today=TODAY)


def test_change_log_examples():
    for salon, (a, b, c, street_c) in EXPECTED.items():
        r = _calc(salon, CAT_A, 1_000_000)
        assert r.category == "Категория А" and r.purchase_price_rop == a, (salon, r)
        r = _calc(salon, CAT_B, 1_000_000)
        assert r.category == "Категория В" and r.purchase_price_rop == b, (salon, r)
        r = _calc(salon, CAT_C, 700_000)
        assert r.category == "Категория С" and r.purchase_price_rop == c, (salon, r)
        r = _calc(salon, CAT_C, 700_000, reception="Выкуп с улицы")
        assert r.purchase_price_rop == street_c, (salon, r)


def test_pdf_example_novosibirsk():
    r = _calc(NOVOSIB, CAT_B, 1_000_000)
    assert r.mileage_per_year == 12_857 and r.score == 25 and r.category == "Категория В"
    assert round(r.planned_sale_price_rop) == 940_000 and round(r.planned_sale_price_ddc) == 970_000
    assert round(r.gross_margin_rop) == 169_200 and r.ceiling_price == 800_000
    assert r.purchase_price_rop == 761_000 and round(r.purchase_price_ddc) == 790_800
    assert round(r.gm2_rop) == 84_224  # «≈ 84 200 ₽»
    statuses = {750_000: "Согласовано", 770_000: "Согласование ДДЦ",
                795_000: "Согласование УК", 810_000: "Согласование УК (выше потолка)"}
    for price, status in statuses.items():
        assert _calc(NOVOSIB, CAT_B, 1_000_000, negotiated_price=price).approval_status == status


def test_reject_when_limit_is_zero():
    r = _calc(KAZAN, CAT_C, 250_000, reception="Выкуп с улицы", negotiated_price=50_000)
    assert r.purchase_price_rop == 0 and not r.can_accept and r.approval_status == "Не принимать"


def test_trade_up_uses_trade_in_norms():
    ti = _calc(KAZAN, CAT_A, 1_000_000)
    tu = _calc(KAZAN, CAT_A, 1_000_000, reception="Trade-Up")
    assert ti.purchase_price_rop == tu.purchase_price_rop


def test_toyota_salons_use_krasnodar_coefficients():
    krasnodar = _calc("3047: АСП Краснодар Changan", CAT_B, 1_000_000).purchase_price_rop
    assert krasnodar == 785_000
    assert _calc("10089: !АСП Тюмень Toyota действующий", CAT_B, 1_000_000).purchase_price_rop == krasnodar
    assert _calc("10120: АСП Toyota - Екатеринбург", CAT_B, 1_000_000).purchase_price_rop == krasnodar


def test_find_salon_by_maxposter_name():
    from pricing_engine import find_salon, salon_label
    assert find_salon("10089: !АСП Тюмень Toyota действующий") == "10089: !АСП Тюмень Toyota действующий"
    assert find_salon("АСП Тюмень Toyota действующий") == "10089: !АСП Тюмень Toyota действующий"  # без номера и «!»
    assert find_salon("3047:  асп краснодар CHANGAN ") == "3047: АСП Краснодар Changan"  # регистр и пробелы
    assert find_salon("9999: АСП Самара Haval") is None  # нет в таблице -> оценщик выберет кнопкой
    assert salon_label("3047: АСП Краснодар Changan") == "Краснодар Changan"
    assert salon_label("10089: !АСП Тюмень Toyota действующий") == "Тюмень Toyota"
    assert salon_label("10120: АСП Toyota - Екатеринбург") == "Екатеринбург Toyota"


def test_new_salon_from_sheet_row():
    """Новая строка «ПЦП автосалона» в таблице — и бот её учитывает."""
    import pricing_engine
    rows = [{"Раздел": "ПЦП автосалона", "Параметр": "11111: АСП Самара Haval",
             "Значение1": 95, "Значение2": 92, "Значение3": 82, "Значение4": "Самара", "Значение5": "Haval"}]
    saved = pricing_config._params_cache, pricing_config._params_cache_at
    pricing_config._get_records = lambda: rows
    pricing_config._params_cache = _REAL_LOAD_FROM_SHEET()
    pricing_config._params_cache_at = float("inf")
    try:
        salon = pricing_engine.find_salon("!11111: АСП Самара Haval")
        assert salon == "11111: АСП Самара Haval" and pricing_engine.salon_label(salon) == "Самара Haval"
        # коэффициенты как у Волгограда -> тот же лимит
        assert _calc(salon, CAT_B, 1_000_000).purchase_price_rop == 744_000
    finally:
        pricing_config._params_cache, pricing_config._params_cache_at = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
