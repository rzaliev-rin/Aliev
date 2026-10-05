# -*- coding: utf-8 -*-
"""
Иерархия согласования сделок — кто может нажимать «Одобрить»/«Отклонить»
в чате салона, в зависимости от статуса согласования.

Данные читаются с отдельной вкладки "Иерархия согласования" в той же
Google Таблице, что и история оценок. Формат вкладки (первая строка —
заголовки, должны совпадать буквально):

Салон | РОП ID | РОП Имя | ДДЦ ID | ДДЦ Имя | УК ID | УК Имя

"Салон" — название точно как dealer.companyName в MaxPoster (то же самое,
что вы указывали в /register_salon). ID — числовые ID пользователей MAX
(команда /myid). Имя — просто для наглядности, ботом не используется.

Правило допуска на первом этапе (в чате салона):
  - "Согласование РОП" -> РОП, ДДЦ, УК могут одобрить
  - "Согласование ДДЦ" -> ДДЦ, УК могут одобрить
  - "Согласование УК"  -> ТОЛЬКО ДДЦ может одобрить на этом этапе

Для статуса "Согласование УК" после одобрения ДДЦ идёт ВТОРОЙ этап —
персональное согласование от сотрудника с ролью УК (личным сообщением,
см. get_uk_approvers), и только после этого сделка уходит в ПАЦ. Это
сделано специально, чтобы салон не мог обойти ДДЦ и получить одобрение
повышенной цены выкупа сразу от УК.

Если салон не найден на вкладке (или вкладка ещё пустая/не создана) —
ограничение НЕ применяется (чтобы не блокировать процесс до заполнения
таблицы) — согласовать сможет любой участник чата, как раньше.
"""
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from dealer_chats import normalize_salon_name

from config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEET_ID

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TAB_NAME = "Иерархия согласования"
HEADER = ["Салон", "РОП ID", "РОП Имя", "ДДЦ ID", "ДДЦ Имя", "УК ID", "УК Имя"]

_client_cache = None


def _get_worksheet():
    global _client_cache
    if _client_cache is None:
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _client_cache = gspread.authorize(creds)

    sh = _client_cache.open_by_key(GOOGLE_SHEET_ID)
    return sh.worksheet(TAB_NAME)


def _ids_from_cell(v) -> set:
    """Разбирает ячейку с одним или несколькими ID через запятую/пробел,
    например '111111, 222222' -> {111111, 222222}. Пустая ячейка -> пустое множество."""
    if v is None:
        return set()
    text = str(v).replace(";", ",")
    ids = set()
    for part in text.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def get_first_stage_approvers(dealer_name: Optional[str], status: Optional[str]) -> set:
    """Кто может нажать «Одобрить»/«Отклонить» в чате салона (первый этап).

    Для статуса "Согласование УК" первый этап проходит ТОЛЬКО через ДДЦ —
    сам УК на этом этапе кнопку нажать не может (согласование УК — это
    отдельный, второй этап, см. get_uk_approvers), чтобы салон не мог
    обойти ДДЦ и получить согласование сразу от УК.
    """
    try:
        ws = _get_worksheet()
        rows = ws.get_all_records()
    except Exception as e:  # noqa: BLE001
        print(f"[approval_hierarchy] Не удалось прочитать вкладку «{TAB_NAME}»: {e}", flush=True)
        return set()

    for row in rows:
        if normalize_salon_name(row.get("Салон", "")) != normalize_salon_name(dealer_name):
            continue

        rop_ids = _ids_from_cell(row.get("РОП ID"))
        ddc_ids = _ids_from_cell(row.get("ДДЦ ID"))
        uk_ids = _ids_from_cell(row.get("УК ID"))

        if status == "Согласование РОП":
            return rop_ids | ddc_ids | uk_ids
        if status == "Согласование ДДЦ":
            return ddc_ids | uk_ids
        if status == "Согласование УК":
            return ddc_ids  # только ДДЦ на первом этапе!
        return rop_ids | ddc_ids | uk_ids

    return set()  # салон не найден на вкладке


