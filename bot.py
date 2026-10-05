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
import logging
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

from config import MAX_BOT_TOKEN, ALLOWED_USER_IDS, ADMIN_USER_IDS, OWNER_USER_ID
from pricing_engine import (
    AppraisalInput, calc_appraisal, colors_bucket_from_count,
    get_colors_options, get_condition_options, get_extra_options, RECEPTION_TYPES,
)
from maxposter_client import fetch_appraisal, MaxPosterError
from sheets_logger import log_appraisal
import access_store
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


class AppraisalStates(StatesGroup):
    waiting_link = State()
    manual_vin = State()
    manual_avito = State()
    reception_type = State()
    colors = State()
    condition = State()
    extra = State()
    waiting_planned_sale_price = State()
    waiting_negotiated = State()
    reviewing_result = State()


def _fmt(n) -> str:
    return f"{n:,.0f}".replace(",", " ")


def _parse_amount(text: str):
    """Возвращает float или None, если это 'пропустить'/'skip'/'-'. Бросает ValueError, если не число."""
    if text.strip().lower() in ("пропустить", "skip", "-"):
        return None
    return float(text.strip().replace(" ", "").replace(",", "."))


def _maxposter_link(appraisal_id) -> Optional[str]:
    if not appraisal_id:
        return None
    return f"https://app.maxposter.ru/holding/used/appraisals/{appraisal_id}/view"


def _vehicle_header(vehicle_info: dict, reception_type: Optional[str] = None) -> str:
    """Шапка с типом контракта, авто и ссылкой на оценку — идёт в начале
    и итогового расчёта, и карточки согласования."""
    vehicle_line = " ".join(
        str(x) for x in [vehicle_info.get("brand"), vehicle_info.get("model"), vehicle_info.get("year")] if x
    )
    lines = []
    if reception_type:
        lines.append(f"Тип контракта: {reception_type}")
    if vehicle_line:
        lines.append(vehicle_line)
    if vehicle_info.get("mileage"):
        lines.append(f"Пробег: {_fmt(vehicle_info['mileage'])} км")
    link = _maxposter_link(vehicle_info.get("appraisal_id"))
    if link:
        lines.append(f"Оценка в MaxPoster: {link}")
    return "\n".join(lines)


def _result_text(result, avito_price: float, extra_info: dict, vehicle_info: dict, reception_type: Optional[str] = None) -> str:
    header = _vehicle_header(vehicle_info, reception_type)
    lines = [header, ""] if header else []
    lines += [
        f"Категория: {result.category} (баллы: {result.score})",
        f"Авито-оценка: {_fmt(avito_price)} ₽",
        "",
        "Плановая цена продажи (расчёт системы):",
        f"  РОП: {_fmt(result.planned_sale_price_rop)} ₽ | ДДЦ: {_fmt(result.planned_sale_price_ddc)} ₽",
        "",
        "Потолок цены выкупа клиенту:",
        f"  Согласовано (РОП): до {_fmt(result.purchase_price_rop)} ₽",
        f"  Согласование ДДЦ: до {_fmt(result.purchase_price_ddc)} ₽",
        "  Выше — нужно согласование УК",
        "",
        f"Целевая валовая прибыль: {_fmt(result.gross_margin_rop)} ₽",
    ]
    if extra_info.get("repair_cost_max"):
        lines += [
            "",
            f"Расчёт стоимости ремонта (по данным осмотра): "
            f"{_fmt(extra_info['repair_cost_min'])}–{_fmt(extra_info['repair_cost_max'])} ₽",
        ]
    if extra_info.get("autoteka_url"):
        lines += [f"Отчёт Автотеки: {extra_info['autoteka_url']}"]
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


def skip_kb(payload: str):
    builder = InlineKeyboardBuilder()
    builder.callback(text="Пропустить", payload=payload)
    builder.adjust(1)
    return builder


