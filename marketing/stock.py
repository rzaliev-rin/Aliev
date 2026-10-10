# -*- coding: utf-8 -*-
"""
Склад новых автомобилей салона из MaxPoster (по умолчанию 13463: ПИ Toyota
Екатеринбург).

Источники (по приоритету):
  1. --stock-file: JSON (ответ API или список машин) / CSV (колонки brand,
     model, year, price, complectation, ...) / XML-фид MaxPoster;
  2. партнёрский API MaxPoster: POST {MAXPOSTER_VEHICLES_PATH} с тем же
     форматом фильтров, что и подтверждённый /partners-api/appraisals.
     Путь метода склада вживую не проверен — если API ответит 404,
     поправьте MAXPOSTER_VEHICLES_PATH в .env по partners-api-docs.
"""
import csv
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional

import requests

from marketing import settings


class StockError(Exception):
    pass


@dataclass
class Vehicle:
    brand: str
    model: str
    year: Optional[int] = None
    price: Optional[float] = None
    complectation: Optional[str] = None
    engine: Optional[str] = None
    body: Optional[str] = None
    color: Optional[str] = None
    vin: Optional[str] = None
    is_new: bool = True


@dataclass
class ModelGroup:
    """Модель на складе: сколько машин, вилка цен, комплектации."""
    brand: str
    model: str
    count: int = 0
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    years: set = field(default_factory=set)
    complectations: set = field(default_factory=set)
    bodies: set = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.brand} {self.model}".strip()


def _pick(item: dict, *paths: str) -> Any:
    for path in paths:
        cur: Any = item
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, dict):  # {"id": .., "name": ..}
            cur = cur.get("name") or cur.get("title")
        if cur not in (None, ""):
            return cur
    return None


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def vehicle_from_dict(item: dict) -> Optional[Vehicle]:
    brand = _pick(item, "brand", "mark", "make", "vehicle.brand")
    model = _pick(item, "model", "vehicle.model", "folder")
    if not brand or not model:
        return None
    condition = str(_pick(item, "condition", "state", "isNew", "mileageType") or "new").lower()
    mileage = _num(_pick(item, "mileage", "run", "vehicle.mileage"))
    is_new = condition in ("new", "true", "1", "новый", "новое") or (mileage is not None and mileage < 1000)
    year = _num(_pick(item, "year", "vehicle.year"))
    return Vehicle(
        brand=str(brand).strip(),
        model=str(model).strip(),
        year=int(year) if year else None,
        price=_num(_pick(item, "price", "priceWithDiscount", "salePrice", "cost")),
        complectation=_pick(item, "complectation", "equipment", "complectation.name", "modification"),
        engine=_pick(item, "engine", "modification.engine", "engineType"),
        body=_pick(item, "body", "bodyType", "body_type"),
        color=_pick(item, "color", "colour"),
        vin=_pick(item, "vin"),
        is_new=is_new,
    )


def _items_from_json(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, list):
            return data
        for key in ("vehicles", "cars", "items", "offers", "stock"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _items_from_xml(text: str) -> List[dict]:
    root = ET.fromstring(text)
    items = []
    for node in root.iter():
        if node.tag.lower() in ("car", "vehicle", "offer", "item") and len(node):
            items.append({child.tag: (child.text or "").strip() for child in node})
    return items


def load_stock_file(path: str) -> List[Vehicle]:
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        rows = list(csv.DictReader(text.splitlines(), delimiter=";" if text.count(";") > text.count(",") else ","))
    elif ext == ".xml" or text.lstrip().startswith("<"):
        rows = _items_from_xml(text)
    else:
        rows = _items_from_json(json.loads(text))
    return [v for v in (vehicle_from_dict(r) for r in rows) if v]


def fetch_stock_api(dealer_id: str = settings.MARKETING_DEALER_ID, page_size: int = 100) -> List[Vehicle]:
    if not settings.MAXPOSTER_API_KEY:
        raise StockError("Не задан MAXPOSTER_API_KEY — укажите ключ или передайте --stock-file.")
    url = settings.MAXPOSTER_API_BASE + settings.MAXPOSTER_VEHICLES_PATH
    headers = {"Authorization": f"Basic {settings.MAXPOSTER_API_KEY}", "Content-Type": "application/json"}
    vehicles: List[Vehicle] = []
    offset = 0
    while True:
        body = {
            "limit": page_size,
            "offset": offset,
            "filters": [{"fields": settings.MAXPOSTER_DEALER_FIELD, "type": "equal", "value": [str(dealer_id)]}],
            "orders": ["-id"],
        }
        resp = requests.post(url, headers=headers, json=body, timeout=20)
        if resp.status_code != 200:
            raise StockError(f"MaxPoster {url} вернул {resp.status_code}: {resp.text[:300]}")
        items = _items_from_json(resp.json())
        vehicles += [v for v in (vehicle_from_dict(i) for i in items) if v]
        if len(items) < page_size:
            return vehicles
        offset += page_size


def normalize_model(name: str) -> str:
    """'LAND CRUISER PRADO 250' -> 'Land Cruiser Prado 250', 'rav 4' -> 'RAV4'."""
    text = re.sub(r"\s+", " ", name).strip()
    text = re.sub(r"(?i)^rav\s*-?\s*4$", "RAV4", text)
    return text if any(c.islower() for c in text) or text.isdigit() or len(text) <= 4 else text.title()


def group_by_model(vehicles: Iterable[Vehicle], only_new: bool = True) -> List[ModelGroup]:
    groups: dict = {}
    for v in vehicles:
        if only_new and not v.is_new:
            continue
        key = (v.brand.title(), normalize_model(v.model))
        g = groups.setdefault(key, ModelGroup(brand=key[0], model=key[1]))
        g.count += 1
        if v.price:
            g.price_min = v.price if g.price_min is None else min(g.price_min, v.price)
            g.price_max = v.price if g.price_max is None else max(g.price_max, v.price)
        for attr, bucket in ((v.year, g.years), (v.complectation, g.complectations), (v.body, g.bodies)):
            if attr:
                bucket.add(attr)
    return sorted(groups.values(), key=lambda g: -g.count)
