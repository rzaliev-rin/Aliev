# -*- coding: utf-8 -*-
"""
Временное хранилище сделок в процессе согласования (Этап 1):
  salon_pending -> (после одобрения в чате салона) -> pats_pending -> pats_done

Каждая сделка хранится под коротким токеном, который зашивается в payload
инлайн-кнопок, чтобы бот понимал, к какой именно сделке относится нажатие.
"""
import json
import os
import uuid
from typing import Optional

STORE_PATH = os.path.join(os.path.dirname(__file__), "deals_store.json")


def _load() -> dict:
    if not os.path.exists(STORE_PATH):
        return {}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save(data: dict) -> None:
    # Пишем во временный файл и подменяем атомарно: если бот упадёт или
    # закончится место посреди записи, старый файл останется целым
    # (иначе _load прочитает битый JSON как пустой и следующая запись
    # сотрёт все данные).
    tmp_path = STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STORE_PATH)


def create_deal(record: dict) -> str:
    token = uuid.uuid4().hex[:8]
    data = _load()
    data[token] = record
    _save(data)
    return token


def get_deal(token: str) -> Optional[dict]:
    return _load().get(token)


def update_deal(token: str, **kwargs) -> None:
    data = _load()
    if token in data:
        data[token].update(kwargs)
        _save(data)


def find_awaiting(user_id: int):
    """Возвращает (token, deal) для сделки, где именно этот пользователь должен
    сейчас ввести скорректированную цену/ПЦП, либо (None, None)."""
    data = _load()
    for token, deal in data.items():
        if deal.get("awaiting_input_from") == user_id:
            return token, deal
    return None, None


def delete_deal(token: str) -> None:
    data = _load()
    data.pop(token, None)
    _save(data)