def new_appraisal_kb():
    builder = InlineKeyboardBuilder()
    builder.callback(text="🆕 Новая оценка", payload="new_appraisal")
    builder.adjust(1)
    return builder


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
                         presale_preparation_cost=None) -> str:
    header = _vehicle_header(vehicle_info, reception_type)
    lines = ["📋 СОГЛАСОВАНИЕ СДЕЛКИ", f"Оценщик: {vehicle_info.get('appraiser_name') or manager_name}"]
    if header:
        lines.append(header)
    lines += [
        f"VIN: {vin or '—'}",
        f"Категория: {result.category}",
        f"Авито-оценка: {_fmt(appraisal_input.avito_price)} ₽",
    ]
    if appraisal_input.negotiated_price is not None:
        lines.append(f"Запрашиваемая цена выкупа: {_fmt(appraisal_input.negotiated_price)} ₽")
    if appraiser_sale_cost is not None:
        lines.append(f"ПЦП Оценщика: {_fmt(appraiser_sale_cost)} ₽")
        prep_for_calc = presale_preparation_cost or 0
        lines.append(
            f"Подготовка: {_fmt(presale_preparation_cost)} ₽" if presale_preparation_cost
            else "Подготовка: расходы не запланированы"
        )
        if appraisal_input.negotiated_price is not None:
            vp = appraiser_sale_cost - appraisal_input.negotiated_price - prep_for_calc
            lines.append(f"ВП (валовая прибыль): {_fmt(vp)} ₽")
    lines.append(f"Статус: {result.approval_status or '—'}")
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
                                manager_user_id=None) -> str:
    """Создаёт карточку согласования и постит её в чат салона (Этап 1).
    Возвращает короткую заметку для ответа оценщику о том, что произошло."""
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
        "stage": "salon_pending",
    })

    if not salon_chat_id:
        return (
            "\n\n⚠️ Салон "
            + (f"«{dealer_name}» " if dealer_name else "")
            + "не привязан к чату согласования — пока согласуйте по старому процессу."
        )

    text = _approval_card_text(vehicle_info, appraisal_input, result, manager_name, vin,
                                reception_type=appraisal_input.reception_type,
                                appraiser_sale_cost=appraiser_sale_cost,
                                presale_preparation_cost=presale_preparation_cost)
    sent = await _send_to_chat(bot, salon_chat_id, text, keyboard=deal_approval_kb(token))
    if sent:
        return f"\n\n✅ Карточка отправлена на согласование в чат салона «{dealer_name}»."
    return "\n\n⚠️ Не получилось отправить карточку в чат салона — согласуйте по старому процессу."


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


async def _proceed_after_condition(state: FSMContext, answer_fn, bot):
    """После тех.состояния — либо авто-ДТП, либо спрашиваем доп.данные руками."""
    data = await state.get_data()
    accidents_count = data.get("accidents_count")
    if accidents_count is not None and accidents_count > ACCIDENTS_AUTO_THRESHOLD:
        await state.update_data(extra="Более 3х ДТП")
        await _finish_scoring(state, answer_fn, bot, auto_note=(
            f"\n(автоматически: по данным оценки — {accidents_count} ДТП, это > {ACCIDENTS_AUTO_THRESHOLD})"
        ))
    else:
        await state.set_state(AppraisalStates.extra)
        await answer_fn(text="Доп. данные?", keyboard=extra_kb())


async def _finish_scoring(state: FSMContext, answer_fn, bot, auto_note: str = ""):
    """Считаем предварительный результат и решаем, что спрашивать дальше —
    план.цену продажи и/или согласованную цену (пропускаем то, что уже
    известно из системы)."""
    data = await state.get_data()
    appraisal_input = AppraisalInput(
        avito_price=data["avito_price"],
        reception_type=data["reception_type"],
        colors=data["colors"],
        condition=data["condition"],
        extra=data["extra"],
    )
    result = calc_appraisal(appraisal_input)

    extra_info = {
        "repair_cost_min": data.get("repair_cost_min"),
        "repair_cost_max": data.get("repair_cost_max"),
        "autoteka_url": data.get("autoteka_url"),
    }
    header = _result_text(result, appraisal_input.avito_price, extra_info,
                           _vehicle_info(data), data.get("reception_type")) + auto_note

    planned_default = data.get("appraisal_sale_cost")
    negotiated_default = data.get("appraisal_purchase_cost")

    if planned_default is not None:
        await state.update_data(manager_resale_forecast=planned_default)
    if negotiated_default is not None:
        await state.update_data(negotiated_price=negotiated_default)

    if planned_default is not None and negotiated_default is not None:
        # оба значения уже есть в системе — сразу показываем на подтверждение
        data = await state.get_data()
        await _review_or_close(state, answer_fn, data)
        return

    if planned_default is None:
        await state.set_state(AppraisalStates.waiting_planned_sale_price)
        await answer_fn(
            text=header
            + "\n\nВведите планируемую цену продажи (₽), или нажмите «Пропустить» для расчёта системы.",
            keyboard=skip_kb("skip_planned"),
        )
        return

    # план.цена уже есть, а согласованной цены — нет, спрашиваем её
    await state.set_state(AppraisalStates.waiting_negotiated)
    await answer_fn(
        text=header + "\n\nВведите цену, согласованную с клиентом (₽), или нажмите «Пропустить».",
        keyboard=skip_kb("skip_negotiated"),
    )


