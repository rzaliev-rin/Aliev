# -*- coding: utf-8 -*-
"""
Получение данных оценки из MaxPoster (Autohub) по ссылке, VIN или номеру
сделки. Подтверждено вживую на реальных запросах (curl) в сентябре 2026:
  - базовый адрес и путь: POST https://api.maxposter.ru/partners-api/appraisals
  - авторизация: заголовок "Authorization: Basic <token>", где <token> —
    результат base64("email:пароль") служебного аккаунта MaxPoster
  - поле "imv" = "Авито Оценка"
  - "liquidity.accidentsCount" = количество ДТП
  - "inspection.repairs[]" = расчёт стоимости ремонта (type/cost.from-to/comment)
  - "autotekaReportLinks.web" = ссылка на отчёт Автотеки
  - поля "количество окрасов" и "похоже, скручен" в этом эндпоинте НЕ
    отдаются напрямую — окрасы оцениваем эвристикой по толщине ЛКП
    (см. count_repainted_parts), а "скручен" оставляем как ссылку на
    отчёт Автотеки для ручного просмотра менеджером.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

import requests

from config import MAXPOSTER_API_BASE, MAXPOSTER_API_KEY

APPRAISAL_LINK_RE = re.compile(r"appraisals/(\d+)")
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")  # VIN: 17 симв., без I/O/Q
DEAL_ID_RE = re.compile(r"^\d{5,10}$")         # номер сделки — просто число

AVITO_PRICE_FIELD = "imv"

# Порог толщины ЛКП (мкм), выше которого деталь считается перекрашенной.
# Значение по умолчанию — обычный промышленный порог; можно скорректировать
# после сравнения с реальным числом "Окрасы" на экране MaxPoster.
REPAINT_THRESHOLD_MICRONS = 200


class MaxPosterError(Exception):
    pass


@dataclass
class AppraisalData:
    appraisal_id: str
    vin: Optional[str]
    avito_price: float              # "Авито Оценка" — прогнозная стоимость
    mileage: Optional[int]
    accidents_count: int            # количество ДТП, по данным сервиса
    repainted_parts_count: int      # оценка количества окрашенных деталей (эвристика)
    repair_cost_min: float          # сумма "от" по всем работам из inspection.repairs
    repair_cost_max: float          # сумма "до" по всem работам
    autoteka_url: Optional[str]     # ссылка на отчёт Автотеки (для "похоже, скручен" и т.п.)
    appraisal_purchase_cost: Optional[float]  # "Закупочная цена" (вкладка Оценка) — C12
    appraisal_sale_cost: Optional[float]      # "Цена продажи" (вкладка Оценка) — C10
    presale_preparation_cost: Optional[float]  # "Подготовка"/"Переподготовка" — presalePreparationCost
    brand: Optional[str]             # Марка
    model: Optional[str]             # Модель
    year: Optional[int]              # Год выпуска
    appraiser_name: Optional[str]    # Фамилия Имя оценщика
    dealer_name: Optional[str]       # Название автосалона (для маршрутизации согласования)
    raw: dict = field(repr=False)   # полный сырой ответ — на случай, если понадобятся другие поля


def extract_search_query(text: str) -> Optional[tuple]:
    """
    Определяет, что прислал менеджер: ссылку на оценку, VIN или номер сделки.
    Возвращает (тип_поля_для_фильтра, значение) либо None, если не похоже
    ни на что из этого.
    """
    text = text.strip()

    link_match = APPRAISAL_LINK_RE.search(text)
    if link_match:
        return ("id", link_match.group(1))

    if VIN_RE.match(text.upper()):
        return ("vin", text.upper())

    if DEAL_ID_RE.match(text):
        return ("id", text)

    return None


def extract_appraisal_id(text: str) -> Optional[str]:
    """Оставлено для обратной совместимости: достаёт ID только из ссылки."""
    match = APPRAISAL_LINK_RE.search(text)
    return match.group(1) if match else None


def count_repainted_parts(vehicle_part_states: list) -> int:
    """
    Количество окрашенных/перекрашенных деталей кузова. Приоритет — прямой
    тег дефекта "Покрашено", который проставляет сам осмотрщик (надёжнее
    любых догадок по цифрам). Если тега нет — запасной вариант: эвристика
    по толщине ЛКП, причём проверяем ВЕРХНЮЮ границу диапазона
    (layerThicknessTo), а не только нижнюю (layerThicknessFrom) — толщина
    часто указана диапазоном (например 100–1000 мкм на одной детали), и
    именно верхняя граница выдаёт локальный окрас.
    """
    count = 0
    for part_state in vehicle_part_states or []:
        defects = part_state.get("defects") or []
        if any(str(d.get("name", "")).strip() == "Покрашено" for d in defects):
            count += 1
            continue
        thickness_from = part_state.get("layerThicknessFrom")
        thickness_to = part_state.get("layerThicknessTo")
        candidates = [t for t in (thickness_from, thickness_to) if t is not None]
        if candidates and max(candidates) > REPAINT_THRESHOLD_MICRONS:
            count += 1
    return count


def sum_repair_costs(repairs: list) -> tuple:
    """Суммирует cost.from / cost.to по всем работам из inspection.repairs."""
    total_from = 0.0
    total_to = 0.0
    for repair in repairs or []:
        cost = repair.get("cost") or {}
        total_from += float(cost.get("from") or 0)
        total_to += float(cost.get("to") or 0)
    return total_from, total_to


def fetch_appraisal(query: str) -> AppraisalData:
    """
    Забирает данные оценки по ссылке / VIN / номеру сделки через
    партнёрский API MaxPoster (POST /partners-api/appraisals).

    query — сырой текст от менеджера; сами разбираемся, ссылка это,
    VIN или номер сделки (см. extract_search_query).
    """
    if not MAXPOSTER_API_KEY:
        raise MaxPosterError(
            "Не задан MAXPOSTER_API_KEY — интеграция с MaxPoster ещё не настроена."
        )

    parsed = extract_search_query(query)
    if parsed is None:
        raise MaxPosterError(
            "Не похоже ни на ссылку, ни на VIN, ни на номер сделки."
        )
    field_name, value = parsed

    url = f"{MAXPOSTER_API_BASE}/partners-api/appraisals"
    headers = {
        "Authorization": f"Basic {MAXPOSTER_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "limit": 1,
        "offset": 0,
        "filters": [
            {"fields": field_name, "type": "equal", "value": [str(value)]}
        ],
        "orders": ["-id"],
    }

    resp = requests.post(url, headers=headers, json=body, timeout=10)
    if resp.status_code != 200:
        raise MaxPosterError(
            f"MaxPoster API вернул {resp.status_code}: {resp.text[:300]}"
        )

    payload = resp.json()
    items = payload.get("data", {}).get("appraisals", [])
    if not items:
        raise MaxPosterError(f"Сделка по запросу «{value}» не найдена.")

    data = items[0]
    liquidity = data.get("liquidity") or {}
    inspection = data.get("inspection") or {}
    repairs = inspection.get("repairs") or []
    repair_min, repair_max = sum_repair_costs(repairs)
    autoteka = data.get("autotekaReportLinks") or {}
    brand_obj = data.get("brand") or {}
    model_obj = data.get("model") or {}
    appraiser = data.get("appraiser") or {}
    dealer_obj = data.get("dealer") or {}
    appraiser_name = " ".join(
        part for part in [appraiser.get("lastName"), appraiser.get("firstName")] if part
    ).strip() or None

    return AppraisalData(
        appraisal_id=str(data.get("id")),
        vin=data.get("vin"),
        avito_price=float(data.get(AVITO_PRICE_FIELD, 0) or 0),
        mileage=data.get("mileage"),
        accidents_count=int(liquidity.get("accidentsCount", 0) or 0),
        repainted_parts_count=count_repainted_parts(inspection.get("vehiclePartStates")),
        repair_cost_min=repair_min,
        repair_cost_max=repair_max,
        autoteka_url=autoteka.get("web"),
        appraisal_purchase_cost=(
            float(data["appraisalPurchaseCost"])
            if data.get("appraisalPurchaseCost") is not None else None
        ),
        appraisal_sale_cost=(
            float(data["appraisalSaleCost"])
            if data.get("appraisalSaleCost") is not None else None
        ),
        presale_preparation_cost=(
            float(data["presalePreparationCost"])
            if data.get("presalePreparationCost") is not None else None
        ),
        brand=brand_obj.get("name"),
        model=model_obj.get("name"),
        year=data.get("year"),
        appraiser_name=appraiser_name,
        dealer_name=dealer_obj.get("companyName"),
        raw=data,
    )
