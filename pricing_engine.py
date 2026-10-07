# -*- coding: utf-8 -*-
"""
Модуль расчёта выкупной цены автомобиля в трейд-ин — метрика закупки АСП v2.0.

Логика 1-в-1 повторяет формулы из файла
"Метрики приёма v2.0 24.09.2026 Экспокар.xlsx" (лист "Метрика оценки"),
в комментариях указаны ячейки. Главное правило:
    закупка = ПЦП − плановая ВП − переподготовка (но не выше потолка к Авито Оценке).

Все нормы (баллы, пробег, ПЦП по автосалонам, доходность, потолок, GM2) берутся
из pricing_config.get_params() — их можно менять в Google Таблице без правки кода.
"""

import datetime
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from typing import Optional

import pricing_config

METRIC_VERSION = pricing_config.METRIC_VERSION

RECEPTION_TYPES = ["Trade-In", "Trade-Up", "Выкуп с улицы", "Trade-In на ПИ"]
STREET_BUYOUT = "Выкуп с улицы"

# Статусы согласования (ячейка D12)
STATUS_OK = "Согласовано"
STATUS_DDC = "Согласование ДДЦ"
STATUS_UK = "Согласование УК"
STATUS_UK_CEILING = "Согласование УК (выше потолка)"
STATUS_REJECT = "Не принимать"
# Статусы, которые решает УК (первый этап в чате салона — ДДЦ, затем УК лично)
UK_STATUSES = {STATUS_UK, STATUS_UK_CEILING, STATUS_REJECT}

EXTRA_YOUNG_CAR = "Возраст авто до 2х лет"
EXTRA_NOT_APPLICABLE = "Не применимо"


def colors_bucket_from_count(count: int) -> str:
    """Переводит число окрашенных деталей (по данным API/эвристики) в бакет
    для colors_score, аналогично тому, как это раньше выбирал менеджер руками."""
    if count <= 1:
        return "Не более 1"
    if count <= 3:
        return "2 - 3 окраса"
    return "4 и более"


def get_colors_options() -> list:
    return list(pricing_config.get_params()["colors_score"].keys())


def get_condition_options() -> list:
    return list(pricing_config.get_params()["condition_score"].keys())


def get_extra_options() -> list:
    return list(pricing_config.get_params()["extra_score"].keys())


def get_salon_options() -> list:
    """Названия автосалонов (как в MaxPoster) из таблицы «ПЦП автосалона»."""
    return list(pricing_config.get_params()["salons"].keys())


def salon_info(salon: Optional[str]) -> dict:
    """{"ptsp": {...}, "city": ..., "brand": ...} или {} для неизвестного салона."""
    return pricing_config.get_params()["salons"].get(salon or "", {})


def salon_label(salon: Optional[str]) -> str:
    """Как показывать салон: «Тюмень Toyota» (город и бренд из таблицы);
    если их не заполнили — название из MaxPoster без номера."""
    if not salon:
        return "—"
    info = salon_info(salon)
    label = " ".join(x for x in (info.get("city"), info.get("brand")) if x)
    return label or re.sub(r"^\s*\d+\s*[:\-–]\s*!*\s*", "", str(salon)).strip() or str(salon)


def _normalize_salon(name) -> str:
    """«10089: !АСП Тюмень Toyota действующий» -> «асп тюмень toyota действующий»:
    без номера и «!» в начале, без регистра и лишних пробелов."""
    if not name:
        return ""
    s = str(name).strip().lower().replace("ё", "е")
    while True:
        new_s = re.sub(r"^!+\s*", "", s)
        new_s = re.sub(r"^\d+\s*[:\-–]\s*", "", new_s).strip()
        if new_s == s:
            break
        s = new_s
    return " ".join(s.split())


def find_salon(dealer_name: Optional[str]) -> Optional[str]:
    """Салон из таблицы «ПЦП автосалона» по названию из MaxPoster
    (dealer.companyName). Номер и «!» в начале не важны. None — салона нет в таблице."""
    if not dealer_name:
        return None
    salons = pricing_config.get_params()["salons"]
    if dealer_name in salons:
        return dealer_name
    target = _normalize_salon(dealer_name)
    for name in salons:
        if _normalize_salon(name) == target:
            return name
    return None


def car_age_years(year: Optional[int], today: Optional[datetime.date] = None) -> Optional[int]:
    if not year:
        return None
    today = today or datetime.date.today()
    return today.year - int(year)