def review_kb():
    builder = InlineKeyboardBuilder()
    builder.callback(text="📨 Отправить на согласование", payload="confirm_submit")
    builder.callback(text="🔄 Обновить из MaxPoster", payload="update_price")
    builder.adjust(1)
    return builder


async def _review_or_close(state: FSMContext, answer_fn, data: dict):
    """Показывает статус сделки с ценой выкупа и даёт оценщику выбор:
    отправить на согласование как есть, или поменять цену и посчитать заново.
    Автоматической отправки на согласование больше нет — только по кнопке.
    Шапка (категория/потолок/целевая ВП) пересчитывается и показывается
    заново при каждом вызове — в том числе после «Обновить из MaxPoster» и
    после повторного ввода цены, чтобы картина никогда не была урезанной."""
    appraisal_input = AppraisalInput(
        avito_price=data["avito_price"],
        reception_type=data["reception_type"],
        colors=data["colors"],
        condition=data["condition"],
        extra=data["extra"],
        manager_resale_forecast=data.get("manager_resale_forecast"),
        negotiated_price=data.get("negotiated_price"),
    )
    result = calc_appraisal(appraisal_input)

    extra_info = {
        "repair_cost_min": data.get("repair_cost_min"),
        "repair_cost_max": data.get("repair_cost_max"),
        "autoteka_url": data.get("autoteka_url"),
    }
    full_header = _result_text(result, appraisal_input.avito_price, extra_info,
                                _vehicle_info(data), data.get("reception_type"))
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
        await asyncio.to_thread(log_appraisal, manager_name, data.get("vin"), appraisal_input, result, vehicle_info)
        await state.clear()
        return

    appraiser_ppp = data.get("appraisal_sale_cost")
    ppp_line = f"ПЦП Оценщика: {_fmt(appraiser_ppp)} ₽\n" if appraiser_ppp is not None else ""
    appraiser_name = data.get("appraiser_name")
    appraiser_line = f"Оценщик: {appraiser_name}\n" if appraiser_name else ""

    prep_cost = data.get("presale_preparation_cost")
    if not prep_cost:  # None или 0
        prep_line = "Подготовка: расходы не запланированы\n"
        prep_for_calc = 0
    else:
        prep_line = f"Подготовка: {_fmt(prep_cost)} ₽\n"
        prep_for_calc = prep_cost

    vp_line = ""
    if appraiser_ppp is not None:
        vp = appraiser_ppp - appraisal_input.negotiated_price - prep_for_calc
        vp_line = f"ВП (валовая прибыль): {_fmt(vp)} ₽\n"

    text = (
        prefix
        + "СОГЛАСОВАНИЕ СДЕЛКИ\n"
        + appraiser_line
        + f"Запрашиваемая цена выкупа: {_fmt(appraisal_input.negotiated_price)} ₽\n"
        + ppp_line
        + prep_line
        + vp_line
        + f"Статус: {result.approval_status}\n"
        f"GM2 по факту: {_fmt(result.gm2_negotiated)} ₽"
    )
    await state.set_state(AppraisalStates.reviewing_result)
    await answer_fn(text=text, keyboard=review_kb())


