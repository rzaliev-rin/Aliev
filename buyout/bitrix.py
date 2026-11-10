# -*- coding: utf-8 -*-
"""
Битрикс24: когда клиент готов (выкуп или комиссия), бот создаёт в CRM рабочую
карточку — лид или сделку с контактом — и даёт на неё ссылку в рабочий чат.

Подключение — входящий вебхук Битрикс24 (права «CRM»), его адрес в .env:
  BITRIX_WEBHOOK_URL=https://ваша-компания.bitrix24.ru/rest/1/abc123xyz/
  BITRIX_ENTITY=lead            # lead — лид, deal — сделка (+ контакт клиента)
  BITRIX_DEAL_CATEGORY_ID=0     # для сделок: ID воронки (0 — общая)
  BITRIX_RESPONSIBLE_IDS=       # ID сотрудников Битрикс24 по умолчанию (через запятую)
В профиле салона можно задать своих ответственных и воронку (bitrix_responsible, bitrix_category).

Кто будет ответственным — решает pick_responsible(): сейчас по очереди из списка
профиля (или общего списка). Правило распределения меняется только здесь.
"""
import logging
import os
from typing import Optional
from urllib.parse import urlparse

import requests

from buyout import dialogs, jsonstore
from buyout.listings import Listing

logger = logging.getLogger(__name__)

STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "buyout_bitrix_state.json")
DEAL_TYPES = {"buyout": "Выкуп", "commission": "Комиссия", "unknown": "Выкуп/комиссия"}


class BitrixError(Exception):
    pass


def _cfg(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def enabled() -> bool:
    return bool(_cfg("BITRIX_WEBHOOK_URL"))


def _call(method: str, payload: dict) -> dict:
    base = _cfg("BITRIX_WEBHOOK_URL").rstrip("/") + "/"
    try:
        resp = requests.post(f"{base}{method}.json", json=payload, timeout=(10, 30))
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise BitrixError(f"Битрикс24 не ответил: {e}") from e
    if "error" in data:
        raise BitrixError(f"Битрикс24: {data.get('error_description') or data['error']}")
    return data


def _portal() -> str:
    u = urlparse(_cfg("BITRIX_WEBHOOK_URL"))
    return f"{u.scheme}://{u.netloc}"


def _ids(raw) -> list:
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or "").split(",")
    return [int(x) for x in (str(i).strip() for i in items) if x.isdigit()]


def pick_responsible(profile: dict, dialog: dict) -> Optional[int]:
    """Кому из сотрудников Битрикс24 назначить клиента.
    Сейчас: по очереди из списка профиля салона (bitrix_responsible), иначе из
    BITRIX_RESPONSIBLE_IDS. Пусто — Битрикс24 назначит владельца вебхука."""
    team = _ids(profile.get("bitrix", {}).get("responsible")) or _ids(_cfg("BITRIX_RESPONSIBLE_IDS"))
    if not team:
        return None
    state = jsonstore.load(STATE_PATH, {"next": {}})
    key = profile["id"]
    i = state["next"].get(key, 0) % len(team)
    state["next"][key] = i + 1
    jsonstore.save(STATE_PATH, state)
    return team[i]


def _phone(lead: dict) -> list:
    phone = (lead.get("phone") or "").strip()
    if not phone:
        return []
    if phone.isdigit() and len(phone) == 11:
        phone = "+" + phone
    return [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}]


def _comments(profile: dict, dialog: dict) -> str:
    listing = Listing.from_dict(dialog["listing"])
    lead = dialog.get("lead") or {}
    rows = [
        f"Тип: {DEAL_TYPES[dialogs.deal_type(dialog)]}",
        f"Салон: {profile['salon']}",
        f"Авто: {listing.summary()}",
        f"Объявление: {listing.url}",
    ]
    for label, key in (("Когда удобно", "best_time"), ("Ожидания по цене", "price_expectation"),
                       ("Состояние", "condition"), ("Резюме", "summary")):
        if lead.get(key):
            rows.append(f"{label}: {lead[key]}")
    who = {"us": "Агент", "client": "Клиент", "manager": "Менеджер"}
    rows += ["", "Переписка:"] + [f"{who.get(m['from'], m['from'])}: {m['text']}" for m in dialog["messages"]]
    rows += ["", f"ID диалога в боте: {dialog['key']}"]
    return "\n".join(rows)


def create_card(profile: dict, dialog: dict) -> str:
    """Создаёт лид или сделку и возвращает ссылку на неё."""
    lead = dialog.get("lead") or {}
    listing = Listing.from_dict(dialog["listing"])
    kind = DEAL_TYPES[dialogs.deal_type(dialog)]
    name = lead.get("client_name") or "Продавец"
    title = f"{kind}: {listing.title or listing.summary()} — {name}"[:250]
    responsible = pick_responsible(profile, dialog)
    common = {"TITLE": title, "COMMENTS": _comments(profile, dialog).replace("\n", "<br>"),
              "SOURCE_ID": _cfg("BITRIX_SOURCE_ID", "OTHER"),
              "SOURCE_DESCRIPTION": f"ИИ-агент выкупа: {listing.url}"}
    if responsible:
        common["ASSIGNED_BY_ID"] = responsible
    entity = (profile.get("bitrix", {}).get("entity") or _cfg("BITRIX_ENTITY", "lead")).lower()

    if entity == "deal":
        contact = {"NAME": name, "PHONE": _phone(lead), "SOURCE_ID": common["SOURCE_ID"]}
        if responsible:
            contact["ASSIGNED_BY_ID"] = responsible
        contact_id = _call("crm.contact.add", {"fields": contact})["result"]
        category = profile.get("bitrix", {}).get("category")
        if category in (None, ""):
            category = _cfg("BITRIX_DEAL_CATEGORY_ID", "0")
        fields = {**common, "CONTACT_ID": contact_id, "CATEGORY_ID": int(category or 0)}
        deal_id = _call("crm.deal.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}})["result"]
        return f"{_portal()}/crm/deal/details/{deal_id}/"

    fields = {**common, "NAME": name, "PHONE": _phone(lead)}
    lead_id = _call("crm.lead.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}})["result"]
    return f"{_portal()}/crm/lead/details/{lead_id}/"


def check() -> str:
    """Проверка подключения для команды /bitrix_check."""
    if not enabled():
        return "Битрикс24 не подключён: в .env нет BITRIX_WEBHOOK_URL."
    try:
        data = _call("crm.lead.fields", {})
    except BitrixError as e:
        return f"⚠️ {e}"
    users = []
    try:
        for u in _call("user.get", {"ACTIVE": True}).get("result", [])[:50]:
            users.append(f"{u.get('ID')} — {u.get('NAME', '')} {u.get('LAST_NAME', '')}".strip())
    except BitrixError:
        users.append("(список сотрудников недоступен — у вебхука нет права «Пользователи», это не обязательно)")
    ok = "✅ Битрикс24 подключён" if data.get("result") else "⚠️ Битрикс24 ответил странно"
    return (f"{ok}: {_portal()}\nСоздаём: {'сделку + контакт' if _cfg('BITRIX_ENTITY', 'lead') == 'deal' else 'лид'}\n\n"
            "Сотрудники (ID — имя), для настройки ответственных:\n" + "\n".join(users))
