# -*- coding: utf-8 -*-
"""
Журнал переписок: кому написали, что ответили, на какой стадии диалог.
Нужен, чтобы никогда не писать одному продавцу дважды и чтобы агент
видел всю историю, когда продавец отвечает через день.
"""
import datetime
import os
from typing import Optional

from buyout import jsonstore
from buyout.listings import Listing

STORE_PATH = os.environ.get(
    "BUYOUT_DIALOGS_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "buyout_dialogs.json"))

# Статусы диалога
ACTIVE = "active"            # переписка идёт
READY = "ready"              # клиент готов к выкупу — передан в рабочий чат
DECLINED = "declined"        # отказался — больше не пишем
NOT_RELEVANT = "not_relevant"  # продано / перекуп / не та машина
NEED_HUMAN = "need_human"    # агенту нужен живой менеджер (вопрос, который он не может решить)
CLOSED = (READY, DECLINED, NOT_RELEVANT)

STATUS_TITLES = {ACTIVE: "в диалоге", READY: "готов к выкупу", DECLINED: "отказ",
                 NOT_RELEVANT: "неактуально", NEED_HUMAN: "нужен менеджер"}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    data = jsonstore.load(STORE_PATH, {"dialogs": {}, "fingerprints": {}, "seen": {}})
    for k in ("dialogs", "fingerprints", "seen"):
        data.setdefault(k, {})
    return data


def get(key: str) -> Optional[dict]:
    return _load()["dialogs"].get(key)


def all_dialogs(profile_id: Optional[str] = None) -> list:
    items = _load()["dialogs"].values()
    return [d for d in items if profile_id is None or d["profile_id"] == profile_id]


def already_contacted(listing: Listing) -> bool:
    data = _load()
    if listing.key in data["dialogs"]:
        return True
    fp = listing.fingerprint()
    if fp and fp in data["fingerprints"]:
        return True
    if listing.seller_id:
        seller = f"{listing.platform}:seller:{listing.seller_id}"
        if seller in data["fingerprints"]:
            return True
    return False


def mark_seen(listing: Listing, reason: str) -> None:
    """Объявление отсеяно фильтром — запоминаем, чтобы не проверять заново каждый круг."""
    data = _load()
    data["seen"][listing.key] = {"at": _now(), "reason": reason}
    jsonstore.save(STORE_PATH, data)


def is_seen(listing: Listing) -> bool:
    return listing.key in _load()["seen"]


def start(profile_id: str, listing: Listing, first_message: str, account: str) -> dict:
    data = _load()
    dialog = {
        "key": listing.key,
        "profile_id": profile_id,
        "account": account,
        "listing": listing.to_dict(),
        "status": ACTIVE,
        "messages": [{"from": "us", "text": first_message, "at": _now()}],
        "lead": {},
        "created": _now(),
        "updated": _now(),
        "handed_off": False,
        "followups": 0,
    }
    data["dialogs"][listing.key] = dialog
    fp = listing.fingerprint()
    if fp:
        data["fingerprints"][fp] = listing.key
    if listing.seller_id:
        data["fingerprints"][f"{listing.platform}:seller:{listing.seller_id}"] = listing.key
    jsonstore.save(STORE_PATH, data)
    return dialog


def add_message(key: str, sender: str, text: str) -> dict:
    data = _load()
    dialog = data["dialogs"][key]
    dialog["messages"].append({"from": sender, "text": text, "at": _now()})
    dialog["updated"] = _now()
    jsonstore.save(STORE_PATH, data)
    return dialog


def update(key: str, **fields) -> dict:
    data = _load()
    dialog = data["dialogs"][key]
    dialog.update(fields)
    dialog["updated"] = _now()
    jsonstore.save(STORE_PATH, data)
    return dialog


def first_messages_today(account: str) -> int:
    today = datetime.date.today().isoformat()
    return sum(1 for d in _load()["dialogs"].values()
               if d.get("account") == account and d["created"].startswith(today))


def stats(profile_id: Optional[str] = None) -> dict:
    out = {s: 0 for s in STATUS_TITLES}
    replied = 0
    for d in all_dialogs(profile_id):
        out[d["status"]] = out.get(d["status"], 0) + 1
        if any(m["from"] == "client" for m in d["messages"]):
            replied += 1
    out["total"] = sum(out[s] for s in STATUS_TITLES)
    out["replied"] = replied
    return out
