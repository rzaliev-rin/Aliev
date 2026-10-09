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

Правило допуска на первом этапе (в чате салона), статусы метрики v2.0:
  - "Согласовано"                     -> РОП, ДДЦ, УК могут одобрить
  - "Согласование ДДЦ"                -> ДДЦ, УК могут одобрить
  - "Комиссия: подтверждение РОП"     -> РОП, ДДЦ, УК могут подтвердить

Статусы "Согласование УК", "Согласование УК (выше потолка)", "Не принимать"
в чат салона на одобрение РОП/ДДЦ НЕ идут: карточка сразу уходит УК
(лично и в чат салона, см. get_uk_approvers), решает только УК, затем ПАЦ.

Если салон не найден на вкладке (или вкладка ещё пустая/не создана) —
функции возвращают пустое множество, и тогда в bot.py (_can_approve)
согласовать могут только администраторы бота. Раньше в этом случае
кнопку мог нажать любой участник чата.

Вкладка читается не чаще раза в ROWS_CACHE_TTL_SECONDS: раньше каждое
нажатие кнопки делало 2–3 запроса к Google и на это время бот «замирал».
Если Google временно недоступен — используются последние прочитанные данные.
"""
import time
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from dealer_chats import normalize_salon_name
from pricing_engine import STATUS_DDC, UK_STATUSES

from config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEET_ID

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TAB_NAME = "Иерархия согласования"
HEADER = ["Салон", "РОП ID", "РОП Имя", "ДДЦ ID", "ДДЦ Имя", "УК ID", "УК Имя"]

ROWS_CACHE_TTL_SECONDS = 60

_client_cache = None
_rows_cache = None
_rows_cache_at = 0.0


def _get_worksheet():
    global _client_cache
    if _client_cache is None:
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _client_cache = gspread.authorize(creds)

    sh = _client_cache.open_by_key(GOOGLE_SHEET_ID)
    return sh.worksheet(TAB_NAME)


def _get_rows() -> list:
    """Строки вкладки с кешем. Бросает исключение, только если вкладку не
    удалось прочитать ни разу с момента запуска бота."""
    global _rows_cache, _rows_cache_at
    now = time.time()
    if _rows_cache is not None and (now - _rows_cache_at) < ROWS_CACHE_TTL_SECONDS:
        return _rows_cache
    try:
        rows = _get_worksheet().get_all_records()
    except Exception as e:  # noqa: BLE001
        if _rows_cache is None:
            raise
        print(f"[approval_hierarchy] Не удалось обновить вкладку «{TAB_NAME}», "
              f"использую данные из кеша: {e}", flush=True)
        _rows_cache_at = now  # не долбим Google на каждом нажатии, повторим через TTL
        return _rows_cache
    _rows_cache, _rows_cache_at = rows, now
    return rows


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

    Новые сделки со статусами УК этот этап не проходят (сразу к УК);
    для таких сделок из прежней версии, ждущих в чате, — ДДЦ или УК.
    """
    try:
        rows = _get_rows()
    except Exception as e:  # noqa: BLE001
        print(f"[approval_hierarchy] Не удалось прочитать вкладку «{TAB_NAME}»: {e}", flush=True)
        return set()

    for row in rows:
        if normalize_salon_name(row.get("Салон", "")) != normalize_salon_name(dealer_name):
            continue

        rop_ids = _ids_from_cell(row.get("РОП ID"))
        ddc_ids = _ids_from_cell(row.get("ДДЦ ID"))
        uk_ids = _ids_from_cell(row.get("УК ID"))

        if status == STATUS_DDC:
            return ddc_ids | uk_ids
        if status in UK_STATUSES:
            return ddc_ids | uk_ids  # старые сделки, ждавшие ДДЦ; новые идут сразу к УК
        return rop_ids | ddc_ids | uk_ids  # "Согласовано" (и старое "Согласование РОП")

    return set()  # салон не найден на вкладке


def get_uk_approvers(dealer_name: Optional[str]) -> set:
    """ID сотрудников с ролью УК для данного салона (второй этап согласования
    для сделок со статусом "Согласование УК")."""
    try:
        rows = _get_rows()
    except Exception as e:  # noqa: BLE001
        print(f"[approval_hierarchy] Не удалось прочитать вкладку «{TAB_NAME}»: {e}", flush=True)
        return set()

    for row in rows:
        if normalize_salon_name(row.get("Салон", "")) != normalize_salon_name(dealer_name):
            continue
        return _ids_from_cell(row.get("УК ID"))

    return set()


def get_role_ids(dealer_name: Optional[str], role: str) -> set:
    """ID сотрудников роли «РОП» / «ДДЦ» / «УК» в салоне (для утренней сводки)."""
    column = {"РОП": "РОП ID", "ДДЦ": "ДДЦ ID", "УК": "УК ID"}[role]
    try:
        rows = _get_rows()
    except Exception as e:  # noqa: BLE001
        print(f"[approval_hierarchy] Не удалось прочитать вкладку «{TAB_NAME}»: {e}", flush=True)
        return set()
    for row in rows:
        if normalize_salon_name(row.get("Салон", "")) == normalize_salon_name(dealer_name):
            return _ids_from_cell(row.get(column))
    return set()


def get_pats_approvers() -> set:
    """ID сотрудников, которым разрешено подтверждать приёмку в общем чате
    ПАЦ. Настраивается отдельной строкой на той же вкладке «Иерархия
    согласования»: в колонке «Салон» пишете буквально «ПАЦ», а разрешённые
    ID перечисляете в колонке «РОП ID» через запятую (остальные колонки в
    этой строке можно оставить пустыми). Пустое множество = ограничение не
    применяется (подтвердить сможет любой участник чата ПАЦ)."""
    try:
        rows = _get_rows()
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
        rows = _get_rows()
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
