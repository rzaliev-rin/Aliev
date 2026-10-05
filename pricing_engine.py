# -*- coding: utf-8 -*-
"""
Модуль расчёта выкупной цены автомобиля в трейд-ин.

Логика 1-в-1 повторяет формулы из файла
"Метрики_приёма_06_2026_.xlsx" (лист "Метрика оценки").

Все "магические числа" (баллы, коэффициенты, ценовые диапазоны)
вынесены в константы ниже — если в компании поменяют условия,
достаточно поправить эти таблицы, код трогать не нужно.
"""

from dataclasses import dataclass, field
from typing import Optional

import pricing_config


# =====================================================================
# 1. СПРАВОЧНИКИ ДЛЯ ОПРЕДЕЛЕНИЯ КАТЕГОРИИ (A/B/C)
#    Актуальные значения читаются из Google Таблицы (вкладка "Параметры
#    Метрики") через pricing_config — значения ниже используются только
#    как запасной вариант, если таблица недоступна.
# =====================================================================

RECEPTION_TYPES = ["Trade-In", "Trade-Up", "Выкуп с улицы", "Trade-In на ПИ"]


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


def calc_category(colors: str, condition: str, extra: str) -> tuple[str, int]:
    """Возвращает (категория, сумма_баллов). Аналог B4/K5 в Excel."""
    params = pricing_config.get_params()
    score = (
        params["colors_score"].get(colors, 0)
        + params["condition_score"].get(condition, 0)
        + params["extra_score"].get(extra, 0)
    )
    for name, threshold in params["category_thresholds"]:
        if score >= threshold:
            return name, score
    return "Ошибка", score  # практически недостижимо


def _vlookup_tier(price: float, tiers):
    """
    Аналог VLOOKUP(price, диапазон, ..., 1) — приближённый поиск.
    Возвращает (доходность, мин.доход) для тарифной строки, в которую
    попадает price (по нижней границе диапазона, как это делает Excel
    с приближённым VLOOKUP).
    """
    match = tiers[0]
    for row in tiers:
        if price >= row[0]:
            match = row
        else:
            break
    _, _, rate, min_income = match
    return rate, min_income


def calc_gross_margin(avito_price: float, category: str, reception_type: str) -> float:
    """
    Аналог формулы B11 (валовая прибыль с НДС).
    """
    params = pricing_config.get_params()

    def tiered_value(tiers):
        rate, min_income = _vlookup_tier(avito_price, tiers)
        candidates = [avito_price * rate]
        if min_income is not None:
            candidates.append(min_income)
        return max(candidates)

    if reception_type == "Trade-In" and category == "Категория А":
        return tiered_value(params["tradein_cat_a_tiers"])

    # иначе — как в оригинальной формуле: берём максимум из
    # (тарифной таблицы B/C) и (плоской ставки по типу приёма, если применимо)
    candidates = [tiered_value(params["tradein_cat_bc_tiers"])]

    flat_rates = params["flat_rates"]
    if reception_type in flat_rates:
        fr = flat_rates[reception_type]
        candidates.append(max(avito_price * fr["доходность"], fr["мин.доход"]))
    else:
        candidates.append(0)

    return max(candidates)


# =====================================================================
# 4. ОСНОВНОЙ РАСЧЁТ
# =====================================================================

@dataclass
class AppraisalInput:
    avito_price: float          # "Авито Оценка" (B6) — прогнозная цена из стороннего приложения
    reception_type: str         # "Тип приема" (B7)
    colors: str                 # "Окрасы" (B1)
    condition: str              # "Тех. состояние" (B2)
    extra: str                  # "Доп. данные" (B3)
    negotiated_price: Optional[float] = None   # "Прогноз ДЦ" -> Закупка (C12), цена, о которой договорились с клиентом
    refurbishment_cost: float = 0.0            # "Переподготовка" (C13)
    manager_resale_forecast: Optional[float] = None
    # "Прогноз ДЦ" -> Планируемая цена продажи (C10) — собственный прогноз
    # менеджера по цене перепродажи. Если не указан, по умолчанию берётся
    # системный расчёт (B10), как в Excel предполагается ручной ввод.


