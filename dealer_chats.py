# -*- coding: utf-8 -*-
"""
Соответствие названия автосалона (поле dealer.companyName из MaxPoster) —
чату в MAX, куда бот постит карточку согласования. Плюс отдельный общий
чат "ПАЦ" на всю компанию.

Настраивается командами в самом боте (см. bot.py):
  - /register_salon <Название салона как в MaxPoster>  — выполнить в рабочем чате салона
  - /register_pats                                      — выполнить в чате ПАЦ
"""
import json
import os
import re
from typing import Optional

STORE_PATH = os.path.join(os.path.dirname(__file__), "dealer_chats.json")

_DEFAULT = {"salons": {}, "pats_chat_id": None}


def normalize_salon_name(name) -> str:
    """Приводит название салона к сравнимому виду: убирает ведущий числовой
    код вроде "3047: " и ведущие "!" (в любом порядке и по несколько раз),
    регистр не важен. Нужно потому, что название, под которым вы
    регистрируете чат (/register_salon), и то, что реально приходит от
    MaxPoster в поле dealer.companyName, иногда не совпадают буква в букву —
    например, с номером и без него."""
    if not name:
        return ""
    s = str(name).strip()
    while True:
        new_s = re.sub(r"^!+\s*", "", s)
        new_s = re.sub(r"^\d+\s*[:\-–]\s*", "", new_s)
        new_s = new_s.strip()
        if new_s == s:
            break
        s = new_s
    return s.lower()


def _load() -> dict:
    if not os.path.exists(STORE_PATH):
        return dict(_DEFAULT)
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("salons", {})
        data.setdefault("pats_chat_id", None)
        return data
    except Exception:  # noqa: BLE001
        return dict(_DEFAULT)


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


def set_salon_chat_id(dealer_name: str, chat_id: int) -> None:
    data = _load()
    data["salons"][dealer_name] = chat_id
    _save(data)


def get_salon_chat_id(dealer_name: Optional[str]) -> Optional[int]:
    if not dealer_name:
        return None
    target = normalize_salon_name(dealer_name)
    if not target:
        return None
    salons = _load()["salons"]
    # сначала пробуем точное совпадение (быстро и однозначно)
    if dealer_name in salons:
        return salons[dealer_name]
    # затем — по нормализованному названию (без номера/регистра)
    for name, chat_id in salons.items():
        if normalize_salon_name(name) == target:
            return chat_id
    return None


def set_pats_chat_id(chat_id: int) -> None:
    data = _load()
    data["pats_chat_id"] = chat_id
    _save(data)


def get_pats_chat_id() -> Optional[int]:
    return _load()["pats_chat_id"]


def list_salons() -> dict:
    return _load()["salons"]
