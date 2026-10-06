# -*- coding: utf-8 -*-
"""
Запись истории оценок в Google Таблицу.

Настройка (один раз):
  1. Создайте проект в Google Cloud Console, включите Google Sheets API.
  2. Создайте сервисный аккаунт, скачайте JSON-ключ -> сохраните как
     service_account.json рядом с ботом.
  3. Создайте Google Таблицу, откройте доступ на редактирование для email
     сервисного аккаунта (он в JSON-ключе, поле client_email).
  4. Скопируйте ID таблицы из её URL и укажите в переменной окружения
     GOOGLE_SHEET_ID (пример URL: docs.google.com/spreadsheets/d/ЭТОТ_ID/edit).
"""
import datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEET_ID, GOOGLE_SHEET_TAB_V2
from pricing_config import METRIC_VERSION

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Вкладка истории для метрики v2.0. Старая вкладка (GOOGLE_SHEET_TAB, метрика
# v1) не трогается: у v2 другой смысл колонок (ПЦП по городу, потолок и т.д.),
# и смешивать строки двух метрик в одной вкладке нельзя.
TAB_NAME = GOOGLE_SHEET_TAB_V2

HEADER = [
    "Дата/время", "Менеджер (MAX)", "Салон", "Город", "VIN", "Марка", "Модель",
    "Год выпуска", "Пробег", "Пробег в год", "Оценщик", "Тип приёма",
    "Авито оценка", "Окрасы", "Тех.состояние", "Доп.данные", "Категория", "Баллы",
    "ПЦП (РОП)", "ПЦП (ДДЦ)", "Плановая ВП", "Потолок к Авито", "Лимит РОП", "Предел ДДЦ",
    "Переподготовка", "GM2 по метрике", "GM2 (ДДЦ)",
    "Прогноз ДЦ: цена продажи", "Прогноз ДЦ: закупка", "ВП по прогнозу ДЦ", "GM2 по прогнозу ДЦ",
    "Статус согласования", "Версия метрики",
]

_client_cache = None


def _get_worksheet():
    global _client_cache
    if _client_cache is None:
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _client_cache = gspread.authorize(creds)

    sh = _client_cache.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(TAB_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=TAB_NAME, rows=1000, cols=len(HEADER))
        ws.append_row(HEADER)
    return ws


def _blank_if_none(v):
    return "" if v is None else v


def log_appraisal(manager_name: str, vin: Optional[str], data, result, vehicle_info: Optional[dict] = None) -> None:
    """
    data — AppraisalInput, result — AppraisalResult (см. pricing_engine.py)
    vehicle_info — необязательный словарь с ключами brand/model/year/mileage/appraiser_name
    (данные по самому автомобилю и оценщику, не связанные с расчётом цены).
    Любая ошибка записи в таблицу не должна ронять бот — только логируется.
    """
    vehicle_info = vehicle_info or {}
    try:
        ws = _get_worksheet()
        ws.append_row([
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            manager_name,
            vehicle_info.get("dealer_name") or "",
            getattr(data, "city", None) or "",
            vin or "",
            vehicle_info.get("brand") or "",
            vehicle_info.get("model") or "",
            data.year or "",
            data.mileage if data.mileage is not None else "",
            result.mileage_per_year,
            vehicle_info.get("appraiser_name") or "",
            data.reception_type,
            data.avito_price,
            data.colors,
            data.condition,
            data.extra,
            result.category,
            result.score,
            result.planned_sale_price_rop,
            result.planned_sale_price_ddc,
            result.gross_margin_rop,
            result.ceiling_price,
            result.purchase_price_rop,
            result.purchase_price_ddc,
            result.refurbishment,
            result.gm2_rop,
            result.gm2_ddc,
            _blank_if_none(result.manager_resale_forecast),
            _blank_if_none(result.negotiated_price),
            _blank_if_none(result.margin_negotiated),
            _blank_if_none(result.gm2_negotiated),
            result.approval_status or "",
            METRIC_VERSION,
        ], value_input_option="USER_ENTERED")
        print("[sheets_logger] Строка успешно записана в Google Таблицу", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[sheets_logger] Не удалось записать в Google Таблицу: {e}", flush=True)