@dataclass
class AppraisalResult:
    category: str
    score: int
    planned_sale_price_rop: float      # B10
    planned_sale_price_ddc: float      # E10
    gross_margin_rop: float            # B11
    gross_margin_ddc: float            # E11
    purchase_price_rop: float          # B12 — рекомендованный потолок закупки (уровень РОП)
    purchase_price_ddc: float          # E12 — потолок закупки при согласовании ДДЦ
    refurbishment: float               # B13/E13
    gm2_rop: float                     # B14
    gm2_ddc: float                     # E14
    negotiated_price: Optional[float]  # C12, если менеджер указал цену
    approval_status: Optional[str]     # D10-аналог, если negotiated_price задан
    manager_resale_forecast: Optional[float]  # C10 (введённый или дефолтный = B10)
    margin_negotiated: Optional[float]        # C11
    gm2_negotiated: Optional[float]    # C14, если negotiated_price задан


def calc_appraisal(data: AppraisalInput) -> AppraisalResult:
    category, score = calc_category(data.colors, data.condition, data.extra)

    params = pricing_config.get_params()
    coef = params["ptsp_coef"].get(category, {"РОП": 0, "ДДЦ": 0})
    planned_rop = data.avito_price * coef["РОП"]   # B10
    planned_ddc = data.avito_price * coef["ДДЦ"]   # E10

    gross_margin_rop = calc_gross_margin(data.avito_price, category, data.reception_type)  # B11
    gross_margin_ddc = gross_margin_rop  # E11 = B11 в оригинале

    refurb = data.refurbishment_cost  # B13 = C13, E13 = C13

    # B12 = ROUND(B10 - B11 - C13, -3), минимум 5000, если результат < 0
    raw_rop = round((planned_rop - gross_margin_rop - refurb) / 1000) * 1000
    purchase_rop = raw_rop if raw_rop >= 0 else 5000

    # E12 = E10 - B11 - C13  (в оригинале без округления и без пола в 5000)
    purchase_ddc = planned_ddc - gross_margin_rop - refurb

    def gm2(purchase_price: float, margin: float) -> float:
        # GM2 = margin - refurb - fixed_1 - (purchase * rate%) * multiplier - fixed_2
        f = params["gm2_formula"]
        rate = f["purchase_rate"]
        return margin - refurb - f["fixed_1"] - (purchase_price * rate) * f["purchase_rate_multiplier"] - f["fixed_2"]

    gm2_rop = gm2(purchase_rop, gross_margin_rop)     # B14
    gm2_ddc = gm2(purchase_ddc, gross_margin_ddc)     # E14

    approval_status = None
    gm2_negotiated = None
    margin_negotiated = None
    manager_resale_forecast = None
    if data.negotiated_price is not None:
        # D10: сравнение цены, согласованной с клиентом (C12), с потолками B12 / E12
        if data.negotiated_price <= purchase_rop:
            approval_status = "Согласование РОП"
        elif data.negotiated_price <= purchase_ddc:
            approval_status = "Согласование ДДЦ"
        else:
            approval_status = "Согласование УК"

        # C10: если менеджер не задал свой прогноз цены перепродажи, берём системный B10
        manager_resale_forecast = (
            data.manager_resale_forecast
            if data.manager_resale_forecast is not None
            else planned_rop
        )
        # C11 = C10 - C12 - C13
        margin_negotiated = manager_resale_forecast - data.negotiated_price - refurb
        # C14 = C11 - C13 - fixed_1 - (C12*rate%)*multiplier - fixed_2
        gm2_negotiated = gm2(data.negotiated_price, margin_negotiated)

    return AppraisalResult(
        category=category,
        score=score,
        planned_sale_price_rop=planned_rop,
        planned_sale_price_ddc=planned_ddc,
        gross_margin_rop=gross_margin_rop,
        gross_margin_ddc=gross_margin_ddc,
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
