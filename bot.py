# -*- coding: utf-8 -*-
"""
Бот для MAX: расчёт выкупной цены авто в трейд-ин.
Написан на pymaxgram (актуальная асинхронная библиотека под MAX Bot API).

Сценарий диалога:
  1. Менеджер присылает ссылку на оценку из MaxPoster, VIN или номер сделки
     (или пишет "вручную").
  2. Бот подтягивает по API: Авито-оценку, число ДТП, количество окрасов
     (эвристика по толщине ЛКП), стоимость ремонта, ссылку на отчёт
     Автотеки, а также — если они уже заполнены во вкладке "Оценка" —
     закупочную цену и цену продажи.
       - Если ДТП > 3 или окрасов больше 1 — соответствующие шаги выбора
         заполняются автоматически и не спрашиваются у менеджера.
       - Если закупочная цена и/или цена продажи уже есть в системе —
         соответствующие вопросы в конце диалога пропускаются (менеджер
         может явно их переопределить, введя новое число).
  3. Бот пошагово спрашивает: тип приёма, (окрасы — если не удалось
     определить по API), тех. состояние, доп. данные (если ДТП ≤ 3).
  4. Показывает полную раскладку: категория, план.цена продажи, валовая
     прибыль (маржа/GM1), потолки закупки (РОП/ДДЦ), GM2, стоимость
     ремонта, ссылку на Автотеку.
  5. При необходимости спрашивает планируемую цену продажи и/или цену,
     согласованную с клиентом -> считает статус согласования.
  6. Пишет запись в Google Таблицу для отчётности.

Запуск: python bot.py  (long polling, доп. настройка вебхука не нужна)
"""
import asyncio
import datetime
import logging
import time
from typing import Optional
from types import SimpleNamespace

from dotenv import load_dotenv
load_dotenv()

from maxgram import Bot, Dispatcher, F, Router
from maxgram.filters import Command, CommandObject, StateFilter
from maxgram.fsm.state import State, StatesGroup
from maxgram.fsm.context import FSMContext
from maxgram.types import BotStarted
from maxgram.utils.keyboard import InlineKeyboardBuilder

from config import (MAX_BOT_TOKEN, ALLOWED_USER_IDS, ADMIN_USER_IDS, OWNER_USER_ID,
                    REMINDER_AFTER_MINUTES, REMINDER_HOURS)
from pricing_engine import (
    AppraisalInput, calc_appraisal, colors_bucket_from_count,
    get_colors_options, get_condition_options, get_extra_options, get_salon_options, RECEPTION_TYPES,
    find_salon, salon_label, is_young_car, UK_STATUSES, STATUS_REJECT,
    STATUS_OK, STATUS_DDC, STATUS_UK, STATUS_UK_CEILING,
    EXTRA_YOUNG_CAR, EXTRA_NOT_APPLICABLE, METRIC_VERSION,
)
from maxposter_client import fetch_appraisal, MaxPosterError
from sheets_logger import log_appraisal
import access_store
import appraisals_log
import dealer_chats
import deals_store
import approval_hierarchy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not MAX_BOT_TOKEN:
    raise SystemExit("Не задан MAX_BOT_TOKEN (переменная окружения). См. README.md")

bot = Bot(token=MAX_BOT_TOKEN)
dp = Dispatcher()
router = Router()

ACCIDENTS_AUTO_THRESHOLD = 3  # выше этого — автоматически "Более 3х ДТП"
ACCIDENTS_AUTO_OPTION = "Более 3х ДТП"  # должно совпадать с пунктом «Доп.данные» в таблице


class AppraisalStates(StatesGroup):
    waiting_link = State()
    manual_vin = State()
    manual_avito = State()
    reception_type = State()
    salon = State()
    year = State()
    mileage = State()
    colors = State()
    condition = State()
    extra = State()
    waiting_planned_sale_price = State()
    waiting_negotiated = State()
    reviewing_result = State()


def _fmt(n) -> str:
    if n is None:
        return "—"
    return f"{n:,.0f}".replace(",", " ")


def _text(message) -> str:
    """Текст сообщения; пустая строка, если прислали фото/файл/стикер без текста."""
    body = getattr(message, "body", None)
    return (getattr(body, "text", None) or "").strip()


def _parse_positive(text: str) -> float:
    """Число > 0 из строки вида '1 200 000' или '950000,5'. Бросает ValueError, если не число или ≤ 0."""
    value = float((text or "").strip().replace(" ", "").replace(" ", "").replace(",", "."))
    if value <= 0:
        raise ValueError("amount must be positive")
    return value


def _parse_amount(text: str):
    """Возвращает float или None, если это 'пропустить'/'skip'/'-'. Бросает ValueError, если не число."""
    if (text or "").strip().lower() in ("пропустить", "skip", "-"):
        return None
    return _parse_positive(text)


def _maxposter_link(appraisal_id) -> Optional[str]:
    if not appraisal_id:
        return None
    return f"https://app.maxposter.ru/holding/used/appraisals/{appraisal_id}/view"


STATUS_BADGES = {
    STATUS_OK: "🟢",
    "Согласование РОП": "🟢",  # сделки, созданные до метрики v2
    STATUS_DDC: "🟡",
    STATUS_UK: "🟠",
    STATUS_UK_CEILING: "🔴",
    STATUS_REJECT: "⛔",
}


def _status_text(status: Optional[str]) -> str:
    """«🟢 Согласовано», «🟠 Согласование УК» — цвет виден с одного взгляда."""
    if not status:
        return "—"
    badge = STATUS_BADGES.get(status)
    return f"{badge} {status}" if badge else status


def _vehicle_header(vehicle_info: dict, reception_type: Optional[str] = None) -> str:
    """Шапка с городом, типом контракта, авто и ссылкой на оценку — идёт в начале
    и итогового расчёта, и карточки согласования. Автосалон (город и бренд) —
    первой строкой: от него зависит ПЦП, согласующий проверяет его первым."""
    vehicle_line = " ".join(
        str(x) for x in [vehicle_info.get("brand"), vehicle_info.get("model"), vehicle_info.get("year")] if x
    )
    lines = []
    if vehicle_info.get("salon"):
        lines.append(f"🏢 Автосалон: {salon_label(vehicle_info['salon'])}")
    elif vehicle_info.get("city"):  # сделки, созданные до перехода на ПЦП автосалона
        lines.append(f"🏢 Город: {vehicle_info['city']}")
    if reception_type:
        lines.append(f"Тип контракта: {reception_type}")
    if vehicle_line:
        if not (vehicle_info.get("brand") or vehicle_info.get("model")):
            vehicle_line = f"Год выпуска: {vehicle_line}"
        lines.append(vehicle_line)
    if vehicle_info.get("mileage") is not None:
        lines.append(f"Пробег: {_fmt(vehicle_info['mileage'])} км")
    link = _maxposter_link(vehicle_info.get("appraisal_id"))
    if link:
        lines.append(f"Оценка в MaxPoster: {link}")
    return "\n".join(lines)


def _appraisal_input(data: dict) -> AppraisalInput:
    """Входные данные расчёта из состояния диалога (всё, что подтянули из
    MaxPoster или ввёл оценщик)."""
    return AppraisalInput(
        avito_price=data["avito_price"],
        reception_type=data["reception_type"],
        colors=data["colors"],
        condition=data["condition"],
        extra=data["extra"],
        salon=data.get("salon"),
        year=data.get("year"),
        mileage=data.get("mileage"),
        refurbishment_cost=data.get("presale_preparation_cost") or 0.0,
        manager_resale_forecast=data.get("manager_resale_forecast"),
        negotiated_price=data.get("negotiated_price"),
    )


def _metrics(result) -> dict:
    """Ключевые цифры расчёта — сохраняются в сделке для карточек согласования."""
    return {
        "version": METRIC_VERSION,
        "score": result.score,
        "mileage_per_year": result.mileage_per_year,
        "ptsp_rop": result.planned_sale_price_rop,
        "ptsp_ddc": result.planned_sale_price_ddc,
        "gross_margin": result.gross_margin_rop,
        "limit_rop": result.purchase_price_rop,
        "limit_ddc": result.purchase_price_ddc,
        "ceiling": result.ceiling_price,
        "refurbishment": result.refurbishment,
    }


def _metrics_lines(metrics: dict) -> list:
    if not metrics:
        return []
    return [
        f"ПЦП по метрике: {_fmt(metrics.get('ptsp_rop'))} ₽ (ДДЦ: {_fmt(metrics.get('ptsp_ddc'))} ₽)",
        f"Плановая ВП: {_fmt(metrics.get('gross_margin'))} ₽",
        f"Лимит РОП: {_fmt(metrics.get('limit_rop'))} ₽ | Предел ДДЦ: {_fmt(metrics.get('limit_ddc'))} ₽",
        f"Потолок к Авито: {_fmt(metrics.get('ceiling'))} ₽",
    ]


def _price_ladder(result) -> list:
    """Лесенка статусов по цене выкупа; совпадающие пороги не дублируем
    (у категории С лимит РОП, предел ДДЦ и потолок часто равны)."""
    rop, ddc, ceiling = result.purchase_price_rop, result.purchase_price_ddc, result.ceiling_price
    lines = [f"🟢 до {_fmt(rop)} ₽ — Согласовано (РОП)"]
    if round(ddc) > round(rop):
        lines.append(f"🟡 до {_fmt(ddc)} ₽ — Согласование ДДЦ")
    if round(ceiling) > round(max(rop, ddc)):
        lines.append(f"🟠 до {_fmt(ceiling)} ₽ — Согласование УК")
    lines.append(f"🔴 выше {_fmt(ceiling)} ₽ (потолок к Авито) — только УК")
    return lines


def _result_text(result, avito_price: float, extra_info: dict, vehicle_info: dict, reception_type: Optional[str] = None) -> str:
    header = _vehicle_header(vehicle_info, reception_type)
    lines = [header, ""] if header else []
    lines += [
        f"Категория: {result.category} (баллы: {result.score}, пробег в год: {_fmt(result.mileage_per_year)} км)",
        f"Авито-оценка: {_fmt(avito_price)} ₽",
        "",
        f"ПЦП по метрике: {_fmt(result.planned_sale_price_rop)} ₽ | с полномочиями ДДЦ: {_fmt(result.planned_sale_price_ddc)} ₽",
        f"Плановая ВП: {_fmt(result.gross_margin_rop)} ₽",
        f"Переподготовка: {_fmt(result.refurbishment)} ₽" if result.refurbishment else "Переподготовка: не запланирована",
        "",
    ]
    if not result.can_accept:
        lines += [
            f"⛔ {STATUS_REJECT}: лимит закупки по метрике = 0 (ВП больше, чем позволяет ПЦП).",
            "Принять можно только с согласования УК.",
        ]
    else:
        lines += [
            "Цена выкупа клиенту:",
            *_price_ladder(result),
            f"GM2 по метрике: {_fmt(result.gm2_rop)} ₽",
        ]
    if extra_info.get("repair_cost_max"):
        repair_min = extra_info.get("repair_cost_min")
        repair_range = (
            f"{_fmt(repair_min)}–{_fmt(extra_info['repair_cost_max'])}" if repair_min
            else f"до {_fmt(extra_info['repair_cost_max'])}"
        )
        lines += [
            "",
            f"Расчёт стоимости ремонта (по данным осмотра): {repair_range} ₽",
        ]
    if extra_info.get("autoteka_url"):
        lines += [f"Отчёт Автотеки: {extra_info['autoteka_url']}"]
    lines += ["", f"Метрика v{METRIC_VERSION}"]
    return "\n".join(lines)