def is_young_car(year: Optional[int], today: Optional[datetime.date] = None) -> bool:
    """«Возраст авто до 2х лет»: разница «текущий год − год выпуска» меньше 2."""
    age = car_age_years(year, today)
    return age is not None and age < 2


def _excel_round(x: float, digits: int) -> float:
    """ROUND() как в Excel — половина округляется от нуля (в отличие от round() в Python)."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def _excel_rounddown(x: float, digits: int) -> float:
    """ROUNDDOWN() как в Excel — к нулю."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_DOWN))


def mileage_per_year(year: int, mileage: float, today: Optional[datetime.date] = None) -> float:
    """E3 = ROUND(пробег / MAX(1, текущий год − год выпуска), 0)."""
    today = today or datetime.date.today()
    return _excel_round(mileage / max(1, today.year - int(year)), 0)


def mileage_points(per_year: float) -> int:
    """K4: баллы за пробег в год."""
    for up_to, points in pricing_config.get_params()["mileage_score"]:
        if per_year <= up_to:
            return points
    return 0


def calc_category(colors: str, condition: str, extra: str, per_year: float) -> tuple[str, int]:
    """Возвращает (категория, сумма_баллов). Аналог B4/K5."""
    params = pricing_config.get_params()
    score = (
        params["colors_score"].get(colors, 0)
        + params["condition_score"].get(condition, 0)
        + params["extra_score"].get(extra, 0)
        + mileage_points(per_year)
    )
    for name, threshold in params["category_thresholds"]:
        if score >= threshold:
            return name, score
    return "Категория С", score


def _vlookup_tier(price: float, tiers):
    """
    Аналог VLOOKUP(price, диапазон, ..., 1) — приближённый поиск по нижней
    границе ценовой группы. Возвращает (доходность, мин.доход).
    """
    match = tiers[0]
    for row in tiers:
        if price >= row[0]:
            match = row
        else:
            break
    _, _, rate, min_income = match
    return rate, min_income


def calc_gross_margin(ptsp: float, category: str, reception_type: str) -> float:
    """B13: плановая валовая прибыль с НДС — от ПЦП (не от Авито Оценки)."""
    params = pricing_config.get_params()

    def tiered_value(tiers):
        rate, min_income = _vlookup_tier(ptsp, tiers)
        return max(ptsp * rate, min_income or 0)

    if reception_type in ("Trade-In", "Trade-Up") and category == "Категория А":
        return tiered_value(params["tradein_cat_a_tiers"])

    candidates = [tiered_value(params["tradein_cat_bc_tiers"])]
    fr = params["flat_rates"].get(reception_type)
    candidates.append(max(ptsp * fr["доходность"], fr["мин.доход"]) if fr else 0)
    return max(candidates)


# =====================================================================
# ОСНОВНОЙ РАСЧЁТ
# =====================================================================

@dataclass
class AppraisalInput:
    avito_price: float          # B8 "Авито Оценка" на день осмотра
    reception_type: str         # B9 "Тип приема"
    colors: str                 # B1 "Окрасы"
    condition: str              # B2 "Тех. состояние"
    extra: str                  # B3 "Доп. данные"
    salon: Optional[str] = None  # B10 "ГОРОД" -> автосалон из таблицы «ПЦП автосалона»
    year: Optional[int] = None  # E1 "Год выпуска"
    mileage: Optional[float] = None  # E2 "Пробег, км"
    negotiated_price: Optional[float] = None   # C14 "Прогноз ДЦ: закупка" — цена, о которой договорились с клиентом
    refurbishment_cost: float = 0.0            # C15 "Переподготовка"
    manager_resale_forecast: Optional[float] = None  # C12 "Прогноз ДЦ: цена продажи" (если не задан — ПЦП по метрике)


@dataclass
class AppraisalResult:
    category: str
    score: int
    mileage_per_year: float            # E3
    ceiling_coef: float                # E4
    ceiling_price: float               # E5 — потолок закупки к Авито Оценке
    planned_sale_price_rop: float      # B12 — ПЦП по метрике
    planned_sale_price_ddc: float      # E12 — ПЦП с полномочиями ДДЦ
    gross_margin_rop: float            # B13
    gross_margin_ddc: float            # E13 = B13
    purchase_price_rop: float          # B14 — лимит закупки РОП
    purchase_price_ddc: float          # E14 — предел ДДЦ
    refurbishment: float               # B15/E15 = C15
    gm2_rop: float                     # B16
    gm2_ddc: float                     # E16
    negotiated_price: Optional[float]  # C14
    approval_status: Optional[str]     # D12 (если задан C14)
    manager_resale_forecast: Optional[float]  # C12 (введённый или = B12)
    margin_negotiated: Optional[float]        # C13 = C12 − C14 − C15
    gm2_negotiated: Optional[float]    # C16

    @property
    def can_accept(self) -> bool:
        """False = лимит закупки 0 (ВП больше, чем позволяет ПЦП) → «Не принимать»."""
        return self.purchase_price_rop > 0


