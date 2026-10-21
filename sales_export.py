# -*- coding: utf-8 -*-
"""
Выгрузка данных для калибровки рекомендации по цене (recommend.py, версия 2).

Бот сам, своим ключом MaxPoster (из .env на сервере):
  1) выгружает оценки за период с рыночными полями (Авито-оценка и коридор,
     ликвидность, ПЦП Автохаб, цены выкупа/продажи из вкладки «Оценка», статус);
  2) проверяет, какие данные о складе и продажах отдаёт ваш доступ к API
     (цена выставления, фактическая цена и дата продажи) — пробует несколько
     адресов API и записывает, что ответил каждый;
  3) если склад доступен — подтягивает по каждой выкупленной оценке её авто
     (vehicleId) и добавляет поля с ценами и датами.
Результат — CSV для Excel и отчёт о проверке (JSON). Персональные данные
клиентов (ФИО, телефоны, e-mail) в выгрузку не попадают.
"""
import csv
import datetime
import json
import os
import re

import requests

import maxposter_client as mc

EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")
MAX_PAGES = 150  # до 15 000 оценок за выгрузку
VEHICLE_ENDPOINTS = [("POST", "/partners-api/vehicles"), ("POST", "/partners-api/vehicles/search"),
                     ("GET", "/partners-api/vehicles"), ("POST", "/partners-api/stock"),
                     ("POST", "/partners-api/sales"), ("POST", "/partners-api/deals")]
PERSONAL = re.compile(r"owner|phone|email|firstName|lastName|middleName|inn|birth|passport|customer|client",
                      re.IGNORECASE)
VEHICLE_FIELDS = re.compile(r"price|cost|date|status|sold|sale|days|createdAt|updatedAt|stock|id$",
                            re.IGNORECASE)


def _flatten(obj, prefix="", out=None, depth=0):
    out = {} if out is None else out
    if isinstance(obj, dict) and depth < 3:
        for k, v in obj.items():
            _flatten(v, f"{prefix}{k}.", out, depth + 1)
    elif isinstance(obj, list):
        out[prefix[:-1]] = f"[{len(obj)}]"
    else:
        out[prefix[:-1]] = obj
    return out


def _headers():
    return {"Authorization": f"Basic {mc.MAXPOSTER_API_KEY}", "Content-Type": "application/json"}


def probe_vehicle_endpoints() -> list:
    """Что отвечает API на запросы склада/продаж: код ответа, ключи, пример полей (без перс. данных)."""
    report = []
    for method, path in VEHICLE_ENDPOINTS:
        url = mc.MAXPOSTER_API_BASE + path
        try:
            if method == "POST":
                resp = requests.post(url, headers=_headers(), timeout=30,
                                     json={"limit": 1, "offset": 0, "filters": [], "orders": ["-id"]})
            else:
                resp = requests.get(url, headers=_headers(), timeout=30, params={"limit": 1})
            entry = {"method": method, "path": path, "status": resp.status_code}
            try:
                body = resp.json()
            except ValueError:
                entry["body"] = resp.text[:200]
                report.append(entry)
                continue
            data = body.get("data") if isinstance(body, dict) else None
            entry["top_keys"] = list(body.keys())[:20] if isinstance(body, dict) else type(body).__name__
            items = None
            if isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        items = v
                        break
            elif isinstance(data, list):
                items = data
            if items:
                flat = _flatten(items[0])
                entry["fields"] = {k: (str(v)[:40] if VEHICLE_FIELDS.search(k) else "…")
                                   for k, v in flat.items() if not PERSONAL.search(k)}
        except Exception as e:  # noqa: BLE001
            entry = {"method": method, "path": path, "error": str(e)[:200]}
        report.append(entry)
    return report


def _working_vehicle_endpoint(report: list):
    for e in report:
        if e.get("status") == 200 and e.get("fields") and e["method"] == "POST":
            return e["path"]
    return None