@router.message_callback(F.payload == "confirm_submit", StateFilter(AppraisalStates.reviewing_result))
async def on_confirm_submit(callback, state: FSMContext, bot):
    data = await state.get_data()
    appraisal_input = AppraisalInput(
        avito_price=data["avito_price"],
        reception_type=data["reception_type"],
        colors=data["colors"],
        condition=data["condition"],
        extra=data["extra"],
        manager_resale_forecast=data.get("manager_resale_forecast"),
        negotiated_price=data.get("negotiated_price"),
    )
    result = calc_appraisal(appraisal_input)
    manager_name = data.get("_manager_name") or "неизвестно"
    vehicle_info = _vehicle_info(data)
    approval_note = await _submit_for_approval(
        bot, appraisal_input, result, vehicle_info, manager_name, data.get("vin"),
        appraiser_sale_cost=data.get("appraisal_sale_cost"),
        presale_preparation_cost=data.get("presale_preparation_cost"),
        manager_user_id=data.get("_manager_user_id"),
    )
    await callback.answer(
        text="✅ Отправлено на согласование." + approval_note
        + "\n\nГотово. Чтобы посчитать следующее авто — жмите кнопку ниже.",
        keyboard=new_appraisal_kb(),
    )
    await asyncio.to_thread(log_appraisal, manager_name, data.get("vin"), appraisal_input, result, vehicle_info)
    await state.clear()


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
    except MaxPosterError as e:
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
            "Привет! Я считаю выкупную цену авто в трейд-ин.\n\n"
            "Пришлите ссылку на оценку из MaxPoster, VIN или номер сделки, "
            "или напишите «вручную», чтобы ввести данные без ссылки."
        )
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
    )