def _kb(options, prefix: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for t in options:
        builder.callback(text=t, payload=f"{prefix}:{t}")
    builder.adjust(1)
    return builder


def reception_kb():
    return _kb(RECEPTION_TYPES, "reception")


def colors_kb():
    return _kb(get_colors_options(), "colors")


def condition_kb():
    return _kb(get_condition_options(), "condition")


def extra_kb():
    return _kb(get_extra_options(), "extra")


def salon_kb():
    """Кнопки салонов: подпись «Тюмень Toyota», в payload — название из таблицы."""
    builder = InlineKeyboardBuilder()
    for name in get_salon_options():
        builder.callback(text=salon_label(name), payload=f"salon:{name}")
    builder.adjust(1)
    return builder


def skip_kb(payload: str):
    builder = InlineKeyboardBuilder()
    builder.callback(text="Пропустить", payload=payload)
    builder.adjust(1)
    return builder


def main_menu_kb():
    """Главное меню: показывается при входе в бота и после каждой оценки."""
    builder = InlineKeyboardBuilder()
    builder.callback(text="🆕 Новая оценка", payload="new_appraisal")
    builder.callback(text="🧮 Предварительный расчёт", payload="quick_calc")
    builder.callback(text="📋 Мои оценки", payload="my_appraisals")
    builder.callback(text="❓ Помощь", payload="help")
    builder.adjust(1, 1, 2)
    return builder


def new_appraisal_kb():
    return main_menu_kb()


def _get_user_id(obj) -> Optional[int]:
    """Достаёт user_id из message/callback/event разных типов pymaxgram — с запасными вариантами,
    так как точная структура объекта не всегда одинакова."""
    uid = getattr(obj, "user_id", None)
    if uid is not None:
        return uid
    sender = getattr(obj, "sender", None)
    if sender is not None:
        uid = getattr(sender, "user_id", None)
        if uid is not None:
            return uid
    user = getattr(obj, "user", None)
    if user is not None:
        return getattr(user, "user_id", None) or getattr(user, "id", None)
    return None


def _get_user_name(obj) -> Optional[str]:
    """Достаёт имя пользователя из message/callback (по аналогии с _get_user_id)."""
    sender = getattr(obj, "sender", None)
    if sender is not None:
        name = getattr(sender, "first_name", None)
        if name:
            return name
    user = getattr(obj, "user", None)
    if user is not None:
        name = getattr(user, "first_name", None)
        if name:
            return name
    return None


def _vehicle_info(data: dict) -> dict:
    return {
        "brand": data.get("brand"),
        "model": data.get("model"),
        "year": data.get("year"),
        "mileage": data.get("mileage"),
        "appraiser_name": data.get("appraiser_name"),
        "dealer_name": data.get("dealer_name"),
        "appraisal_id": data.get("appraisal_id"),
        "salon": data.get("salon"),
    }


def deal_approval_kb(token: str):
    builder = InlineKeyboardBuilder()
    builder.callback(text="✅ Согласовано", payload=f"deal_approve:{token}")
    builder.callback(text="✏️ Согласовано с корректировкой", payload=f"deal_adjust:{token}")
    builder.callback(text="❌ Отказ в приёме", payload=f"deal_decline:{token}")
    builder.adjust(1)
    return builder


def pats_done_kb(token: str):
    builder = InlineKeyboardBuilder()
    builder.callback(text="✅ Готово (проверено, ушло на склад)", payload=f"pats_done:{token}")
    builder.callback(text="📎 Запросить диагностику", payload=f"pats_req_diag:{token}")
    builder.callback(text="🏷 Запросить исправление категории", payload=f"pats_req_cat:{token}")
    builder.adjust(1)
    return builder


def _approval_card_text(vehicle_info: dict, appraisal_input, result, manager_name: str, vin,
                         reception_type: Optional[str] = None, appraiser_sale_cost=None,
                         presale_preparation_cost=None, metrics: Optional[dict] = None) -> str:
    metrics = metrics or {}
    header = _vehicle_header(vehicle_info, reception_type)
    # шапка начинается с автосалона, оценщик — сразу под ней
    header_lines = header.split("\n") if header else []
    salon_lines = [ln for ln in header_lines if ln.startswith("🏢")]
    rest = [ln for ln in header_lines if not ln.startswith("🏢")]
    lines = ["📋 СОГЛАСОВАНИЕ СДЕЛКИ", *salon_lines,
             f"Оценщик: {vehicle_info.get('appraiser_name') or manager_name or '—'}", *rest]
    lines += [
        f"VIN: {vin or '—'}",
        f"Категория: {result.category}"
        + (f" (баллы: {metrics['score']}, пробег в год: {_fmt(metrics.get('mileage_per_year'))} км)"
           if metrics.get("score") is not None else ""),
        f"Авито-оценка: {_fmt(appraisal_input.avito_price)} ₽",
    ]
    lines += _metrics_lines(metrics)
    if appraisal_input.negotiated_price is not None:
        lines.append(f"Запрашиваемая цена выкупа: {_fmt(appraisal_input.negotiated_price)} ₽")
    if appraiser_sale_cost is not None:
        lines.append(f"Прогноз цены продажи (ДЦ): {_fmt(appraiser_sale_cost)} ₽")
        prep_for_calc = presale_preparation_cost or 0
        lines.append(
            f"Подготовка: {_fmt(presale_preparation_cost)} ₽" if presale_preparation_cost
            else "Подготовка: расходы не запланированы"
        )
        if appraisal_input.negotiated_price is not None:
            vp = appraiser_sale_cost - appraisal_input.negotiated_price - prep_for_calc
            lines.append(f"ВП (валовая прибыль): {_fmt(vp)} ₽")
    lines.append(f"Статус: {_status_text(result.approval_status)}")
    if result.gm2_negotiated is not None:
        lines.append(f"GM2 по факту: {_fmt(result.gm2_negotiated)} ₽")
    return "\n".join(line for line in lines if line)


async def _send_to_chat(bot, chat_id: int, text: str, keyboard=None) -> bool:
    """Отправка сообщения в чат по chat_id по инициативе бота (не в ответ).
    Точный метод в библиотеке документирован не полностью — пробуем наиболее
    вероятный вариант и логируем ошибку вместо падения бота, если не сработает."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, keyboard=keyboard)
        logger.info("[send_to_chat] Успешно отправлено в чат %s (%s символов)", chat_id, len(text))
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("[send_to_chat] Не удалось отправить сообщение в чат %s: %s", chat_id, e)
        return False


async def _submit_for_approval(bot, appraisal_input, result, vehicle_info: dict, manager_name: str, vin,
                                appraiser_sale_cost=None, presale_preparation_cost=None,
                                manager_user_id=None) -> tuple:
    """Создаёт карточку согласования и постит её в чат салона (Этап 1).
    Возвращает (заметка для ответа оценщику, токен сделки, ушла ли карточка в чат)."""
    dealer_name = vehicle_info.get("dealer_name")
    salon_chat_id = dealer_chats.get_salon_chat_id(dealer_name)

    token = deals_store.create_deal({
        "dealer_name": dealer_name,
        "manager_name": manager_name,
        "manager_user_id": manager_user_id,
        "vin": vin,
        "vehicle": vehicle_info,
        "negotiated_price": appraisal_input.negotiated_price,
        "avito_price": appraisal_input.avito_price,
        "category": result.category,
        "approval_status": result.approval_status,
        "gm2": result.gm2_negotiated,
        "reception_type": appraisal_input.reception_type,
        "appraiser_sale_cost": appraiser_sale_cost,
        "presale_preparation_cost": presale_preparation_cost,
        "salon": vehicle_info.get("salon"),
        "metrics": _metrics(result),
        "stage": "salon_pending",
    })

    if not salon_chat_id:
        return (
            "\n\n⚠️ Салон "
            + (f"«{_salon_name_for_chat(vehicle_info)}» " if dealer_name else "")
            + "не привязан к чату согласования — пока согласуйте по старому процессу."
        ), token, False

    text = _approval_card_text(vehicle_info, appraisal_input, result, manager_name, vin,
                                reception_type=appraisal_input.reception_type,
                                appraiser_sale_cost=appraiser_sale_cost,
                                presale_preparation_cost=presale_preparation_cost,
                                metrics=_metrics(result))
    sent = await _send_to_chat(bot, salon_chat_id, text, keyboard=deal_approval_kb(token))
    if sent:
        deals_store.update_deal(token, card_sent=True)  # только такие сделки попадают в напоминания
        return (f"\n\n✅ Карточка отправлена на согласование в чат салона «{_salon_name_for_chat(vehicle_info)}».\n"
                "О решении бот напишет вам сюда."), token, True
    return "\n\n⚠️ Не получилось отправить карточку в чат салона — согласуйте по старому процессу.", token, False


def _is_owner(user_id: Optional[int]) -> bool:
    return user_id == OWNER_USER_ID


def _allowed(user_id: int) -> bool:
    if _is_owner(user_id):
        return True
    if not access_store.is_bot_enabled():
        return False
    if user_id in ADMIN_USER_IDS:
        return True
    if ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS:
        return True
    return access_store.is_allowed(user_id)


def _is_admin(user_id: int) -> bool:
    if _is_owner(user_id):
        return True
    if not access_store.is_bot_enabled():
        return False
    return user_id in ADMIN_USER_IDS


def _can_approve(user_id: Optional[int], approvers) -> bool:
    """Может ли пользователь нажимать кнопки согласования. Если список
    согласующих для салона/этапа не настроен — только администраторы
    (раньше в этом случае кнопки мог нажать любой участник чата)."""
    if user_id is None:
        return False
    if approvers:
        return user_id in approvers
    return _is_admin(user_id)


async def _deny_and_request_access(message, user_id: int) -> None:
    if not access_store.is_bot_enabled():
        await message.answer(text="Бот временно отключён администратором. Попробуйте позже.")
        return
    name = getattr(message.sender, "first_name", None) or ""
    is_new = access_store.request_access(user_id, name)
    if is_new:
        await message.answer(
            text=(
                f"У вас нет доступа к этому боту. Ваш ID: {user_id}.\n"
                "Заявка на доступ отправлена — сообщите администратору, "
                "что запросили доступ, и попросите одобрить."
            )
        )
    else:
        await message.answer(
            text=f"Доступ пока не одобрен. Ваш ID: {user_id}. Заявка уже зарегистрирована, ожидайте."
        )


async def _ask_next(state: FSMContext, answer_fn, bot, note: str = ""):
    """Спрашивает следующий НЕзаполненный шаг. Всё, что уже известно (из
    MaxPoster, по салону или из прошлых ответов), пропускается — так оценщик
    нажимает минимум кнопок. note — текст, который показываем перед вопросом
    (что нашли/подставили автоматически)."""
    data = await state.get_data()
    prefix = (note.strip() + "\n\n") if note.strip() else ""

    if not data.get("reception_type"):
        await state.set_state(AppraisalStates.reception_type)
        await answer_fn(text=prefix + "Выберите тип приёма:", keyboard=reception_kb())
        return
    if not data.get("salon") or data["salon"] not in get_salon_options():
        await state.set_state(AppraisalStates.salon)
        await answer_fn(
            text=prefix + "Выберите АВТОСАЛОН (только свой — от него зависит ПЦП):",
            keyboard=salon_kb(),
        )
        return
    if not data.get("year"):
        await state.set_state(AppraisalStates.year)
        await answer_fn(text=prefix + "Введите год выпуска (по ПТС), например 2019:")
        return
    if data.get("mileage") is None:
        await state.set_state(AppraisalStates.mileage)
        await answer_fn(text=prefix + "Введите пробег по одометру, км (например 90000):")
        return
    if not data.get("colors"):
        await state.set_state(AppraisalStates.colors)
        await answer_fn(text=prefix + "Сколько окрасов?", keyboard=colors_kb())
        return
    if not data.get("condition"):
        await state.set_state(AppraisalStates.condition)
        await answer_fn(text=prefix + "Техническое состояние?", keyboard=condition_kb())
        return
    if not data.get("extra"):
        accidents_count = data.get("accidents_count")
        # Пункт ставим автоматически, только если он есть в таблице «Параметры Метрики»:
        # если его там переименуют, иначе молча получили бы 0 баллов вместо штрафа.
        if (accidents_count is not None and accidents_count > ACCIDENTS_AUTO_THRESHOLD
                and ACCIDENTS_AUTO_OPTION in get_extra_options() and not data.get("_extra_manual")):
            await state.update_data(extra=ACCIDENTS_AUTO_OPTION)
            note = (note + f"\n(доп. данные — автоматически «{ACCIDENTS_AUTO_OPTION}»: "
                    f"по данным оценки {accidents_count} ДТП)").strip()
        else:
            await state.set_state(AppraisalStates.extra)
            await answer_fn(text=prefix + "Доп. данные?", keyboard=extra_kb())
            return
    await _finish_scoring(state, answer_fn, bot, auto_note=("\n\n" + note.strip()) if note.strip() else "")


async def _finish_scoring(state: FSMContext, answer_fn, bot, auto_note: str = ""):
    """Считаем предварительный результат и решаем, что спрашивать дальше —
    план.цену продажи и/или согласованную цену (пропускаем то, что уже
    известно из системы)."""
    data = await state.get_data()
    appraisal_input = _appraisal_input({**data, "manager_resale_forecast": None, "negotiated_price": None})
    result = calc_appraisal(appraisal_input)

    extra_info = {
        "repair_cost_min": data.get("repair_cost_min"),
        "repair_cost_max": data.get("repair_cost_max"),
        "autoteka_url": data.get("autoteka_url"),
    }
    header = _result_text(result, appraisal_input.avito_price, extra_info,
                           _vehicle_info(data), data.get("reception_type")) + auto_note

    if data.get("_quick"):
        # предварительный расчёт: только показываем лимиты, в согласование ничего не уходит
        await state.clear()
        await answer_fn(
            text="🧮 ПРЕДВАРИТЕЛЬНЫЙ РАСЧЁТ\n\n" + header
            + "\n\nЭто прикидка — на согласование ничего не отправлено. Для сделки сделайте "
              "оценку в MaxPoster и нажмите «Новая оценка».",
            keyboard=main_menu_kb(),
        )
        return

    if data.get("negotiated_price") is not None:
        # пересчёт после правки на экране итога — цены уже введены, сразу к итогу
        await _review_or_close(state, answer_fn, data, note=auto_note)
        return

    planned_default = data.get("appraisal_sale_cost")
    negotiated_default = data.get("appraisal_purchase_cost")

    if planned_default is not None:
        await state.update_data(manager_resale_forecast=planned_default)
    if negotiated_default is not None:
        await state.update_data(negotiated_price=negotiated_default)

    if planned_default is not None and negotiated_default is not None:
        # оба значения уже есть в системе — сразу показываем на подтверждение
        data = await state.get_data()
        await _review_or_close(state, answer_fn, data, note=auto_note)
        return

    if planned_default is None:
        await state.set_state(AppraisalStates.waiting_planned_sale_price)
        await answer_fn(
            text=header
            + "\n\nВведите планируемую цену продажи (₽), или нажмите «Пропустить» — возьму ПЦП по метрике.",
            keyboard=skip_kb("skip_planned"),
        )
        return

    # план.цена уже есть, а согласованной цены — нет, спрашиваем её
    await state.set_state(AppraisalStates.waiting_negotiated)
    await answer_fn(
        text=header + "\n\nВведите цену, согласованную с клиентом (₽), или нажмите «Пропустить».",
        keyboard=skip_kb("skip_negotiated"),
    )


async def _log_appraisal_safe(manager_name, vin, appraisal_input, result, vehicle_info) -> None:
    """Запись в Google Таблицу. Сбой таблицы не должен ломать диалог с менеджером."""
    try:
        await asyncio.to_thread(log_appraisal, manager_name, vin, appraisal_input, result, vehicle_info)
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось записать оценку в Google Таблицу (VIN %s): %s", vin, e)


EDITABLE_FIELDS = {
    "reception_type": "Тип приёма",
    "colors": "Окрасы",
    "condition": "Тех. состояние",
    "extra": "Доп. данные",
}


def review_kb():
    builder = InlineKeyboardBuilder()
    builder.callback(text="📨 Отправить на согласование", payload="confirm_submit")
    builder.callback(text="🔄 Обновить из MaxPoster", payload="update_price")
    for field, label in EDITABLE_FIELDS.items():
        builder.callback(text=f"✏️ {label}", payload=f"edit:{field}")
    builder.adjust(1, 1, 2, 2)
    return builder


def _journal_fields(data: dict, result, appraisal_input) -> dict:
    """Что пишем в журнал «Мои оценки» / отчёт."""
    vehicle = " ".join(str(x) for x in [data.get("brand"), data.get("model"), data.get("year")] if x)
    return {
        "user_id": data.get("_manager_user_id"),
        "manager": data.get("_manager_name"),
        "salon": data.get("salon"),
        "dealer_name": data.get("dealer_name"),
        "vehicle": vehicle,
        "vin": data.get("vin"),
        "avito_price": appraisal_input.avito_price,
        "negotiated_price": appraisal_input.negotiated_price,
        "status": result.approval_status,
        "category": result.category,
        "limit_rop": result.purchase_price_rop,
    }


async def _journal_save(state: FSMContext, data: dict, result, appraisal_input) -> None:
    """Одна запись на оценку: при повторном пересчёте (правка, обновление
    из MaxPoster) обновляем ту же запись, а не плодим новые."""
    try:
        fields = _journal_fields(data, result, appraisal_input)
        rec_id = data.get("_record_id")
        if rec_id:
            appraisals_log.update(rec_id, **fields)
        else:
            await state.update_data(_record_id=appraisals_log.add(fields))
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось записать оценку в журнал: %s", e)


async def _review_or_close(state: FSMContext, answer_fn, data: dict, note: str = ""):
    """Показывает статус сделки с ценой выкупа и даёт оценщику выбор:
    отправить на согласование как есть, или поменять цену и посчитать заново.
    Автоматической отправки на согласование больше нет — только по кнопке.
    Шапка (категория/потолок/целевая ВП) пересчитывается и показывается
    заново при каждом вызове — в том числе после «Обновить из MaxPoster» и
    после повторного ввода цены, чтобы картина никогда не была урезанной."""
    appraisal_input = _appraisal_input(data)
    result = calc_appraisal(appraisal_input)
    await _journal_save(state, data, result, appraisal_input)
    data = await state.get_data()

    extra_info = {
        "repair_cost_min": data.get("repair_cost_min"),
        "repair_cost_max": data.get("repair_cost_max"),
        "autoteka_url": data.get("autoteka_url"),
    }
    full_header = _result_text(result, appraisal_input.avito_price, extra_info,
                                _vehicle_info(data), data.get("reception_type")) + note
    prefix = full_header + "\n\n"

    if appraisal_input.negotiated_price is None:
        manager_name = data.get("_manager_name") or "неизвестно"
        vehicle_info = _vehicle_info(data)
        await answer_fn(
            text=prefix
            + "⚠️ Сделка НЕ отправлена на согласование — не указана согласованная цена с клиентом."
            + "\n\nГотово. Чтобы посчитать следующее авто — жмите кнопку ниже.",
            keyboard=new_appraisal_kb(),
        )
        await state.clear()
        await _log_appraisal_safe(manager_name, data.get("vin"), appraisal_input, result, vehicle_info)
        return

    appraiser_name = data.get("appraiser_name")
    forecast_source = "ПЦП оценщика" if data.get("manager_resale_forecast") is not None else "ПЦП по метрике"
    lines = [
        "СОГЛАСОВАНИЕ СДЕЛКИ",
        f"Оценщик: {appraiser_name}" if appraiser_name else "",
        "Прогноз ДЦ:",
        f"  Цена продажи: {_fmt(result.manager_resale_forecast)} ₽ ({forecast_source})",
        f"  Запрашиваемая цена выкупа: {_fmt(appraisal_input.negotiated_price)} ₽",
        f"  Переподготовка: {_fmt(result.refurbishment)} ₽" if result.refurbishment
        else "  Переподготовка: расходы не запланированы",
        f"  ВП (валовая прибыль): {_fmt(result.margin_negotiated)} ₽",
        f"  GM2 прогноз: {_fmt(result.gm2_negotiated)} ₽",
        f"Статус: {_status_text(result.approval_status)}",
    ]
    text = prefix + "\n".join(line for line in lines if line)
    await state.set_state(AppraisalStates.reviewing_result)
    await answer_fn(text=text, keyboard=review_kb())


@router.message_callback(F.payload == "confirm_submit", StateFilter(AppraisalStates.reviewing_result))
async def on_confirm_submit(callback, state: FSMContext, bot):
    if not _allowed(_get_user_id(callback)):
        await state.clear()
        await callback.answer(text="У вас больше нет доступа к боту — карточка не отправлена.")
        return
    data = await state.get_data()
    # Сбрасываем сценарий сразу, чтобы повторное нажатие кнопки не создало вторую карточку.
    await state.clear()
    appraisal_input = _appraisal_input(data)
    result = calc_appraisal(appraisal_input)
    manager_name = data.get("_manager_name") or "неизвестно"
    vehicle_info = _vehicle_info(data)
    approval_note, token, sent = await _submit_for_approval(
        bot, appraisal_input, result, vehicle_info, manager_name, data.get("vin"),
        appraiser_sale_cost=result.manager_resale_forecast,
        presale_preparation_cost=data.get("presale_preparation_cost"),
        manager_user_id=data.get("_manager_user_id"),
    )
    try:
        if not data.get("_record_id"):
            data["_record_id"] = appraisals_log.add(_journal_fields(data, result, appraisal_input))
        appraisals_log.update(data["_record_id"], token=token, outcome="salon" if sent else "not_sent",
                              status=result.approval_status, negotiated_price=appraisal_input.negotiated_price)
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось обновить журнал оценок: %s", e)
    await callback.answer(
        text="✅ Отправлено на согласование." + approval_note
        + "\n\nГотово. Чтобы посчитать следующее авто — жмите кнопку ниже.",
        keyboard=new_appraisal_kb(),
    )
    await _log_appraisal_safe(manager_name, data.get("vin"), appraisal_input, result, vehicle_info)


@router.message_callback(F.payload == "update_price", StateFilter(AppraisalStates.reviewing_result))
async def on_update_price(callback, state: FSMContext, bot):
    """Не спрашиваем цену руками — подтягиваем её заново из MaxPoster, чтобы
    цифры в боте всегда совпадали с тем, что реально стоит в системе.
    Оценщик сначала торгуется с клиентом и меняет цену в MaxPoster, потом
    жмёт эту кнопку (можно нажимать сколько угодно раз)."""
    data = await state.get_data()
    appraisal_id = data.get("appraisal_id")

    if not appraisal_id:
        # ручной ввод без реальной оценки в MaxPoster — тут обновлять неоткуда,
        # остаётся текстовый ввод
        await state.set_state(AppraisalStates.waiting_negotiated)
        await callback.answer(
            text="Введите новую цену, согласованную с клиентом (₽):",
            keyboard=skip_kb("skip_negotiated"),
        )
        return

    try:
        fresh = await asyncio.to_thread(fetch_appraisal, str(appraisal_id))
    except Exception as e:  # noqa: BLE001 — MaxPosterError или сетевой сбой
        if not isinstance(e, MaxPosterError):
            logger.exception("Ошибка обновления оценки %s из MaxPoster", appraisal_id)
        await callback.answer(
            text=f"Не удалось обновить данные из MaxPoster ({e}). Проверьте оценку и нажмите кнопку ещё раз.",
            keyboard=review_kb(),
        )
        return

    if fresh.appraisal_purchase_cost is None:
        await callback.answer(
            text="В MaxPoster пока не видно новой закупочной цены — сначала обновите её там "
                 "(блок «Продажа»), затем нажмите эту кнопку ещё раз.",
            keyboard=review_kb(),
        )
        return

    await state.update_data(
        appraisal_purchase_cost=fresh.appraisal_purchase_cost,
        appraisal_sale_cost=fresh.appraisal_sale_cost,
        negotiated_price=fresh.appraisal_purchase_cost,
        manager_resale_forecast=(
            fresh.appraisal_sale_cost if fresh.appraisal_sale_cost is not None
            else data.get("manager_resale_forecast")
        ),
        # переподготовку и Авито-оценку тоже могли поправить в MaxPoster
        presale_preparation_cost=fresh.presale_preparation_cost,
        avito_price=fresh.avito_price or data.get("avito_price"),
    )
    data = await state.get_data()
    await _review_or_close(state, callback.answer, data)


@router.bot_started()
async def on_bot_started(event: BotStarted):
    user_id = getattr(event, "user_id", None) or getattr(getattr(event, "sender", None), "user_id", None)
    if user_id is not None and not _allowed(user_id):
        access_store.request_access(user_id, getattr(getattr(event, "user", None), "first_name", None) or "")
        await event.answer(
            text=f"У вас нет доступа к этому боту. Ваш ID: {user_id}. Заявка отправлена, сообщите администратору."
        )
        return
    await event.answer(
        text=(
            "Привет! Я считаю выкупную цену авто в трейд-ин по метрике АСП.\n\n"
            "Пришлите ссылку на оценку из MaxPoster, VIN или номер сделки — или выберите действие:"
        ),
        keyboard=main_menu_kb(),
    )


@router.message(Command("myid"))
async def cmd_myid(message, bot):
    await message.answer(text=f"Ваш ID в MAX: {message.sender.user_id}")


@router.message(Command("users"))
async def cmd_users(message, bot):
    if not _is_admin(message.sender.user_id):
        await message.answer(text="Эта команда только для администраторов.")
        return
    allowed = access_store.list_allowed()
    lines = ["Пользователи с доступом к боту:"]
    if ADMIN_USER_IDS:
        lines.append("Администраторы: " + ", ".join(str(x) for x in ADMIN_USER_IDS))
    builder = InlineKeyboardBuilder()
    if allowed:
        lines.append("\nМенеджеры (нажмите «Убрать», если человек уволился):")
        for uid, info in allowed.items():
            name = info.get("name") or "без имени"
            lines.append(f"  {uid} — {name}")
            builder.callback(text=f"🗑 Убрать {name or uid}", payload=f"revoke_user:{uid}")
        builder.adjust(1)
    else:
        lines.append("\nМенеджеры: пока никого не одобрено.")
    await message.answer(text="\n".join(lines), keyboard=builder if allowed else None)


@router.message_callback(F.payload.startswith("revoke_user:"))
async def on_revoke_user(callback, bot):
    if not _is_admin(_get_user_id(callback)):
        await callback.answer(notification="Только для администраторов.")
        return
    uid = int(callback.payload.split(":", 1)[1])
    access_store.revoke(uid)
    await callback.answer(text=f"Готово, доступ для {uid} закрыт.")


def admin_menu_kb():
    builder = InlineKeyboardBuilder()
    builder.callback(text="📋 Заявки на доступ", payload="admin_pending")
    builder.callback(text="👥 Пользователи", payload="admin_users")
    builder.callback(text="🏢 Салоны", payload="admin_salons")
    builder.callback(text="📊 Отчёт за 7 дней", payload="admin_report")
    builder.adjust(1)
    return builder


@router.message(Command("disable_all"))
async def cmd_disable_all(message, bot):
    if not _is_owner(message.sender.user_id):
        await message.answer(text="Эта команда доступна только владельцу бота.")
        return
    access_store.set_bot_enabled(False)
    await message.answer(
        text=(
            "🔴 Бот отключён для всех, кроме вас. Никто (включая других "
            "администраторов) больше не сможет им пользоваться, пока вы не "
            "включите его обратно командой /enable_all."
        )
    )


@router.message(Command("enable_all"))
async def cmd_enable_all(message, bot):
    if not _is_owner(message.sender.user_id):
        await message.answer(text="Эта команда доступна только владельцу бота.")
        return
    access_store.set_bot_enabled(True)
    await message.answer(text="🟢 Бот снова доступен всем, у кого был доступ.")


@router.message(Command("admin"))
async def cmd_admin(message, bot):
    if not _is_admin(message.sender.user_id):
        await message.answer(text="Эта команда только для администраторов.")
        return
    await message.answer(text="Меню администратора:", keyboard=admin_menu_kb())


@router.message_callback(F.payload == "admin_pending")
async def on_admin_pending(callback, bot):
    if not _is_admin(_get_user_id(callback)):
        await callback.answer(notification="Только для администраторов.")
        return
    pending = access_store.list_pending()
    if not pending:
        await callback.answer(text="Заявок на доступ нет.")
        return
    builder = InlineKeyboardBuilder()
    lines = ["Заявки на доступ:"]
    for uid, info in pending.items():
        lines.append(f"  {uid} — {info.get('name') or 'без имени'}, {info.get('requested_at')}")
        builder.callback(text=f"✅ Одобрить {uid}", payload=f"approve:{uid}")
        builder.callback(text=f"❌ Отклонить {uid}", payload=f"deny:{uid}")
    builder.adjust(2)
    await callback.answer(text="\n".join(lines), keyboard=builder)


@router.message_callback(F.payload == "admin_users")
async def on_admin_users(callback, bot):
    if not _is_admin(_get_user_id(callback)):
        await callback.answer(notification="Только для администраторов.")
        return
    allowed = access_store.list_allowed()
    lines = ["Пользователи с доступом к боту:"]
    if ADMIN_USER_IDS:
        lines.append("Администраторы: " + ", ".join(str(x) for x in ADMIN_USER_IDS))
    builder = InlineKeyboardBuilder()
    if allowed:
        lines.append("\nМенеджеры (нажмите «Убрать», если человек уволился):")
        for uid, info in allowed.items():
            name = info.get("name") or "без имени"
            lines.append(f"  {uid} — {name}")
            builder.callback(text=f"🗑 Убрать {name or uid}", payload=f"revoke_user:{uid}")
        builder.adjust(1)
    else:
        lines.append("\nМенеджеры: пока никого не одобрено.")
    await callback.answer(text="\n".join(lines), keyboard=builder if allowed else None)


@router.message_callback(F.payload == "admin_salons")
async def on_admin_salons(callback, bot):
    if not _is_admin(_get_user_id(callback)):
        await callback.answer(notification="Только для администраторов.")
        return
    salons = dealer_chats.list_salons()
    pats_id = dealer_chats.get_pats_chat_id()
    lines = ["Зарегистрированные салоны:"]
    if salons:
        for name, cid in salons.items():
            lines.append(f"  «{name}» → chat_id {cid}")
    else:
        lines.append("  (пока нет)")
    lines.append(f"\nЧат ПАЦ: {pats_id if pats_id else '(не задан)'}")
    await callback.answer(text="\n".join(lines))


@router.message(Command("pending"))
async def cmd_pending(message, bot):
    if not _is_admin(message.sender.user_id):
        await message.answer(text="Эта команда только для администраторов.")
        return
    pending = access_store.list_pending()
    if not pending:
        await message.answer(text="Заявок на доступ нет.")
        return

    builder = InlineKeyboardBuilder()
    lines = ["Заявки на доступ:"]
    for uid, info in pending.items():
        lines.append(f"  {uid} — {info.get('name') or 'без имени'}, {info.get('requested_at')}")
        builder.callback(text=f"✅ Одобрить {uid}", payload=f"approve:{uid}")
        builder.callback(text=f"❌ Отклонить {uid}", payload=f"deny:{uid}")
    builder.adjust(2)
    await message.answer(text="\n".join(lines), keyboard=builder)


@router.message_callback(F.payload.startswith("approve:"))
async def on_approve_callback(callback, bot):
    if not _is_admin(_get_user_id(callback)):
        await callback.answer(notification="Только для администраторов.")
        return
    uid = int(callback.payload.split(":", 1)[1])
    access_store.approve(uid)
    await callback.answer(text=f"Готово, доступ для {uid} открыт.")


@router.message_callback(F.payload.startswith("deny:"))
async def on_deny_callback(callback, bot):
    if not _is_admin(_get_user_id(callback)):
        await callback.answer(notification="Только для администраторов.")
        return
    uid = int(callback.payload.split(":", 1)[1])
    access_store.deny(uid)
    await callback.answer(text=f"Заявка {uid} отклонена.")


@router.message(Command("approve"))
async def cmd_approve(message, command: CommandObject, bot):
    if not _is_admin(message.sender.user_id):
        await message.answer(text="Эта команда только для администраторов.")
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer(text="Использование: /approve <ID> (или используйте кнопки в /pending)")
        return
    uid = int(command.args.strip())
    access_store.approve(uid)
    await message.answer(text=f"Готово, доступ для {uid} открыт.")


@router.message(Command("deny"))
async def cmd_deny(message, command: CommandObject, bot):
    if not _is_admin(message.sender.user_id):
        await message.answer(text="Эта команда только для администраторов.")
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer(text="Использование: /deny <ID> (или используйте кнопки в /pending)")
        return
    uid = int(command.args.strip())
    access_store.deny(uid)
    await message.answer(text=f"Заявка {uid} отклонена.")


def uk_approval_kb(token: str):
    builder = InlineKeyboardBuilder()
    builder.callback(text="✅ Согласовано", payload=f"uk_approve:{token}")
    builder.callback(text="✏️ Согласовано с корректировкой", payload=f"uk_adjust:{token}")
    builder.callback(text="❌ Отказ в приёме", payload=f"uk_decline:{token}")
    builder.adjust(1)
    return builder


async def _send_to_user(bot, user_id: int, text: str, keyboard=None) -> bool:
    """Личное сообщение конкретному пользователю по его user_id (не в ответ,
    а по инициативе бота). Как и _send_to_chat — метод не задокументирован
    официально, работает по аналогии с отправкой в чат."""
    try:
        await bot.send_message(user_id=user_id, text=text, keyboard=keyboard)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось отправить личное сообщение пользователю %s: %s", user_id, e)
        return False


def _deal_card_text(deal: dict) -> str:
    """Восстанавливает полный текст карточки согласования из сохранённой
    сделки — используется, чтобы ДОБАВЛЯТЬ статус (кто согласовал/отказ/
    корректировка) к полной информации, а не заменять её короткой строкой."""
    appraisal_input = AppraisalInput(
        avito_price=deal.get("avito_price") or 0,
        reception_type="", colors="", condition="", extra="",
        negotiated_price=deal.get("negotiated_price"),
    )
    result = SimpleNamespace(
        category=deal.get("category"),
        approval_status=deal.get("approval_status"),
        gm2_negotiated=deal.get("gm2"),
    )
    return _approval_card_text(
        deal.get("vehicle") or {}, appraisal_input, result,
        deal.get("manager_name") or "", deal.get("vin"),
        reception_type=deal.get("reception_type"),
        appraiser_sale_cost=deal.get("appraiser_sale_cost"),
        presale_preparation_cost=deal.get("presale_preparation_cost"),
        metrics=deal.get("metrics"),
    )


def _salon_name_for_chat(vehicle_info: dict) -> str:
    """«Тюмень Toyota» вместо «10089: !АСП Тюмень Toyota действующий»."""
    if vehicle_info.get("salon"):
        return salon_label(vehicle_info["salon"])
    return vehicle_info.get("dealer_name") or "—"


def _deal_salon_label(deal: dict) -> str:
    if deal.get("salon"):
        return salon_label(deal["salon"])
    return deal.get("dealer_name") or "—"


def _pats_card_text(deal: dict, approved_role_label: str = "") -> str:
    status_line = f"Статус: {approved_role_label}\n" if approved_role_label else ""
    link = _maxposter_link((deal.get("vehicle") or {}).get("appraisal_id"))
    link_line = f"Оценка в MaxPoster: {link}\n" if link else ""
    return (
        "📋 НА ПОДТВЕРЖДЕНИЕ В АВТОХАБ (ПАЦ)\n"
        f"🏢 Автосалон: {_deal_salon_label(deal)}\n"
        f"VIN: {deal.get('vin') or '—'}\n"
        + link_line +
        f"Оценщик: {(deal.get('vehicle') or {}).get('appraiser_name') or deal.get('manager_name') or '—'}\n"
        f"Цена выкупа: {_fmt(deal['negotiated_price']) + ' ₽' if deal.get('negotiated_price') else '—'}\n"
        + status_line
    )


async def _send_to_pats(bot, token: str, deal: dict, approved_role_label: str = "") -> str:
    """Отправляет карточку в чат ПАЦ (финальный этап перед складом).
    Возвращает заметку о результате для ответа согласовавшему."""
    deals_store.update_deal(token, stage="pats_pending", final_approved_by_role=approved_role_label)
    pats_chat_id = dealer_chats.get_pats_chat_id()
    logger.info("[send_to_pats] token=%s pats_chat_id=%s", token, pats_chat_id)
    pats_text = (
        _pats_card_text(deal, approved_role_label)
        + "\n⚠️ Перед подтверждением проверьте, что в Документы → Заказ-наряды приложена диагностика."
    )
    if not pats_chat_id:
        logger.warning("[send_to_pats] token=%s — чат ПАЦ не зарегистрирован, карточка не отправлена", token)
        return "\n\n⚠️ Чат ПАЦ ещё не зарегистрирован."
    sent = await _send_to_chat(bot, pats_chat_id, pats_text, keyboard=pats_done_kb(token))
    return "\n\nОтправлено в ПАЦ." if sent else "\n\n⚠️ Не удалось отправить в ПАЦ, сообщите администратору."


def _deal_short(deal: dict) -> str:
    """«Тюмень Toyota · Kia Rio 2019 · VIN …» — для уведомлений оценщику."""
    vehicle = deal.get("vehicle") or {}
    car = " ".join(str(x) for x in [vehicle.get("brand"), vehicle.get("model"), vehicle.get("year")] if x)
    return " · ".join(x for x in [_deal_salon_label(deal), car, f"VIN {deal['vin']}" if deal.get("vin") else ""] if x)


async def _notify_appraiser(bot, token: str, deal: dict, event: str, outcome: Optional[str] = None) -> None:
    """Пишет оценщику в личку о решении по его сделке и обновляет «Мои оценки»."""
    try:
        fields = {"last_event": event}
        if outcome:
            fields["outcome"] = outcome
        appraisals_log.update_by_token(token, **fields)
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось обновить журнал по сделке %s: %s", token, e)
    user_id = deal.get("manager_user_id")
    if user_id:
        await _send_to_user(bot, user_id, f"{event}\n{_deal_short(deal)}")


async def _advance_after_salon_approval(bot, token: str, deal: dict, role: Optional[str], approver_name: Optional[str] = None) -> str:
    """Общая логика после согласования на этапе чата салона (только для
    настоящего «Согласовано» — «с корректировкой» обрабатывается отдельно,
    см. _return_for_correction, и дальше по цепочке НЕ идёт)."""
    status = deal.get("approval_status")
    card = _deal_card_text(deal)

    if status in UK_STATUSES:
        approved_role = role or "ДДЦ"
        deals_store.update_deal(token, stage="uk_pending", ddc_approved_role=approved_role)
        uk_ids = approval_hierarchy.get_uk_approvers(deal.get("dealer_name"))
        uk_text = (
            f"Ожидается ваше согласование сделки в рабочем чате «{_deal_salon_label(deal)}».\n\n" + card
        )
        sent_any = False
        for uk_id in uk_ids:
            if await _send_to_user(bot, uk_id, uk_text, keyboard=uk_approval_kb(token)):
                sent_any = True

        # Дублируем ту же карточку в чат салона — УК может согласовать и там,
        # если личное сообщение почему-либо не дошло, или ему так удобнее.
        salon_chat_id = dealer_chats.get_salon_chat_id(deal.get("dealer_name"))
        posted_in_chat = False
        if salon_chat_id:
            posted_in_chat = await _send_to_chat(bot, salon_chat_id, uk_text, keyboard=uk_approval_kb(token))

        if sent_any and posted_in_chat:
            note = "\n\nОтправлено на согласование УК (лично и в этот чат)."
        elif sent_any:
            note = "\n\nОтправлено на согласование УК лично."
        elif posted_in_chat:
            note = "\n\nЛичное сообщение УК не ушло, но карточка есть в этом чате."
        else:
            note = "\n\n⚠️ Не удалось отправить УК ни лично, ни в чат — сообщите администратору."
        name_suffix2 = f": {approver_name}" if approver_name else ""
        await _notify_appraiser(bot, token, deal,
                                f"✅ Согласовано {approved_role}{name_suffix2}. Сделка ушла на согласование УК.",
                                outcome="uk")
        return card + f"\n\n✅ Согласовано {approved_role}{name_suffix2}." + note

    name_suffix = f": {approver_name}" if approver_name else ""
    final_role_label = f"Согласовано {role}{name_suffix}" if role else "Заявка согласована"
    note = await _send_to_pats(bot, token, deal, approved_role_label=final_role_label)
    await _notify_appraiser(bot, token, deal, f"✅ {final_role_label}. Сделка ушла на проверку в ПАЦ.", outcome="pats")
    return card + f"\n\n✅ {final_role_label}." + note


async def _advance_after_uk_approval(bot, token: str, deal: dict, approver_name: Optional[str] = None) -> str:
    card = _deal_card_text(deal)
    name_suffix = f": {approver_name}" if approver_name else ""
    final_role_label = f"Согласовано УК{name_suffix}"
    note = await _send_to_pats(bot, token, deal, approved_role_label=final_role_label)
    await _notify_appraiser(bot, token, deal, f"✅ {final_role_label}. Сделка ушла на проверку в ПАЦ.", outcome="pats")
    return card + f"\n\n✅ {final_role_label}." + note


async def _return_for_correction(bot, token: str, deal: dict, role: Optional[str]) -> str:
    """Сделка НЕ идёт в ПАЦ — возвращается в чат салона с пометкой
    «оценка с корректировкой» и предложенными цифрами. Оценщику нужно
    поправить данные в MaxPoster и заново отправить оценку боту."""
    salon_chat_id = dealer_chats.get_salon_chat_id(deal.get("dealer_name"))
    role_label = f" ({role})" if role else ""
    vehicle_header = _vehicle_header(deal.get("vehicle") or {}, deal.get("reception_type"))
    text = (
        "🔄 ОЦЕНКА С КОРРЕКТИРОВКОЙ" + role_label + "\n"
        + (vehicle_header + "\n" if vehicle_header else "")
        + f"VIN: {deal.get('vin') or '—'}\n"
        f"Оценщик: {(deal.get('vehicle') or {}).get('appraiser_name') or deal.get('manager_name')}\n\n"
        f"Предложенная цена выкупа: {_fmt(deal.get('negotiated_price'))} ₽\n"
        f"Предложенная ПЦП: {_fmt(deal.get('manager_resale_forecast'))} ₽\n\n"
        "⚠️ Оценщику нужно скорректировать закупочную цену и/или цену продажи "
        "в MaxPoster и заново прислать боту оценку на согласование."
    )
    if salon_chat_id:
        sent = await _send_to_chat(bot, salon_chat_id, text)
        note = "\n\nСообщение отправлено в чат салона." if sent else "\n\n⚠️ Не удалось отправить в чат салона."
    else:
        note = "\n\n⚠️ Чат салона не зарегистрирован — сообщите оценщику вручную."

    await _notify_appraiser(
        bot, token, deal,
        f"🔄 Оценка возвращена на корректировку{role_label}: предложено — цена выкупа "
        f"{_fmt(deal.get('negotiated_price'))} ₽, ПЦП {_fmt(deal.get('manager_resale_forecast'))} ₽. "
        "Поправьте цены в MaxPoster и отправьте оценку заново.",
        outcome="correction")
    card = _deal_card_text(deal)
    deals_store.delete_deal(token)
    return card + "\n\n🔄 Оценка возвращена на корректировку." + note


async def _handle_adjustment_input(message, token: str, deal: dict, bot):
    """Обрабатывает ответ согласующего на вопрос о новой цене/ПЦП после
    нажатия «Согласовано с корректировкой» (личным сообщением боту)."""
    field = deal.get("awaiting_field")
    text = _text(message)

    if text.lower() in ("отмена", "cancel", "/cancel"):
        # Возвращаем сделку на прежний этап и заново публикуем карточку с кнопками
        # (старая карточка после нажатия «с корректировкой» осталась без кнопок).
        prev_stage = deal.get("stage_before_adjust") or "salon_pending"
        deals_store.update_deal(token, stage=prev_stage,
                                 awaiting_input_from=None, awaiting_field=None, stage_before_adjust=None)
        kb = uk_approval_kb(token) if prev_stage == "uk_pending" else deal_approval_kb(token)
        salon_chat_id = dealer_chats.get_salon_chat_id(deal.get("dealer_name"))
        reposted = bool(salon_chat_id) and await _send_to_chat(bot, salon_chat_id, _deal_card_text(deal), keyboard=kb)
        if not reposted:
            await message.answer(text=_deal_card_text(deal), keyboard=kb)
        appraisals_log.update_by_token(token, outcome="uk" if prev_stage == "uk_pending" else "salon")
        await message.answer(text="Корректировка отменена, карточка согласования отправлена заново.")
        return

    try:
        value = _parse_positive(text)
    except ValueError:
        await message.answer(text="Не понял сумму, введите ещё раз (например: 950000) или «отмена»")
        return

    if field == "price":
        deals_store.update_deal(token, negotiated_price=value, awaiting_field="forecast")
        await message.answer(text="Принято. Теперь введите новую прогнозную цену продажи (ПЦП), ₽:")
        return

    # field == "forecast" — оба значения собраны. Дальше по цепочке НЕ идём —
    # оценка возвращается оценщику на доработку в MaxPoster.
    deals_store.update_deal(token, manager_resale_forecast=value,
                             awaiting_input_from=None, awaiting_field=None)
    deal = deals_store.get_deal(token)

    user_id = _get_user_id(message)
    role = approval_hierarchy.resolve_role(deal.get("dealer_name"), user_id)

    await message.answer(
        text=f"Принято. Предложено: цена выкупа {_fmt(deal['negotiated_price'])} ₽, ПЦП {_fmt(value)} ₽."
    )

    result_text = await _return_for_correction(bot, token, deal, role)
    await message.answer(text=result_text)


async def _request_adjustment(bot, callback, token: str, deal: dict, source: str) -> None:
    user_id = _get_user_id(callback)
    card = _deal_card_text(deal)
    sent = await _send_to_user(
        bot, user_id, "Введите новую цену выкупа клиенту (₽) или «отмена», чтобы вернуть карточку:"
    )
    if sent:
        # Пока идёт корректировка, остальные кнопки карточки неактивны —
        # иначе сделку успеют согласовать и отправить в ПАЦ параллельно.
        deals_store.update_deal(token, awaiting_input_from=user_id, awaiting_field="price",
                                 awaiting_source=source, stage_before_adjust=deal.get("stage"),
                                 stage="adjusting")
        appraisals_log.update_by_token(token, outcome="adjusting")
        await callback.answer(text=card + "\n\n🔄 Ожидается ввод новой цены — проверьте личные сообщения от бота.")
    else:
        await callback.answer(notification="Не удалось написать вам лично. Напишите боту что-нибудь в личку и нажмите ещё раз.")


@router.message_callback(F.payload.startswith("deal_approve:"))
async def on_deal_approve(callback, bot):
    token = callback.payload.split(":", 1)[1]
    deal = deals_store.get_deal(token)
    if not deal:
        logger.info("[approve] token=%s не найден в deals_store (возможно, уже обработан)", token)
        await callback.answer(notification="Сделка не найдена (возможно, уже обработана).")
        return
    if deal.get("stage") != "salon_pending":
        # Кто-то уже нажал кнопку раньше — не отправляем в ПАЦ повторно.
        await callback.answer(notification="Эта сделка уже обработана — повторно нажимать не нужно.")
        return

    user_id = _get_user_id(callback)
    status = deal.get("approval_status")
    dealer_name = deal.get("dealer_name")
    approvers = approval_hierarchy.get_first_stage_approvers(dealer_name, status)
    logger.info("[approve] token=%s user=%s dealer=%r status=%r approvers=%s", token, user_id, dealer_name, status, approvers)
    if not _can_approve(user_id, approvers):
        role_hint = " (нужен ДДЦ)" if status in UK_STATUSES else ""
        logger.info("[approve] token=%s отклонён: user=%s нет в approvers", token, user_id)
        await callback.answer(notification=f"У вас нет прав согласовывать сделку со статусом «{status}»{role_hint}.")
        return

    role, sheet_name = approval_hierarchy.resolve_role_and_name(deal.get("dealer_name"), user_id)
    approver_name = _get_user_name(callback) or sheet_name
    text = await _advance_after_salon_approval(bot, token, deal, role, approver_name)
    await callback.answer(text=text)


@router.message_callback(F.payload.startswith("deal_adjust:"))
async def on_deal_adjust(callback, bot):
    token = callback.payload.split(":", 1)[1]
    deal = deals_store.get_deal(token)
    if not deal:
        await callback.answer(notification="Сделка не найдена (возможно, уже обработана).")
        return
    if deal.get("stage") != "salon_pending":
        await callback.answer(notification="Эта сделка уже обработана — повторно нажимать не нужно.")
        return

    user_id = _get_user_id(callback)
    status = deal.get("approval_status")
    approvers = approval_hierarchy.get_first_stage_approvers(deal.get("dealer_name"), status)
    if not _can_approve(user_id, approvers):
        role_hint = " (нужен ДДЦ)" if status in UK_STATUSES else ""
        await callback.answer(notification=f"У вас нет прав согласовывать сделку со статусом «{status}»{role_hint}.")
        return

    await _request_adjustment(bot, callback, token, deal, source="salon")


@router.message_callback(F.payload.startswith("deal_decline:"))
async def on_deal_decline(callback, bot):
    token = callback.payload.split(":", 1)[1]
    deal = deals_store.get_deal(token)
    if not deal:
        await callback.answer(notification="Сделка не найдена (возможно, уже обработана).")
        return
    if deal.get("stage") != "salon_pending":
        await callback.answer(notification="Эта сделка уже обработана — повторно нажимать не нужно.")
        return

    user_id = _get_user_id(callback)
    approvers = approval_hierarchy.get_first_stage_approvers(deal.get("dealer_name"), deal.get("approval_status"))
    if not _can_approve(user_id, approvers):
        await callback.answer(notification=f"У вас нет прав отклонять сделку со статусом «{deal.get('approval_status')}».")
        return

    card = _deal_card_text(deal)
    approver_name = _get_user_name(callback)
    name_suffix = f" ({approver_name})" if approver_name else ""
    deals_store.delete_deal(token)
    await _notify_appraiser(bot, token, deal, f"❌ Отказ в приёме{name_suffix}. Автомобиль не покупаем.",
                            outcome="declined")
    await callback.answer(text=card + f"\n\n❌ Отказ в приёме{name_suffix}. Автомобиль не будет куплен.")


@router.message_callback(F.payload.startswith("uk_approve:"))
async def on_uk_approve(callback, bot):
    token = callback.payload.split(":", 1)[1]
    deal = deals_store.get_deal(token)
    if not deal:
        await callback.answer(notification="Сделка не найдена (возможно, уже обработана).")
        return
    if deal.get("stage") != "uk_pending":
        await callback.answer(notification="Эта сделка уже обработана — повторно нажимать не нужно.")
        return

    user_id = _get_user_id(callback)
    uk_ids = approval_hierarchy.get_uk_approvers(deal.get("dealer_name"))
    if not _can_approve(user_id, uk_ids):
        await callback.answer(notification="У вас нет прав согласовывать эту сделку (нужен УК).")
        return

    text = await _advance_after_uk_approval(bot, token, deal, _get_user_name(callback))
    await callback.answer(text=text)


@router.message_callback(F.payload.startswith("uk_adjust:"))
async def on_uk_adjust(callback, bot):
    token = callback.payload.split(":", 1)[1]
    deal = deals_store.get_deal(token)
    if not deal:
        await callback.answer(notification="Сделка не найдена (возможно, уже обработана).")
        return
    if deal.get("stage") != "uk_pending":
        await callback.answer(notification="Эта сделка уже обработана — повторно нажимать не нужно.")
        return

    user_id = _get_user_id(callback)
    uk_ids = approval_hierarchy.get_uk_approvers(deal.get("dealer_name"))
    if not _can_approve(user_id, uk_ids):
        await callback.answer(notification="У вас нет прав согласовывать эту сделку (нужен УК).")
        return

    await _request_adjustment(bot, callback, token, deal, source="uk")


@router.message_callback(F.payload.startswith("uk_decline:"))
async def on_uk_decline(callback, bot):
    token = callback.payload.split(":", 1)[1]
    deal = deals_store.get_deal(token)
    if not deal:
        await callback.answer(notification="Сделка не найдена (возможно, уже обработана).")
        return
    if deal.get("stage") != "uk_pending":
        await callback.answer(notification="Эта сделка уже обработана — повторно нажимать не нужно.")
        return

    user_id = _get_user_id(callback)
    uk_ids = approval_hierarchy.get_uk_approvers(deal.get("dealer_name"))
    if not _can_approve(user_id, uk_ids):
        await callback.answer(notification="У вас нет прав отклонять эту сделку (нужен УК).")
        return

    card = _deal_card_text(deal)
    approver_name = _get_user_name(callback)
    name_suffix = f" ({approver_name})" if approver_name else ""
    deals_store.delete_deal(token)
    await _notify_appraiser(bot, token, deal, f"❌ Отказ в приёме{name_suffix}. Автомобиль не покупаем.",
                            outcome="declined")
    await callback.answer(text=card + f"\n\n❌ Отказ в приёме{name_suffix}. Автомобиль не будет куплен.")


@router.message_callback(F.payload.startswith("pats_req_diag:"))
async def on_pats_req_diag(callback, bot):
    await _send_pats_request(callback, bot, "diag")


@router.message_callback(F.payload.startswith("pats_req_cat:"))
async def on_pats_req_cat(callback, bot):
    await _send_pats_request(callback, bot, "cat")


async def _send_pats_request(callback, bot, kind: str) -> None:
    """Общая логика для кнопок «Запросить диагностику» / «Запросить
    исправление категории» в карточке ПАЦ. Не трогает саму карточку и её
    кнопки — ПАЦ может нажать «Готово» на любом этапе, до или после запроса."""
    token = callback.payload.split(":", 1)[1]
    deal = deals_store.get_deal(token)
    if not deal:
        await callback.answer(notification="Сделка не найдена (возможно, уже обработана).")
        return

    user_id = _get_user_id(callback)
    pats_approvers = approval_hierarchy.get_pats_approvers()
    if not _can_approve(user_id, pats_approvers):
        await callback.answer(notification="У вас нет прав отправлять такие запросы.")
        return

    vehicle = deal.get("vehicle") or {}
    vehicle_line = " ".join(str(x) for x in [vehicle.get("brand"), vehicle.get("model"), vehicle.get("year")] if x)
    vin = deal.get("vin") or "—"

    if kind == "diag":
        request_text = (
            f"⚠️ ПАЦ: по оценке {vehicle_line or '—'}, VIN {vin} — "
            "необходимо загрузить диагностику в Заказ-наряды."
        )
    else:
        request_text = (
            f"⚠️ ПАЦ: по оценке {vehicle_line or '—'}, VIN {vin} — "
            f"проверьте и исправьте категорию в Автохаб (сейчас указана: {deal.get('category') or '—'})."
        )

    manager_user_id = deal.get("manager_user_id")
    sent_dm = await _send_to_user(bot, manager_user_id, request_text) if manager_user_id else False

    pats_chat_id = dealer_chats.get_pats_chat_id()
    pats_note = request_text + ("\n\n(отправлено оценщику лично)" if sent_dm else "\n\n⚠️ Не удалось отправить оценщику лично — сообщите вручную.")
    if pats_chat_id:
        await _send_to_chat(bot, pats_chat_id, pats_note)

    await callback.answer(notification="Запрос отправлен.")


@router.message_callback(F.payload.startswith("pats_done:"))
async def on_pats_done(callback, bot):
    token = callback.payload.split(":", 1)[1]
    deal = deals_store.get_deal(token)
    if not deal:
        await callback.answer(notification="Сделка не найдена (возможно, уже обработана).")
        return

    user_id = _get_user_id(callback)
    pats_approvers = approval_hierarchy.get_pats_approvers()
    if not _can_approve(user_id, pats_approvers):
        await callback.answer(notification="У вас нет прав подтверждать приёмку в ПАЦ.")
        return

    pats_confirmer = _get_user_name(callback) or "ПАЦ"
    pats_card = _pats_card_text(deal, deal.get("final_approved_by_role"))
    confirm_line = f"\n\n✅ ПАЦ подтвердил приёмку: {pats_confirmer}\n\nНе забудьте нажать «Согласовать» в самом Автохаб."

    salon_chat_id = dealer_chats.get_salon_chat_id(deal.get("dealer_name"))
    if salon_chat_id:
        card = _deal_card_text(deal)
        await _send_to_chat(bot, salon_chat_id, card + confirm_line)

    deals_store.delete_deal(token)
    await _notify_appraiser(bot, token, deal,
                            f"🏁 ПАЦ подтвердил приёмку ({pats_confirmer}). Не забудьте нажать «Согласовать» в Автохабе.",
                            outcome="accepted")
    await callback.answer(text=pats_card + confirm_line)


@router.bot_added()
async def on_bot_added_to_chat(event, bot):
    chat_id = getattr(event, "chat_id", None)
    kind = "канал" if getattr(event, "is_channel", False) else "чат"
    if chat_id is None:
        return
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"Бот добавлен в этот {kind}. ID чата: {chat_id}\n\n"
                "Если это рабочий чат салона — админ вводит здесь:\n"
                "/register_salon Название салона как в MaxPoster\n\n"
                "Если это общий чат ПАЦ — админ вводит здесь:\n"
                "/register_pats"
            ),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось поприветствовать новый чат %s: %s", chat_id, e)


def _flatten_json(obj, prefix: str = "", out: Optional[list] = None, max_list_items: int = 2) -> list:
    """{'a': {'b': 1}, 'c': [..]} -> ['a.b = 1', 'c[0]... ']. Длинные списки обрезаем."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten_json(v, f"{prefix}.{k}" if prefix else str(k), out, max_list_items)
    elif isinstance(obj, list):
        if not obj:
            out.append(f"{prefix} = []")
        for i, v in enumerate(obj[:max_list_items]):
            _flatten_json(v, f"{prefix}[{i}]", out, max_list_items)
        if len(obj) > max_list_items:
            out.append(f"{prefix} … ещё {len(obj) - max_list_items} эл.")
    else:
        value = repr(obj)
        out.append(f"{prefix} = {value[:80] + '…' if len(value) > 80 else value}")
    return out


@router.message(Command("raw"))
async def cmd_raw(message, command: CommandObject, bot):
    """[админ] Все поля, которые MaxPoster отдаёт по оценке, — чтобы найти, что
    ещё можно подставлять автоматически (тип приёма, ПЦП Автохаб, такси и т.п.)."""
    if not _is_admin(message.sender.user_id):
        await message.answer(text="Эта команда только для администраторов.")
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer(text="Использование: /raw <ссылка на оценку | VIN | номер сделки>")
        return
    try:
        data = await asyncio.to_thread(fetch_appraisal, query)
    except Exception as e:  # noqa: BLE001
        await message.answer(text=f"Не получилось получить оценку: {e}")
        return
    lines = _flatten_json(data.raw)
    chunk, chunks = "", []
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        chunks.append(chunk)
    for i, part in enumerate(chunks[:8], 1):
        await message.answer(text=f"Поля оценки {data.appraisal_id} ({i}/{min(len(chunks), 8)}):\n{part}")
    if len(chunks) > 8:
        await message.answer(text=f"…и ещё {len(chunks) - 8} сообщ. — показаны первые 8.")


def _build_report(records: list, days: int) -> str:
    """Отчёт по салонам: сколько оценок, куда ушли статусы, чем закончилось, закупка к Авито."""
    if not records:
        return f"📊 За {days} дн. оценок нет (учитываются оценки, сделанные в этой версии бота)."
    groups = {}
    for r in records:
        key = salon_label(r["salon"]) if r.get("salon") else (r.get("dealer_name") or "Без салона")
        groups.setdefault(key, []).append(r)

    def block(name, recs):
        sent = [r for r in recs if r.get("outcome") != "not_sent"]
        by_status = {}
        for r in sent:
            by_status[r.get("status")] = by_status.get(r.get("status"), 0) + 1
        ratios = [r["negotiated_price"] / r["avito_price"] for r in sent
                  if r.get("negotiated_price") and r.get("avito_price")]
        in_work = sum(1 for r in sent if r.get("outcome") in ("salon", "uk", "adjusting", "pats"))
        lines = [
            f"🏢 {name}",
            f"   оценок: {len(recs)} · отправлено: {len(sent)}",
        ]
        if sent:
            lines.append("   " + " · ".join(f"{_status_text(st)}: {n}" for st, n in
                                            sorted(by_status.items(), key=lambda x: -x[1])))
            lines.append(
                f"   принято ПАЦ: {sum(1 for r in sent if r.get('outcome') == 'accepted')} · "
                f"в работе: {in_work} · "
                f"корректировка: {sum(1 for r in sent if r.get('outcome') == 'correction')} · "
                f"отказ: {sum(1 for r in sent if r.get('outcome') == 'declined')}"
            )
        if ratios:
            lines.append(f"   цена выкупа к Авито: в среднем {sum(ratios) / len(ratios) * 100:.1f}%")
        return lines

    out = [f"📊 ОТЧЁТ ЗА {days} ДН.", ""]
    for name in sorted(groups, key=lambda n: -len(groups[n])):
        out += block(name, groups[name]) + [""]
    out += block("ВСЕГО", records)
    return "\n".join(out)


@router.message_callback(F.payload == "admin_report")
async def on_admin_report(callback, bot):
    if not _is_admin(_get_user_id(callback)):
        await callback.answer(notification="Только для администраторов.")
        return
    await callback.answer(text=_build_report(appraisals_log.since(7), 7)[:3900])


@router.message(Command("report"))
async def cmd_report(message, command: CommandObject, bot):
    """[админ] /report — за 7 дней, /report 30 — за 30 дней."""
    if not _is_admin(message.sender.user_id):
        await message.answer(text="Эта команда только для администраторов.")
        return
    arg = (command.args or "").strip()
    days = int(arg) if arg.isdigit() and 0 < int(arg) <= 366 else 7
    text = _build_report(appraisals_log.since(days), days)
    while text:  # длинный отчёт — несколькими сообщениями
        chunk = text[:3800]
        if len(text) > 3800 and "\n" in chunk:
            chunk = chunk[:chunk.rfind("\n")]
        await message.answer(text=chunk)
        text = text[len(chunk):].lstrip("\n")


@router.message(Command("register_salon"))
async def cmd_register_salon(message, command: CommandObject, bot):
    if not _is_admin(message.sender.user_id):
        await message.answer(text="Эта команда только для администраторов.")
        return
    if not command.args:
        await message.answer(text="Использование: /register_salon Название салона (точно как в MaxPoster, поле dealer.companyName)")
        return
    chat_id = message.recipient.chat_id
    name = command.args.strip()
    dealer_chats.set_salon_chat_id(name, chat_id)
    await message.answer(text=f"Готово. Этот чат (ID {chat_id}) привязан к салону «{name}».")


@router.message(Command("register_pats"))
async def cmd_register_pats(message, bot):
    if not _is_admin(message.sender.user_id):
        await message.answer(text="Эта команда только для администраторов.")
        return
    chat_id = message.recipient.chat_id
    dealer_chats.set_pats_chat_id(chat_id)
    await message.answer(text=f"Готово. Этот чат (ID {chat_id}) назначен общим чатом ПАЦ.")


@router.message(Command("salons"))
async def cmd_salons(message, bot):
    if not _is_admin(message.sender.user_id):
        await message.answer(text="Эта команда только для администраторов.")
        return
    salons = dealer_chats.list_salons()
    pats_id = dealer_chats.get_pats_chat_id()
    lines = ["Зарегистрированные салоны:"]
    if salons:
        for name, cid in salons.items():
            lines.append(f"  «{name}» → chat_id {cid}")
    else:
        lines.append("  (пока нет)")
    lines.append(f"\nЧат ПАЦ: {pats_id if pats_id else '(не задан)'}")
    await message.answer(text="\n".join(lines))


async def _start_new_appraisal(state: FSMContext, answer_fn):
    await state.clear()
    await state.set_state(AppraisalStates.waiting_link)
    await answer_fn(text="Пришлите ссылку на оценку из MaxPoster, VIN, номер сделки, или напишите «вручную».")


def _is_private_chat(message) -> bool:
    """Определяет, личное это сообщение боту или сообщение в групповом чате.
    В группах бот не должен реагировать на произвольный текст — только на
    явные команды и кнопки под своими карточками, иначе он спамит всех подряд
    «доступ запрещён» на обычную переписку в рабочем чате."""
    recipient = getattr(message, "recipient", None)
    chat_type = getattr(recipient, "chat_type", None)
    if chat_type is not None:
        return str(chat_type).lower() in ("dialog", "private", "user")
    chat_id = getattr(recipient, "chat_id", None)
    user_id = getattr(getattr(message, "sender", None), "user_id", None)
    if chat_id is not None and user_id is not None:
        return chat_id == user_id
    return True  # не смогли определить — не блокируем, чтобы не сломать личку


HELP_TEXT = (
    "❓ КАК ПОЛЬЗОВАТЬСЯ БОТОМ\n\n"
    "🆕 Новая оценка — пришлите ссылку на оценку из MaxPoster (или VIN, номер сделки, «вручную»). "
    "Бот сам подставит автосалон, год, пробег, окрасы, ДТП, переподготовку и цены из MaxPoster, "
    "спросит недостающее кнопками и покажет расчёт. На согласование карточка уходит только по кнопке "
    "«Отправить на согласование».\n\n"
    "✏️ На экране итога можно исправить тип приёма, окрасы, тех. состояние и доп. данные — "
    "бот пересчитает статус.\n\n"
    "🧮 Предварительный расчёт — быстрая прикидка без MaxPoster: Авито-оценка, год, пробег и пара кнопок. "
    "Ничего не отправляется.\n\n"
    "📋 Мои оценки — последние 10 оценок и где сейчас каждая сделка. О решениях согласующих бот "
    "пишет вам сам.\n\n"
    "Статусы: 🟢 Согласовано (РОП) · 🟡 Согласование ДДЦ · 🟠 Согласование УК · "
    "🔴 выше потолка к Авито (только УК) · ⛔ Не принимать.\n\n"
    "Команды: /new — новая оценка, /calc — предварительный расчёт, /my — мои оценки, /menu — меню, "
    "/myid — ваш ID в MAX."
)


def _format_my_appraisals(records: list) -> str:
    if not records:
        return "📋 У вас пока нет оценок, сделанных в этой версии бота."
    lines = [f"📋 МОИ ОЦЕНКИ (последние {len(records)})", ""]
    for i, r in enumerate(records, 1):
        ts = (r.get("ts") or "")[:16]
        when = f"{ts[8:10]}.{ts[5:7]} {ts[11:16]}" if len(ts) >= 16 else ts
        head = " · ".join(x for x in [when, salon_label(r.get("salon")) if r.get("salon") else r.get("dealer_name"),
                                      r.get("vehicle") or r.get("vin")] if x)
        lines.append(f"{i}. {head}")
        outcome = appraisals_log.OUTCOME_TEXT.get(r.get("outcome"), r.get("outcome") or "")
        if r.get("negotiated_price"):
            lines.append(f"   {_fmt(r['negotiated_price'])} ₽ · {_status_text(r.get('status'))} → {outcome}")
        else:
            lines.append(f"   расчёт: лимит РОП {_fmt(r.get('limit_rop'))} ₽ · {outcome}")
        if r.get("last_event"):
            lines.append(f"   {r['last_event']}")
    return "\n".join(lines)


async def _check_private_access(message) -> bool:
    if not _is_private_chat(message):
        return False
    if not _allowed(message.sender.user_id):
        await _deny_and_request_access(message, message.sender.user_id)
        return False
    return True


async def _start_quick_calc(state: FSMContext, answer_fn, user_id, user_name):
    await state.clear()
    await state.update_data(_quick=True, _manager_user_id=user_id, _manager_name=user_name)
    await state.set_state(AppraisalStates.manual_avito)
    await answer_fn(text="🧮 ПРЕДВАРИТЕЛЬНЫЙ РАСЧЁТ\n\nВведите Авито-оценку автомобиля (₽), например 1200000:")


@router.message(Command("menu"))
async def cmd_menu(message, bot):
    if await _check_private_access(message):
        await message.answer(text="Выберите действие:", keyboard=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message, bot):
    if await _check_private_access(message):
        await message.answer(text=HELP_TEXT, keyboard=main_menu_kb())


@router.message(Command("my"))
async def cmd_my(message, bot):
    if await _check_private_access(message):
        records = appraisals_log.for_user(message.sender.user_id)
        await message.answer(text=_format_my_appraisals(records), keyboard=main_menu_kb())


@router.message(Command("calc"))
async def cmd_calc(message, state: FSMContext, bot):
    if await _check_private_access(message):
        await _start_quick_calc(state, message.answer, message.sender.user_id,
                                getattr(message.sender, "first_name", None) or str(message.sender.user_id))


@router.message_callback(F.payload == "help")
async def on_help(callback, bot):
    await callback.answer(text=HELP_TEXT, keyboard=main_menu_kb())


@router.message_callback(F.payload == "my_appraisals")
async def on_my_appraisals(callback, bot):
    user_id = _get_user_id(callback)
    if not _allowed(user_id):
        await callback.answer(text=f"У вас нет доступа к этому боту. Ваш ID: {user_id}.")
        return
    await callback.answer(text=_format_my_appraisals(appraisals_log.for_user(user_id)), keyboard=main_menu_kb())


@router.message_callback(F.payload == "quick_calc")
async def on_quick_calc(callback, state: FSMContext, bot):
    user_id = _get_user_id(callback)
    if not _allowed(user_id):
        await callback.answer(text=f"У вас нет доступа к этому боту. Ваш ID: {user_id}.")
        return
    await _start_quick_calc(state, callback.answer, user_id, _get_user_name(callback) or str(user_id))


@router.message_callback(F.payload.startswith("edit:"), StateFilter(AppraisalStates.reviewing_result))
async def on_edit_field(callback, state: FSMContext, bot):
    """Исправить один ответ на экране итога без новой оценки: бот спросит только его
    и сразу вернётся к итогу с пересчитанным статусом."""
    field = callback.payload.split(":", 1)[1]
    if field not in EDITABLE_FIELDS:
        return
    update = {field: None}
    if field == "extra":
        update["_extra_manual"] = True  # не подставлять «Более 3х ДТП» автоматически повторно
    await state.update_data(**update)
    await _ask_next(state, callback.answer, bot, note=f"✏️ Исправляем: {EDITABLE_FIELDS[field]}")


@router.message(Command("new"))
async def cmd_new(message, state: FSMContext, bot):
    if not _is_private_chat(message):
        return
    if not _allowed(message.sender.user_id):
        await _deny_and_request_access(message, message.sender.user_id)
        return
    await _start_new_appraisal(state, message.answer)


@router.message_callback(F.payload == "new_appraisal")
async def on_new_appraisal_callback(callback, state: FSMContext, bot):
    user_id = _get_user_id(callback)
    if not _allowed(user_id):
        await callback.answer(text=f"У вас нет доступа к этому боту. Ваш ID: {user_id}.")
        return
    await _start_new_appraisal(state, callback.answer)


@router.message(StateFilter(None))
async def on_free_text(message, state: FSMContext, bot):
    """Если пользователь просто пишет боту вне сценария — считаем это началом,
    если только он сейчас не должен ввести скорректированную цену/ПЦП после
    нажатия «Согласовано с корректировкой». В групповых чатах игнорируем
    произвольный текст полностью — там бот реагирует только на команды."""
    if not _is_private_chat(message):
        return

    token, deal = deals_store.find_awaiting(message.sender.user_id)
    if token:
        await _handle_adjustment_input(message, token, deal, bot)
        return

    if not _allowed(message.sender.user_id):
        await _deny_and_request_access(message, message.sender.user_id)
        return
    await state.set_state(AppraisalStates.waiting_link)
    await on_waiting_link(message, state, bot)


@router.message(StateFilter(AppraisalStates.waiting_link))
async def on_waiting_link(message, state: FSMContext, bot):
    text = _text(message)
    manager_name = getattr(message.sender, "first_name", None) or str(message.sender.user_id)

    if text.lower() in ("вручную", "manual"):
        await state.clear()  # новая оценка — не тащим ответы из прошлой
        await state.update_data(_manager_name=manager_name, _manager_user_id=message.sender.user_id)
        await state.set_state(AppraisalStates.manual_vin)
        await message.answer(text="Введите ВИН авто:")
        return

    if not text:
        await message.answer(text="Пришлите ссылку на оценку, VIN или номер сделки текстом, либо напишите «вручную».")
        return

    try:
        data = await asyncio.to_thread(fetch_appraisal, text)
    except Exception as e:  # noqa: BLE001 — MaxPosterError или сетевой сбой
        if not isinstance(e, MaxPosterError):
            logger.exception("Ошибка запроса к MaxPoster: %r", text)
        await message.answer(
            text=(
                f"Не получилось найти оценку ({e}).\n"
                "Пришлите ссылку/VIN/номер сделки ещё раз или напишите «вручную»."
            )
        )
        return

    colors_bucket = None
    if data.repainted_parts_count is not None:
        colors_bucket = colors_bucket_from_count(data.repainted_parts_count)

    salon = _resolve_salon(data.dealer_name)

    await state.clear()  # новая оценка — не тащим ответы из прошлой
    await state.update_data(
        _manager_name=manager_name,
        _manager_user_id=message.sender.user_id,
        vin=data.vin,
        avito_price=data.avito_price,
        accidents_count=data.accidents_count,
        repair_cost_min=data.repair_cost_min,
        repair_cost_max=data.repair_cost_max,
        autoteka_url=data.autoteka_url,
        colors=colors_bucket,  # None, если не удалось определить
        appraisal_purchase_cost=data.appraisal_purchase_cost,
        appraisal_sale_cost=data.appraisal_sale_cost,
        presale_preparation_cost=data.presale_preparation_cost,
        brand=data.brand,
        model=data.model,
        year=data.year,
        mileage=data.mileage,
        appraiser_name=data.appraiser_name,
        dealer_name=data.dealer_name,
        appraisal_id=data.appraisal_id,
        salon=salon,
    )

    vehicle_line = " ".join(str(x) for x in [data.brand, data.model, data.year] if x)
    auto = []  # что подставили сами — оценщику не нужно вводить
    if data.year:
        auto.append(f"год — {data.year}")
    if data.mileage is not None:
        auto.append(f"пробег — {_fmt(data.mileage)} км")
    if colors_bucket:
        auto.append(f"окрасы — «{colors_bucket}» (деталей: {data.repainted_parts_count})")
    if data.presale_preparation_cost:
        auto.append(f"переподготовка — {_fmt(data.presale_preparation_cost)} ₽")
    if data.appraisal_sale_cost is not None:
        auto.append(f"цена продажи ДЦ — {_fmt(data.appraisal_sale_cost)} ₽")
    if data.appraisal_purchase_cost is not None:
        auto.append(f"цена выкупа — {_fmt(data.appraisal_purchase_cost)} ₽")
    salon_line = (f"🏢 Автосалон: {salon_label(salon)}" if salon
                  else f"🏢 Автосалон: {data.dealer_name or '—'} (нет в таблице — выберите ниже)")
    note = (
        f"Нашёл оценку.\n{salon_line}\n{vehicle_line + chr(10) if vehicle_line else ''}"
        f"ВИН: {data.vin or '—'}, Оценщик: {data.appraiser_name or '—'}\n"
        f"Авито-оценка: {_fmt(data.avito_price)} ₽, ДТП: {data.accidents_count}"
        + ("\n\nИз MaxPoster подставлено: " + "; ".join(auto) + "." if auto else "")
    )
    await _ask_next(state, message.answer, bot, note=note)


def _resolve_salon(dealer_name: Optional[str]) -> Optional[str]:
    """Салон из таблицы «ПЦП автосалона» по названию из MaxPoster. Если его там
    нет — оценщик выберет кнопкой, а в лог пишем подсказку для администратора."""
    salon = find_salon(dealer_name)
    if dealer_name and not salon:
        logger.warning("Салона «%s» нет в разделе «ПЦП автосалона» вкладки «Параметры Метрики v2» — "
                       "оценщик выберет салон кнопкой. Добавьте строку с этим названием.", dealer_name)
    return salon


@router.message(StateFilter(AppraisalStates.manual_vin))
async def on_manual_vin(message, state: FSMContext, bot):
    vin = _text(message).upper()
    if not vin:
        await message.answer(text="Пришлите ВИН текстом:")
        return
    await state.update_data(vin=vin)
    await state.set_state(AppraisalStates.manual_avito)
    await message.answer(text="Введите прогнозную Авито-оценку (число, ₽):")


@router.message(StateFilter(AppraisalStates.manual_avito))
async def on_manual_avito(message, state: FSMContext, bot):
    try:
        avito_price = _parse_positive(_text(message))
    except ValueError:
        await message.answer(text="Не понял число, попробуйте ещё раз (например: 1200000)")
        return
    await state.update_data(
        avito_price=avito_price, accidents_count=None, colors=None,
        repair_cost_min=None, repair_cost_max=None, autoteka_url=None,
        appraisal_purchase_cost=None, appraisal_sale_cost=None, presale_preparation_cost=None,
        salon=None, year=None, mileage=None,
    )
    await _ask_next(state, message.answer, bot)


@router.message_callback(F.payload.startswith("reception:"), StateFilter(AppraisalStates.reception_type))
async def on_reception(callback, state: FSMContext, bot):
    value = callback.payload.split(":", 1)[1]
    await state.update_data(reception_type=value)
    await _ask_next(state, callback.answer, bot, note=f"Тип приёма: {value}")


@router.message_callback(F.payload.startswith("salon:"), StateFilter(AppraisalStates.salon))
async def on_salon(callback, state: FSMContext, bot):
    value = callback.payload.split(":", 1)[1]
    if value not in get_salon_options():
        await callback.answer(text="Такого салона нет в таблице. Выберите из списка:", keyboard=salon_kb())
        return
    data = await state.get_data()
    update = {"salon": value}
    if not data.get("dealer_name"):
        # ручной ввод: салон из таблицы = салон для маршрутизации карточки согласования
        update["dealer_name"] = value
    await state.update_data(**update)
    await _ask_next(state, callback.answer, bot, note=f"Автосалон: {salon_label(value)}")


@router.message(StateFilter(AppraisalStates.year))
async def on_year(message, state: FSMContext, bot):
    text = _text(message)
    current_year = datetime.date.today().year
    if not text.isdigit() or not (1950 <= int(text) <= current_year + 1):
        await message.answer(text=f"Не понял год. Введите год выпуска числом, например {current_year - 5}:")
        return
    await state.update_data(year=int(text))
    await _ask_next(state, message.answer, bot)


@router.message(StateFilter(AppraisalStates.mileage))
async def on_mileage(message, state: FSMContext, bot):
    text = _text(message).replace(" ", "").replace("\u00a0", "")
    if not text.isdigit():
        await message.answer(text="Не понял пробег. Введите число километров, например 90000:")
        return
    await state.update_data(mileage=int(text))
    await _ask_next(state, message.answer, bot)


@router.message_callback(F.payload.startswith("colors:"), StateFilter(AppraisalStates.colors))
async def on_colors(callback, state: FSMContext, bot):
    value = callback.payload.split(":", 1)[1]
    await state.update_data(colors=value)
    await _ask_next(state, callback.answer, bot, note=f"Окрасы: {value}")


@router.message_callback(F.payload.startswith("condition:"), StateFilter(AppraisalStates.condition))
async def on_condition(callback, state: FSMContext, bot):
    value = callback.payload.split(":", 1)[1]
    await state.update_data(condition=value)
    await _ask_next(state, callback.answer, bot, note=f"Тех. состояние: {value}")


@router.message_callback(F.payload.startswith("extra:"), StateFilter(AppraisalStates.extra))
async def on_extra(callback, state: FSMContext, bot):
    value = callback.payload.split(":", 1)[1]
    note = ""
    data = await state.get_data()
    # «Не применимо» для авто младше 2 лет — по метрике это «Возраст авто до 2х лет»
    if (value == EXTRA_NOT_APPLICABLE and is_young_car(data.get("year"))
            and EXTRA_YOUNG_CAR in get_extra_options()):
        value = EXTRA_YOUNG_CAR
        note = f"(доп. данные исправлены на «{EXTRA_YOUNG_CAR}»: год выпуска {data.get('year')})"
    await state.update_data(extra=value)
    await _ask_next(state, callback.answer, bot, note=note)


async def _handle_planned_sale_price(text: str, state: FSMContext, answer_fn, bot):
    try:
        planned_sale_price = _parse_amount(text)
    except ValueError:
        await answer_fn(text="Не понял число. Введите сумму или нажмите «Пропустить».")
        return

    data = await state.get_data()
    if planned_sale_price is None:
        planned_sale_price = data.get("appraisal_sale_cost")  # None, если не было в системе

    await state.update_data(manager_resale_forecast=planned_sale_price)

    negotiated_default = data.get("appraisal_purchase_cost")
    if negotiated_default is not None:
        await state.update_data(negotiated_price=negotiated_default)
        data = await state.get_data()
        await _review_or_close(state, answer_fn, data)
        return

    await state.set_state(AppraisalStates.waiting_negotiated)
    await answer_fn(
        text="Введите цену, согласованную с клиентом (₽), или нажмите «Пропустить».",
        keyboard=skip_kb("skip_negotiated"),
    )


async def _handle_negotiated(text: str, state: FSMContext, answer_fn, bot):
    try:
        negotiated_price = _parse_amount(text)
    except ValueError:
        await answer_fn(text="Не понял число. Введите сумму или нажмите «Пропустить».")
        return

    await state.update_data(negotiated_price=negotiated_price)
    data = await state.get_data()
    await _review_or_close(state, answer_fn, data)


@router.message(StateFilter(AppraisalStates.waiting_planned_sale_price))
async def on_planned_sale_price(message, state: FSMContext, bot):
    await _handle_planned_sale_price(_text(message), state, message.answer, bot)


@router.message_callback(F.payload == "skip_planned", StateFilter(AppraisalStates.waiting_planned_sale_price))
async def on_planned_sale_price_skip(callback, state: FSMContext, bot):
    await _handle_planned_sale_price("пропустить", state, callback.answer, bot)


@router.message(StateFilter(AppraisalStates.waiting_negotiated))
async def on_waiting_negotiated(message, state: FSMContext, bot):
    await _handle_negotiated(_text(message), state, message.answer, bot)


@router.message_callback(F.payload == "skip_negotiated", StateFilter(AppraisalStates.waiting_negotiated))
async def on_waiting_negotiated_skip(callback, state: FSMContext, bot):
    await _handle_negotiated("пропустить", state, callback.answer, bot)


@router.message()
async def fallback(message, bot):
    if not _is_private_chat(message):
        return
    await message.answer(text="Воспользуйтесь кнопками выше или меню ниже.", keyboard=main_menu_kb())


dp.include_router(router)


# ---------------------------------------------------------------------------
# Напоминания: сделка долго ждёт решения -> повторно присылаем карточку с кнопками
# ---------------------------------------------------------------------------
REMINDER_CHECK_EVERY_SECONDS = 10 * 60


def _waiting_text(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


async def _remind_deal(bot, token: str, deal: dict, waited: float) -> bool:
    stage = deal.get("stage")
    prefix = f"⏰ НАПОМИНАНИЕ: сделка ждёт решения {_waiting_text(waited)}\n\n"
    if stage == "salon_pending":
        chat_id = dealer_chats.get_salon_chat_id(deal.get("dealer_name"))
        return bool(chat_id) and await _send_to_chat(bot, chat_id, prefix + _deal_card_text(deal),
                                                     keyboard=deal_approval_kb(token))
    if stage == "uk_pending":
        text = prefix + _deal_card_text(deal)
        sent = False
        for uk_id in approval_hierarchy.get_uk_approvers(deal.get("dealer_name")):
            sent = await _send_to_user(bot, uk_id, text, keyboard=uk_approval_kb(token)) or sent
        if not sent:
            chat_id = dealer_chats.get_salon_chat_id(deal.get("dealer_name"))
            sent = bool(chat_id) and await _send_to_chat(bot, chat_id, text, keyboard=uk_approval_kb(token))
        return sent
    if stage == "pats_pending":
        chat_id = dealer_chats.get_pats_chat_id()
        return bool(chat_id) and await _send_to_chat(
            bot, chat_id, prefix + _pats_card_text(deal, deal.get("final_approved_by_role")),
            keyboard=pats_done_kb(token))
    return False


REMINDER_MAX_AGE_SECONDS = 24 * 60 * 60  # сделки, которые ждут дольше суток, не трогаем


async def _check_reminders(bot) -> None:
    """Одно напоминание на этап сделки, и только если:
    - сделка создана этой версией бота и её карточка действительно ушла в чат
      (card_sent) — старые и «согласуйте по старому процессу» сделки из файла
      не трогаем никогда;
    - сделка ждёт на этапе дольше REMINDER_AFTER_MINUTES, но меньше суток;
    - по этому этапу ещё не напоминали (reminded_stage)."""
    now = time.time()
    if not (REMINDER_HOURS[0] <= datetime.datetime.now().hour < REMINDER_HOURS[1]):
        return
    after = REMINDER_AFTER_MINUTES * 60
    for token, deal in deals_store.list_deals().items():
        stage = deal.get("stage")
        if stage not in ("salon_pending", "uk_pending", "pats_pending"):
            continue
        if not deal.get("card_sent") or not deal.get("stage_since"):
            continue
        if deal.get("reminded_stage") == stage:
            continue
        waited = now - deal["stage_since"]
        if waited < after or waited > REMINDER_MAX_AGE_SECONDS:
            continue
        # отмечаем ДО отправки: даже если отправка сорвётся, повторов не будет
        deals_store.update_deal(token, reminded_stage=stage)
        if await _remind_deal(bot, token, deal, waited):
            logger.info("[reminder] token=%s stage=%s напоминание отправлено", token, stage)


async def _reminder_loop(bot) -> None:
    if REMINDER_AFTER_MINUTES <= 0:
        logger.info("Напоминания согласующим выключены (REMINDER_AFTER_MINUTES=0)")
        return
    while True:
        await asyncio.sleep(REMINDER_CHECK_EVERY_SECONDS)
        try:
            await _check_reminders(bot)
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка при проверке напоминаний")


async def _set_commands_menu():
    """Заполняет нативное меню команд MAX (кнопка '/'), чтобы не нужно было
    запоминать команды — они появятся со своими описаниями в списке."""
    try:
        await bot.set_my_commands([
            {"name": "menu", "description": "Главное меню"},
            {"name": "new", "description": "Начать новую оценку"},
            {"name": "calc", "description": "Предварительный расчёт (без отправки)"},
            {"name": "my", "description": "Мои оценки и их статусы"},
            {"name": "help", "description": "Как пользоваться ботом"},
            {"name": "myid", "description": "Узнать свой ID в MAX"},
            {"name": "admin", "description": "Меню администратора (заявки/пользователи/салоны)"},
            {"name": "pending", "description": "[админ] Заявки на доступ"},
            {"name": "users", "description": "[админ] Список пользователей с доступом"},
            {"name": "salons", "description": "[админ] Список привязанных салонов"},
            {"name": "register_salon", "description": "[админ] Привязать этот чат к салону"},
            {"name": "register_pats", "description": "[админ] Назначить этот чат общим чатом ПАЦ"},
            {"name": "raw", "description": "[админ] Все поля оценки из MaxPoster"},
            {"name": "report", "description": "[админ] Отчёт по салонам за 7 дней (/report 30 — за 30)"},
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось задать меню команд: %s", e)


async def _main():
    await _set_commands_menu()
    reminder_task = asyncio.create_task(_reminder_loop(bot))  # noqa: F841 — живёт, пока работает бот
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_main())