def get_uk_approvers(dealer_name: Optional[str]) -> set:
    """ID сотрудников с ролью УК для данного салона (второй этап согласования
    для сделок со статусом "Согласование УК")."""
    try:
        ws = _get_worksheet()
        rows = ws.get_all_records()
    except Exception as e:  # noqa: BLE001
        print(f"[approval_hierarchy] Не удалось прочитать вкладку «{TAB_NAME}»: {e}", flush=True)
        return set()

    for row in rows:
        if normalize_salon_name(row.get("Салон", "")) != normalize_salon_name(dealer_name):
            continue
        return _ids_from_cell(row.get("УК ID"))

    return set()


def get_pats_approvers() -> set:
    """ID сотрудников, которым разрешено подтверждать приёмку в общем чате
    ПАЦ. Настраивается отдельной строкой на той же вкладке «Иерархия
    согласования»: в колонке «Салон» пишете буквально «ПАЦ», а разрешённые
    ID перечисляете в колонке «РОП ID» через запятую (остальные колонки в
    этой строке можно оставить пустыми). Пустое множество = ограничение не
    применяется (подтвердить сможет любой участник чата ПАЦ)."""
    try:
        ws = _get_worksheet()
        rows = ws.get_all_records()
    except Exception as e:  # noqa: BLE001
        print(f"[approval_hierarchy] Не удалось прочитать вкладку «{TAB_NAME}»: {e}", flush=True)
        return set()

    for row in rows:
        if normalize_salon_name(row.get("Салон", "")) == normalize_salon_name("ПАЦ"):
            return _ids_from_cell(row.get("РОП ID"))

    return set()


def resolve_role(dealer_name: Optional[str], user_id: Optional[int]) -> Optional[str]:
    """Определяет роль пользователя (РОП/ДДЦ/УК) для данного салона по таблице
    «Иерархия согласования» — используется, чтобы подписать финальный статус
    вида «Согласовано РОП» / «Согласовано ДДЦ» / «Согласовано УК»."""
    role, _ = resolve_role_and_name(dealer_name, user_id)
    return role


def resolve_role_and_name(dealer_name: Optional[str], user_id: Optional[int]) -> tuple:
    """Как resolve_role, но заодно возвращает имя из соответствующей колонки
    таблицы («РОП Имя»/«ДДЦ Имя»/«УК Имя») — на случай, если у бота нет
    актуального имени из самого MAX. Возвращает (role, name), любое из них
    может быть None."""
    if user_id is None:
        return None, None
    try:
        ws = _get_worksheet()
        rows = ws.get_all_records()
    except Exception as e:  # noqa: BLE001
        print(f"[approval_hierarchy] Не удалось прочитать вкладку «{TAB_NAME}»: {e}", flush=True)
        return None, None

    for row in rows:
        if normalize_salon_name(row.get("Салон", "")) != normalize_salon_name(dealer_name):
            continue
        if user_id in _ids_from_cell(row.get("УК ID")):
            return "УК", str(row.get("УК Имя") or "").strip() or None
        if user_id in _ids_from_cell(row.get("ДДЦ ID")):
            return "ДДЦ", str(row.get("ДДЦ Имя") or "").strip() or None
        if user_id in _ids_from_cell(row.get("РОП ID")):
            return "РОП", str(row.get("РОП Имя") or "").strip() or None
        return None, None

    return None, None


def get_approvers(dealer_name: Optional[str], status: Optional[str]) -> set:
    """Устаревшая функция (оставлена для обратной совместимости) — теперь
    используйте get_first_stage_approvers для первого этапа и get_uk_approvers
    для второго этапа при статусе "Согласование УК"."""
    return get_first_stage_approvers(dealer_name, status)