def _pats_card_text(deal: dict, approved_role_label: str = "") -> str:
    status_line = f"Статус: {approved_role_label}\n" if approved_role_label else ""
    link = _maxposter_link((deal.get("vehicle") or {}).get("appraisal_id"))
    link_line = f"Оценка в MaxPoster: {link}\n" if link else ""
    return (
        "📋 НА ПОДТВЕРЖДЕНИЕ В АВТОХАБ (ПАЦ)\n"
        f"Салон: {deal.get('dealer_name') or '—'}\n"
        f"VIN: {deal.get('vin') or '—'}\n"
        + link_line +
        f"Оценщик: {(deal.get('vehicle') or {}).get('appraiser_name') or deal.get('manager_name')}\n"
        f"Цена выкупа: {_fmt(deal['negotiated_price']) if deal.get('negotiated_price') else '—'} ₽\n"
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


async def _advance_after_salon_approval(bot, token: str, deal: dict, role: Optional[str], approver_name: Optional[str] = None) -> str:
    """Общая логика после согласования на этапе чата салона (только для
    настоящего «Согласовано» — «с корректировкой» обрабатывается отдельно,
    см. _return_for_correction, и дальше по цепочке НЕ идёт)."""
    status = deal.get("approval_status")
    card = _deal_card_text(deal)

    if status == "Согласование УК":
        approved_role = role or "ДДЦ"
        deals_store.update_deal(token, stage="uk_pending", ddc_approved_role=approved_role)
        uk_ids = approval_hierarchy.get_uk_approvers(deal.get("dealer_name"))
        uk_text = (
            f"Ожидается ваше согласование сделки в рабочем чате «{deal.get('dealer_name') or '—'}».\n\n" + card
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
        return card + f"\n\n✅ Согласовано {approved_role}{name_suffix2}." + note

    name_suffix = f": {approver_name}" if approver_name else ""
    final_role_label = f"Согласовано {role}{name_suffix}" if role else "Заявка согласована"
    note = await _send_to_pats(bot, token, deal, approved_role_label=final_role_label)
    return card + f"\n\n✅ {final_role_label}." + note


async def _advance_after_uk_approval(bot, token: str, deal: dict, approver_name: Optional[str] = None) -> str:
    card = _deal_card_text(deal)
    name_suffix = f": {approver_name}" if approver_name else ""
    final_role_label = f"Согласовано УК{name_suffix}"
    note = await _send_to_pats(bot, token, deal, approved_role_label=final_role_label)
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

    card = _deal_card_text(deal)
    deals_store.delete_deal(token)
    return card + "\n\n🔄 Оценка возвращена на корректировку." + note


async def _handle_adjustment_input(message, token: str, deal: dict, bot):
    """Обрабатывает ответ согласующего на вопрос о новой цене/ПЦП после
    нажатия «Согласовано с корректировкой» (личным сообщением боту)."""
    field = deal.get("awaiting_field")
    text = (message.body.text or "").strip()
    try:
        value = float(text.replace(" ", "").replace(",", "."))
    except ValueError:
        await message.answer(text="Не понял число, введите ещё раз (например: 950000)")
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
    deals_store.update_deal(token, awaiting_input_from=user_id, awaiting_field="price", awaiting_source=source)
    card = _deal_card_text(deal)
    sent = await _send_to_user(bot, user_id, "Введите новую цену выкупа клиенту (₽):")
    if sent:
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
    if approvers and user_id not in approvers:
        role_hint = " (нужен ДДЦ)" if status == "Согласование УК" else ""
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
    if approvers and user_id not in approvers:
        role_hint = " (нужен ДДЦ)" if status == "Согласование УК" else ""
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
    if approvers and user_id not in approvers:
        await callback.answer(notification=f"У вас нет прав отклонять сделку со статусом «{deal.get('approval_status')}».")
        return

    card = _deal_card_text(deal)
    approver_name = _get_user_name(callback)
    name_suffix = f" ({approver_name})" if approver_name else ""
    deals_store.delete_deal(token)
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
    if uk_ids and user_id not in uk_ids:
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
    if uk_ids and user_id not in uk_ids:
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
    if uk_ids and user_id not in uk_ids:
        await callback.answer(notification="У вас нет прав отклонять эту сделку (нужен УК).")
        return

    card = _deal_card_text(deal)
    approver_name = _get_user_name(callback)
    name_suffix = f" ({approver_name})" if approver_name else ""
    deals_store.delete_deal(token)
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
    if pats_approvers and user_id not in pats_approvers:
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
    if pats_approvers and user_id not in pats_approvers:
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
    text = (message.body.text or "").strip()
    manager_name = getattr(message.sender, "first_name", None) or str(message.sender.user_id)

    if text.lower() in ("вручную", "manual"):
        await state.update_data(_manager_name=manager_name, _manager_user_id=message.sender.user_id)
        await state.set_state(AppraisalStates.manual_vin)
        await message.answer(text="Введите ВИН авто:")
        return

    try:
        data = await asyncio.to_thread(fetch_appraisal, text)
    except MaxPosterError as e:
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
    )
    await state.set_state(AppraisalStates.reception_type)

    colors_note = (
        f"окрасов (эвристика): {data.repainted_parts_count} → «{colors_bucket}»"
        if colors_bucket else "окрасы определить не удалось, спрошу отдельно"
    )
    vehicle_line = " ".join(
        str(x) for x in [data.brand, data.model, data.year] if x
    )
    await message.answer(
        text=(
            f"Нашёл оценку. {vehicle_line + chr(10) if vehicle_line else ''}"
            f"ВИН: {data.vin or '—'}, "
            f"Пробег: {_fmt(data.mileage) if data.mileage else '—'} км, "
            f"Оценщик: {data.appraiser_name or '—'}\n"
            f"Авито-оценка: {_fmt(data.avito_price)} ₽, "
            f"ДТП: {data.accidents_count}, {colors_note}.\n\nВыберите тип приёма:"
        ),
        keyboard=reception_kb(),
    )


@router.message(StateFilter(AppraisalStates.manual_vin))
async def on_manual_vin(message, state: FSMContext, bot):
    await state.update_data(vin=message.body.text.strip())
    await state.set_state(AppraisalStates.manual_avito)
    await message.answer(text="Введите прогнозную Авито-оценку (число, ₽):")


@router.message(StateFilter(AppraisalStates.manual_avito))
async def on_manual_avito(message, state: FSMContext, bot):
    text = message.body.text.strip()
    try:
        avito_price = float(text.replace(" ", "").replace(",", "."))
    except ValueError:
        await message.answer(text="Не понял число, попробуйте ещё раз (например: 1200000)")
        return
    await state.update_data(
        avito_price=avito_price, accidents_count=None, colors=None,
        repair_cost_min=None, repair_cost_max=None, autoteka_url=None,
        appraisal_purchase_cost=None, appraisal_sale_cost=None, presale_preparation_cost=None,
    )
    await state.set_state(AppraisalStates.reception_type)
    await message.answer(text="Выберите тип приёма:", keyboard=reception_kb())


@router.message_callback(F.payload.startswith("reception:"), StateFilter(AppraisalStates.reception_type))
async def on_reception(callback, state: FSMContext, bot):
    value = callback.payload.split(":", 1)[1]
    await state.update_data(reception_type=value)

    data = await state.get_data()
    if data.get("colors"):
        # окрасы уже определены по API — сразу к тех.состоянию
        await state.set_state(AppraisalStates.condition)
        await callback.answer(text="Техническое состояние?", keyboard=condition_kb())
    else:
        await state.set_state(AppraisalStates.colors)
        await callback.answer(text=f"Тип приёма: {value}\n\nСколько окрасов?", keyboard=colors_kb())


@router.message_callback(F.payload.startswith("colors:"), StateFilter(AppraisalStates.colors))
async def on_colors(callback, state: FSMContext, bot):
    value = callback.payload.split(":", 1)[1]
    await state.update_data(colors=value)
    await state.set_state(AppraisalStates.condition)
    await callback.answer(text=f"Окрасы: {value}\n\nТехническое состояние?", keyboard=condition_kb())


@router.message_callback(F.payload.startswith("condition:"), StateFilter(AppraisalStates.condition))
async def on_condition(callback, state: FSMContext, bot):
    value = callback.payload.split(":", 1)[1]
    await state.update_data(condition=value)
    await _proceed_after_condition(state, callback.answer, bot)


@router.message_callback(F.payload.startswith("extra:"), StateFilter(AppraisalStates.extra))
async def on_extra(callback, state: FSMContext, bot):
    value = callback.payload.split(":", 1)[1]
    await state.update_data(extra=value)
    await _finish_scoring(state, callback.answer, bot)


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
    await _handle_planned_sale_price(message.body.text, state, message.answer, bot)


@router.message_callback(F.payload == "skip_planned", StateFilter(AppraisalStates.waiting_planned_sale_price))
async def on_planned_sale_price_skip(callback, state: FSMContext, bot):
    await _handle_planned_sale_price("пропустить", state, callback.answer, bot)


@router.message(StateFilter(AppraisalStates.waiting_negotiated))
async def on_waiting_negotiated(message, state: FSMContext, bot):
    await _handle_negotiated(message.body.text, state, message.answer, bot)


@router.message_callback(F.payload == "skip_negotiated", StateFilter(AppraisalStates.waiting_negotiated))
async def on_waiting_negotiated_skip(callback, state: FSMContext, bot):
    await _handle_negotiated("пропустить", state, callback.answer, bot)


@router.message()
async def fallback(message, bot):
    if not _is_private_chat(message):
        return
    await message.answer(text="Пожалуйста, воспользуйтесь кнопками выше, либо введите /new для новой оценки.")


dp.include_router(router)


async def _set_commands_menu():
    """Заполняет нативное меню команд MAX (кнопка '/'), чтобы не нужно было
    запоминать команды — они появятся со своими описаниями в списке."""
    try:
        await bot.set_my_commands([
            {"name": "new", "description": "Начать новую оценку"},
            {"name": "myid", "description": "Узнать свой ID в MAX"},
            {"name": "admin", "description": "Меню администратора (заявки/пользователи/салоны)"},
            {"name": "pending", "description": "[админ] Заявки на доступ"},
            {"name": "users", "description": "[админ] Список пользователей с доступом"},
            {"name": "salons", "description": "[админ] Список привязанных салонов"},
            {"name": "register_salon", "description": "[админ] Привязать этот чат к салону"},
            {"name": "register_pats", "description": "[админ] Назначить этот чат общим чатом ПАЦ"},
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось задать меню команд: %s", e)


async def _main():
    await _set_commands_menu()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_main())
