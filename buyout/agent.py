# -*- coding: utf-8 -*-
"""
ИИ-агент отдела выкупа и комиссии: пишет первое сообщение продавцу и ведёт диалог
живым, человеческим языком, пока клиент не будет готов к выкупу или к продаже
на комиссии (или не откажется).

Каждый ход агент возвращает структурированный ответ:
  reply   — текст, который уходит продавцу (может быть пустым — молчим)
  status  — active / ready / declined / not_relevant / need_human
  lead    — что удалось узнать: выкуп или комиссия, имя, телефон, удобное время, состояние, ожидания по цене

Модель (BUYOUT_AI_PROVIDER в .env):
  yandex    — YandexGPT / Alice AI в Yandex AI Studio (YANDEX_API_KEY, YANDEX_FOLDER_ID, YANDEX_MODEL)
  anthropic — Claude (ANTHROPIC_API_KEY)
Не указан — берётся тот, для которого задан ключ (сначала Яндекс).
"""
import json
import logging
import os
from typing import Optional

from buyout import dialogs
from buyout.listings import Listing

logger = logging.getLogger(__name__)

MODEL = os.environ.get("BUYOUT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("BUYOUT_EFFORT", "medium")  # low / medium / high
YANDEX_BASE_URL = "https://ai.api.cloud.yandex.net/v1"

_client = None         # Anthropic
_yandex_client = None  # Yandex AI Studio (OpenAI-совместимый API)


def provider() -> str:
    """yandex / anthropic / "" (ничего не настроено)."""
    chosen = os.environ.get("BUYOUT_AI_PROVIDER", "").strip().lower()
    if chosen in ("yandex", "anthropic"):
        return chosen
    if os.environ.get("YANDEX_API_KEY"):
        return "yandex"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return ""


def configured() -> bool:
    p = provider()
    if p == "yandex":
        return bool(os.environ.get("YANDEX_API_KEY") and os.environ.get("YANDEX_FOLDER_ID"))
    return p == "anthropic" and bool(os.environ.get("ANTHROPIC_API_KEY"))


def describe_provider() -> str:
    p = provider()
    if p == "yandex":
        return f"Yandex AI Studio, модель {os.environ.get('YANDEX_MODEL', 'yandexgpt-5.1')}"
    if p == "anthropic":
        return f"Anthropic, модель {MODEL}"
    return "не настроена"


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()  # ANTHROPIC_API_KEY из окружения
    return _client


def _get_yandex_client():
    global _yandex_client
    if _yandex_client is None:
        from openai import OpenAI
        _yandex_client = OpenAI(api_key=os.environ["YANDEX_API_KEY"], base_url=YANDEX_BASE_URL,
                                project=os.environ["YANDEX_FOLDER_ID"])  # каталог Yandex Cloud
    return _yandex_client


# Общие правила — одинаковые для всех салонов (кэшируются на стороне API).
RULES = """Ты ведёшь переписку от имени отдела выкупа автомобилей автосалона с частными продавцами, \
которые разместили объявление о продаже машины на Авито, Авто.ру или Дроме.

Салон может предложить продавцу два варианта (какие разрешены в этом диалоге — указано в задаче, \
в строке «Что предлагаем»):
- Выкуп: салон сам покупает машину. Быстро, деньги сразу, без показов и торга с покупателями. \
Цена — ниже рыночной, её называет менеджер после осмотра.
- Комиссия: салон продаёт машину клиента от своего имени на своей площадке, клиент остаётся \
собственником до продажи и получает деньги после неё. Обычно выходит дороже, чем выкуп, \
но нужно время. Условия комиссии — только те, что указаны в данных салона.
Когда предлагаешь оба: начни с того, что ближе продавцу. Торопится, нужны деньги, надоели звонки \
и показы — выкуп. Хочет свою цену и не спешит, или ожидания по цене высокие — комиссия. \
Не вываливай оба варианта в первом же сообщении без повода; второй вариант предложи, \
когда первый не подходит (например, «за такую цену выкупить не сможем, а вот продать на комиссии — реально»). \
Если разрешена только комиссия — выкуп не предлагай, и наоборот.

Как писать:
- Как живой менеджер в мессенджере: коротко (1–3 предложения), просто, вежливо, на «вы». \
Без списков, без канцелярита, без рекламных штампов и восклицательных знаков подряд. Эмодзи — максимум изредка.
- Подстраивайся под тон собеседника: отвечает коротко — и ты коротко.
- Один вопрос за раз. Не устраивай допрос и не вываливай все преимущества сразу.
- Не повторяй дословно то, что уже писал. Не начинай каждое сообщение с приветствия.
- Пиши без ошибок, но не идеально-официально: «добрый день», «подскажите», «понял», «хорошо» — нормально.

Цель: понять, готов ли продавец рассмотреть выкуп или комиссию, и договориться о звонке или осмотре. \
По ходу разговора ненавязчиво уточни то, чего нет в объявлении: состояние, ДТП/окрасы, \
сколько владельцев, ПТС (оригинал/электронный), нет ли кредита или залога, за сколько готов отдать, \
когда удобно показать машину. Всё узнавать не обязательно — главное контакт и готовность.

Когда клиент готов: попроси номер телефона и удобное время для звонка менеджера \
(или договорись о времени осмотра). Как только есть телефон и согласие на звонок/осмотр — \
поблагодари, скажи, что менеджер свяжется, и поставь status=ready.

Цена: конкретную сумму выкупа и цену продажи на комиссии НЕ называй и не обещай — итоговую цену менеджер назовёт после осмотра \
(салон проверяет машину и делает предложение). Если продавец настаивает, можно сказать, что \
ориентируетесь на рынок и готовы предложить честную цену после осмотра, и спросить его ожидания.

Честность:
- Не выдумывай факты о салоне, ценах, акциях, адресах — используй только то, что указано в данных салона.
- Если спрашивают, бот ли ты / живой ли человек — не ври: скажи, что ты помощник отдела выкупа салона, \
и предложи, чтобы перезвонил менеджер. Сам эту тему не поднимай.
- Не давай юридических и финансовых гарантий.

Когда остановиться:
- Продавец отказался, попросил не писать, грубит — вежливо попрощайся одной фразой (или промолчи, \
если просил не писать) и поставь status=declined. Никогда не уговаривай после явного отказа.
- Машина уже продана, это перекупщик/салон, обмен вместо продажи, машина не та — status=not_relevant.
- Вопрос, на который ты не можешь ответить без менеджера (конкретная цена, условия комиссии, которых нет \
в данных салона, trade-in на конкретную машину салона, юридические детали), и клиент настаивает — status=need_human, ответь, что уточнишь \
у менеджера и он свяжется.
- Иначе status=active.

reply — ровно тот текст, который уйдёт продавцу. Пустой reply — если отвечать не нужно \
(например, клиент просил больше не писать).
lead — что известно о клиенте и машине на этот момент (пустые строки, если неизвестно). \
deal_type — что выбрал клиент: buyout (выкуп), commission (комиссия) или unknown (пока не ясно). \
phone — только цифры с кодом, например 79991234567. summary — 1–2 предложения для менеджера: \
что за клиент, на чём договорились."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "status": {"type": "string", "enum": [dialogs.ACTIVE, dialogs.READY, dialogs.DECLINED,
                                               dialogs.NOT_RELEVANT, dialogs.NEED_HUMAN]},
        "lead": {
            "type": "object",
            "properties": {
                "deal_type": {"type": "string", "enum": ["buyout", "commission", "unknown"]},
                "client_name": {"type": "string"},
                "phone": {"type": "string"},
                "best_time": {"type": "string"},
                "price_expectation": {"type": "string"},
                "condition": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["deal_type", "client_name", "phone", "best_time", "price_expectation", "condition", "summary"],
            "additionalProperties": False,
        },
    },
    "required": ["reply", "status", "lead"],
    "additionalProperties": False,
}


def salon_block(profile: dict) -> str:
    pitch = profile["pitch"]
    lines = [
        "Данные салона:",
        f"- Салон: {pitch.get('salon_title') or profile['salon']}",
        f"- Тебя зовут: {pitch.get('manager_name') or 'менеджер отдела выкупа'}",
    ]
    if pitch.get("address"):
        lines.append(f"- Адрес: {pitch['address']}")
    offer = profile.get("offer", "buyout")
    if offer != "commission" and pitch.get("advantages"):
        lines.append("- Плюсы выкупа: " + "; ".join(pitch["advantages"]))
    if offer != "buyout":
        if pitch.get("commission_advantages"):
            lines.append("- Плюсы комиссии: " + "; ".join(pitch["commission_advantages"]))
        lines.append("- Условия комиссии: " + (pitch.get("commission_terms")
                                               or "не заданы — размер и срок не называй, уточнит менеджер"))
    if pitch.get("extra"):
        lines.append(f"- Дополнительно: {pitch['extra']}")
    return "\n".join(lines)


OFFER_LINES = {
    "buyout": "Что предлагаем: только выкуп.",
    "commission": "Что предлагаем: только продажу на комиссии (выкуп по этой машине не предлагаем).",
    "both": "Что предлагаем: выкуп или комиссию — что больше подойдёт продавцу.",
}


def _listing_block(listing: Listing, offer: str = "buyout") -> str:
    lines = [OFFER_LINES.get(offer, OFFER_LINES["buyout"]), f"Объявление ({listing.platform}): {listing.summary()}"]
    if listing.owners:
        lines.append(f"Владельцев: {listing.owners}")
    if listing.seller_name:
        lines.append(f"Продавец: {listing.seller_name}")
    if listing.description:
        lines.append("Текст объявления: " + listing.description[:1500])
    return "\n".join(lines)


def _transcript(messages: list) -> str:
    if not messages:
        return "(переписки ещё нет)"
    who = {"us": "Мы", "client": "Продавец", "manager": "Мы (менеджер)"}
    return "\n".join(f"{who.get(m['from'], m['from'])}: {m['text']}" for m in messages)


# Для YandexGPT — упрощённая схема: в документации нет enum/additionalProperties,
# поэтому допустимые значения описаны словами, а ответ проверяется в _normalize().
YANDEX_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "Текст, который уйдёт продавцу"},
        "status": {"type": "string", "description": "Одно из: active, ready, declined, not_relevant, need_human"},
        "lead": {
            "type": "object",
            "properties": {
                "deal_type": {"type": "string", "description": "Одно из: buyout, commission, unknown"},
                "client_name": {"type": "string"},
                "phone": {"type": "string", "description": "Только цифры с кодом страны, например 79991234567"},
                "best_time": {"type": "string"},
                "price_expectation": {"type": "string"},
                "condition": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["deal_type", "client_name", "phone", "best_time", "price_expectation", "condition", "summary"],
        },
    },
    "required": ["reply", "status", "lead"],
}
_STATUSES = {dialogs.ACTIVE, dialogs.READY, dialogs.DECLINED, dialogs.NOT_RELEVANT, dialogs.NEED_HUMAN}
_LEAD_KEYS = ("deal_type", "client_name", "phone", "best_time", "price_expectation", "condition", "summary")


def _normalize(data) -> dict:
    """Приводим ответ модели к нужному виду: неизвестный статус — продолжаем диалог,
    телефон — только цифры, все поля лида — строки."""
    if not isinstance(data, dict):
        raise ValueError("модель вернула не JSON-объект")
    status = str(data.get("status") or "").strip().lower()
    lead_in = data.get("lead") if isinstance(data.get("lead"), dict) else {}
    lead = {k: str(lead_in.get(k) or "").strip() for k in _LEAD_KEYS}
    if lead["deal_type"] not in ("buyout", "commission"):
        lead["deal_type"] = "unknown"
    digits = "".join(ch for ch in lead["phone"] if ch.isdigit())
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    lead["phone"] = digits
    return {"reply": str(data.get("reply") or "").strip(),
            "status": status if status in _STATUSES else dialogs.ACTIVE,
            "lead": lead}


def _parse_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):  # на всякий случай: ответ в блоке ```json … ```
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1] if start >= 0 and end > start else text)


def _ask_yandex(profile: dict, task: str) -> dict:
    model = os.environ.get("YANDEX_MODEL", "yandexgpt-5.1")
    response = _get_yandex_client().chat.completions.create(
        model=f"gpt://{os.environ['YANDEX_FOLDER_ID']}/{model}",
        messages=[
            {"role": "system", "content": RULES + "\n\n" + salon_block(profile)
             + "\n\nОтвечай строго JSON-объектом с полями reply, status, lead."},
            {"role": "user", "content": task},
        ],
        temperature=float(os.environ.get("YANDEX_TEMPERATURE", "0.5")),
        max_tokens=1500,
        response_format={"type": "json_schema", "json_schema": {"name": "turn", "schema": YANDEX_SCHEMA}},
    )
    return _normalize(_parse_json(response.choices[0].message.content))


def _ask(profile: dict, task: str) -> dict:
    """Один запрос к модели. Всё состояние диалога передаём текстом — это просто и надёжно."""
    if provider() == "yandex":
        return _ask_yandex(profile, task)
    response = _get_client().beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # если основная модель откажется — запрос сам повторится на запасной
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        system=[
            {"type": "text", "text": RULES, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": salon_block(profile)},
        ],
        messages=[{"role": "user", "content": task}],
    )
    if response.stop_reason == "refusal":
        logger.warning("Модель отказалась отвечать: %s", response.stop_details)
        return {"reply": "", "status": dialogs.NEED_HUMAN,
                "lead": {"summary": "Агент не смог ответить — нужен менеджер"}}
    text = next((b.text for b in response.content if b.type == "text"), "")
    return _normalize(json.loads(text))


_OPENING_HOOK = {
    "buyout": "салон интересуется выкупом этой машины",
    "commission": "салон может помочь продать эту машину на комиссии — у себя на площадке, по рыночной цене",
    "both": "салон интересуется этой машиной — может выкупить её или помочь продать",
}


def opening_message(profile: dict, listing: Listing, offer: Optional[str] = None) -> str:
    """Первое сообщение продавцу по объявлению."""
    offer = offer or profile.get("offer", "buyout")
    task = (
        _listing_block(listing, offer)
        + "\n\nНапиши ПЕРВОЕ сообщение этому продавцу. Представься по имени и салону, скажи, что "
        f"увидели объявление и {_OPENING_HOOK.get(offer, _OPENING_HOOK['buyout'])}, и задай один простой "
        "вопрос (например, актуальна ли продажа). Без длинных перечислений преимуществ — максимум одно. "
        "1–3 предложения. status=active."
    )
    return _ask(profile, task)["reply"].strip()


def next_turn(profile: dict, dialog: dict) -> dict:
    """Следующий ход в диалоге после ответа продавца."""
    listing = Listing.from_dict(dialog["listing"])
    known = {k: v for k, v in (dialog.get("lead") or {}).items() if v and v != "unknown"}
    task = (
        _listing_block(listing, dialog.get("offer") or profile.get("offer", "buyout"))
        + "\n\nПереписка (по порядку):\n" + _transcript(dialog["messages"])
        + ("\n\nУже известно о клиенте: " + json.dumps(known, ensure_ascii=False) if known else "")
        + "\n\nНапиши следующий ответ продавцу на его последнее сообщение."
    )
    result = _ask(profile, task)
    result["reply"] = (result.get("reply") or "").strip()
    return result


def followup_message(profile: dict, dialog: dict) -> str:
    """Одно мягкое напоминание, если продавец не ответил на первое сообщение."""
    listing = Listing.from_dict(dialog["listing"])
    task = (
        _listing_block(listing, dialog.get("offer") or profile.get("offer", "buyout"))
        + "\n\nПереписка:\n" + _transcript(dialog["messages"])
        + "\n\nПродавец не ответил. Напиши одно короткое ненавязчивое напоминание (1 предложение), "
        "без давления, не повторяя первое сообщение. status=active."
    )
    return _ask(profile, task)["reply"].strip()