def calc_appraisal(data: AppraisalInput, today: Optional[datetime.date] = None) -> AppraisalResult:
    params = pricing_config.get_params()
    if not data.salon or data.salon not in params["salons"]:
        raise ValueError("Не выбран автосалон")
    if not data.year or data.mileage is None:
        raise ValueError("Не заполнены год выпуска или пробег")

    per_year = mileage_per_year(data.year, data.mileage, today)                       # E3
    category, score = calc_category(data.colors, data.condition, data.extra, per_year)  # B4, K5
    street = data.reception_type == STREET_BUYOUT

    # E4/E5: потолок закупки к Авито Оценке
    ceiling_coef = params["ceiling"].get(category, {}).get("street" if street else "default", 0)
    ceiling_price = _excel_rounddown(data.avito_price * ceiling_coef, -3)

    # B12/E12: ПЦП = Авито Оценка × (коэф. салона и категории − поправка выкупа [+ п.п. ДДЦ])
    coef = params["salons"][data.salon]["ptsp"].get(category, 0)
    if street:
        coef -= params["street_ptsp_minus"].get(category, 0)
    planned_rop = data.avito_price * coef
    planned_ddc = data.avito_price * (coef + params["ddc_ptsp_bonus"])

    gross_margin = calc_gross_margin(planned_rop, category, data.reception_type)  # B13 (= E13)
    refurb = data.refurbishment_cost or 0.0                                        # C15

    # B14 = MAX(0, MIN(ROUND(B12 − B13 − C15, −3), E5))
    purchase_rop = max(0.0, min(_excel_round(planned_rop - gross_margin - refurb, -3), ceiling_price))
    # E14 = MAX(0, MIN(E12 − B13 − C15, E5)) — без округления, как в файле
    purchase_ddc = max(0.0, min(planned_ddc - gross_margin - refurb, ceiling_price))

    f = params["gm2_formula"]

    def gm2(margin_after_refurb: float, purchase_price: float) -> float:
        # GM2 = ВП − переподготовка − 15 000 − закупка × 1,75% × 1,5 − 40 000
        return (margin_after_refurb - f["fixed_1"]
                - (purchase_price * f["purchase_rate"]) * f["purchase_rate_multiplier"] - f["fixed_2"])

    gm2_rop = gm2(gross_margin - refurb, purchase_rop)  # B16
    gm2_ddc = gm2(gross_margin - refurb, purchase_ddc)  # E16

    approval_status = None
    manager_resale_forecast = None
    margin_negotiated = None
    gm2_negotiated = None
    if data.negotiated_price is not None:
        c14 = data.negotiated_price
        # D12: проверки сверху вниз, срабатывает первая подходящая
        if purchase_rop <= 0:
            approval_status = STATUS_REJECT
        elif c14 > ceiling_price:
            approval_status = STATUS_UK_CEILING
        elif c14 <= purchase_rop:
            approval_status = STATUS_OK
        elif c14 <= purchase_ddc:
            approval_status = STATUS_DDC
        else:
            approval_status = STATUS_UK

        manager_resale_forecast = (
            data.manager_resale_forecast if data.manager_resale_forecast is not None else planned_rop
        )
        margin_negotiated = manager_resale_forecast - c14 - refurb   # C13 = C12 − C14 − C15
        gm2_negotiated = gm2(margin_negotiated, c14)                  # C16 (C13 уже без переподготовки)

    return AppraisalResult(
        category=category,
        score=score,
        mileage_per_year=per_year,
        ceiling_coef=ceiling_coef,
        ceiling_price=ceiling_price,
        planned_sale_price_rop=planned_rop,
        planned_sale_price_ddc=planned_ddc,
        gross_margin_rop=gross_margin,
        gross_margin_ddc=gross_margin,
        purchase_price_rop=purchase_rop,
        purchase_price_ddc=purchase_ddc,
        refurbishment=refurb,
        gm2_rop=gm2_rop,
        gm2_ddc=gm2_ddc,
        negotiated_price=data.negotiated_price,
        approval_status=approval_status,
        manager_resale_forecast=manager_resale_forecast,
        margin_negotiated=margin_negotiated,
        gm2_negotiated=gm2_negotiated,
    )