def fetch_vehicles(path: str, ids: list) -> dict:
    """{vehicle_id: плоский словарь полей с ценами/датами} — пачками по 50."""
    result = {}
    for i in range(0, len(ids), 50):
        chunk = [str(x) for x in ids[i:i + 50]]
        try:
            resp = requests.post(mc.MAXPOSTER_API_BASE + path, headers=_headers(), timeout=30,
                                 json={"limit": 50, "offset": 0, "orders": ["-id"],
                                       "filters": [{"fields": "id", "type": "equal", "value": chunk}]})
            if resp.status_code != 200:
                break
            data = resp.json().get("data") or {}
            items = next((v for v in data.values() if isinstance(v, list)), []) if isinstance(data, dict) else data
            for item in items or []:
                flat = _flatten(item)
                result[str(item.get("id"))] = {f"vehicle.{k}": v for k, v in flat.items()
                                               if VEHICLE_FIELDS.search(k) and not PERSONAL.search(k)}
        except Exception:  # noqa: BLE001
            break
    return result


APPRAISAL_COLUMNS = [
    ("id", "id"), ("created", "createdAt"), ("dealer_id", "dealer.id"), ("dealer", "dealer.companyName"),
    ("brand", "brand.name"), ("model", "model.name"), ("year", "year"), ("mileage", "mileage"),
    ("vin", "vin"), ("acquisition", "acquisitionSource"), ("lead_source", "leadSource.name"),
    ("status", "status.name"), ("imv", "imv"), ("min_imv", "minImv"), ("max_imv", "maxImv"),
    ("avg_market_price", "avgMarketPrice"), ("autohub_ptsp", "maxposterSaleCost"),
    ("purchase_cost", "appraisalPurchaseCost"), ("sale_cost", "appraisalSaleCost"),
    ("prep_cost", "presalePreparationCost"), ("competitors", "numberOfCompetitors"),
    ("avg_sale_days", "liquidity.averageSalePeriod"), ("liquidity_pct90", "liquidity.liquidityPercent90"),
    ("market_sales90", "liquidity.completedMarketSales90"), ("market_total90", "liquidity.totalMarketSales90"),
    ("holding_sales", "liquidity.holdingSales"), ("accidents", "liquidity.accidentsCount"),
    ("owners", "liquidity.ownersCount"), ("avg_market_mileage", "liquidity.averageMarketMileage"),
    ("total_rating", "liquidity.totalRating.rating"), ("final_category", "finalVehicleCategory.scoring"),
    ("completion_date", "completionDate"), ("vehicle_id", "vehicleId"), ("actual_vehicle_id", "actualVehicleId"),
]


def run_export(days: int = 180) -> tuple:
    """Возвращает (путь к CSV, путь к отчёту JSON, краткий итог для сообщения)."""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    # сразу превращаем каждую оценку в строку — полные ответы (с фото осмотра) в памяти не держим
    rows = mc.list_appraisals(None, start, end, max_pages=MAX_PAGES,
                              transform=lambda it: {col: mc.get_path(it, p) for col, p in APPRAISAL_COLUMNS})
    items = rows
    report = {"period": [start.isoformat(), end.isoformat()], "appraisals": len(rows),
              "vehicle_endpoints": probe_vehicle_endpoints()}

    path = _working_vehicle_endpoint(report["vehicle_endpoints"])
    vehicle_ids = sorted({str(r["vehicle_id"] or r["actual_vehicle_id"]) for r in rows
                          if r["vehicle_id"] or r["actual_vehicle_id"]})
    report["appraisals_with_vehicle"] = len(vehicle_ids)
    vehicles = fetch_vehicles(path, vehicle_ids) if path and vehicle_ids else {}
    report["vehicles_fetched"] = len(vehicles)
    extra_cols = sorted({k for v in vehicles.values() for k in v})
    for r in rows:
        r.update(vehicles.get(str(r["vehicle_id"] or r["actual_vehicle_id"]), {}))

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = os.path.join(EXPORT_DIR, f"maxposter_sales_{stamp}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[c for c, _ in APPRAISAL_COLUMNS] + extra_cols,
                                delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    json_path = os.path.join(EXPORT_DIR, f"maxposter_probe_{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=str)

    ok = [e["path"] for e in report["vehicle_endpoints"] if e.get("status") == 200]
    summary = (f"Оценок за {days} дн.: {len(items)}; с привязкой к авто на складе: {len(vehicle_ids)}; "
               f"данных по авто получено: {len(vehicles)}.\n"
               + (f"API склада ответил: {', '.join(ok)}" if ok else "API склада/продаж по проверенным адресам не ответил."))
    return csv_path, json_path, summary
