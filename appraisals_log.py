# -*- coding: utf-8 -*-
"""
Журнал оценок для «Мои оценки» и отчёта /report.

Каждая завершённая оценка — одна запись (даже если её не отправили на
согласование). Запись обновляется по ходу согласования: кто согласовал,
корректировка, отказ, приёмка ПАЦ. Хранится в appraisals_log.json рядом
с ботом (как доступы и сделки).

outcome — где сейчас сделка:
  not_sent   расчёт без отправки на согласование
  salon      на согласовании в чате салона (РОП/ДДЦ)
  uk         ждёт решения УК
  adjusting  согласующий вводит корректировку
  pats       согласована, на проверке в ПАЦ
  accepted   ПАЦ подтвердил приёмку
  correction возвращена на корректировку
  declined   отказ в приёме
"""
import datetime
import json
import os
import uuid
from typing import Optional

STORE_PATH = os.path.join(os.path.dirname(__file__), "appraisals_log.json")
MAX_RECORDS = 5000  # старые записи отбрасываются, чтобы файл не рос бесконечно

OUTCOME_TEXT = {
    "not_sent": "не отправлялась на согласование",
    "salon": "на согласовании в чате салона",
    "uk": "ждёт решения УК",
    "adjusting": "согласующий вводит корректировку",
    "pats": "согласована, проверка в ПАЦ",
    "accepted": "принята ПАЦ",
    "correction": "возвращена на корректировку",
    "declined": "отказ в приёме",
}


def _load() -> list:
    if not os.path.exists(STORE_PATH):
        return []
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(data: list) -> None:
    # атомарная запись: при сбое посреди записи старый файл остаётся целым
    tmp_path = STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data[-MAX_RECORDS:], f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STORE_PATH)


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def add(record: dict) -> str:
    """Добавляет запись, возвращает её id."""
    data = _load()
    rec_id = uuid.uuid4().hex[:10]
    data.append({"id": rec_id, "ts": now_iso(), "outcome": "not_sent", **record})
    _save(data)
    return rec_id


def update(rec_id: Optional[str], **fields) -> None:
    if not rec_id:
        return
    data = _load()
    for rec in data:
        if rec.get("id") == rec_id:
            rec.update(fields)
            rec["updated"] = now_iso()
            _save(data)
            return


def update_by_token(token: Optional[str], **fields) -> Optional[dict]:
    """Обновляет запись сделки по токену согласования; возвращает запись."""
    if not token:
        return None
    data = _load()
    for rec in reversed(data):
        if rec.get("token") == token:
            rec.update(fields)
            rec["updated"] = now_iso()
            _save(data)
            return rec
    return None


def for_user(user_id, limit: int = 10) -> list:
    """Последние оценки пользователя, новые сверху."""
    return [r for r in reversed(_load()) if r.get("user_id") == user_id][:limit]


def since(days: int) -> list:
    border = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat(timespec="seconds")
    return [r for r in _load() if (r.get("ts") or "") >= border]
