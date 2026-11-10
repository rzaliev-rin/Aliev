# -*- coding: utf-8 -*-
"""
Мини-приложение MAX (кнопка «Открыть» в чате с ботом).

Веб-сервер работает внутри процесса бота (aiohttp, 127.0.0.1:WEBAPP_PORT), снаружи
его открывает Caddy по HTTPS (deploy/set_domain.sh). Страница — webapp/index.html,
данные — POST /api/<метод> с JSON. Кто открыл приложение, проверяем по подписи
initData (HMAC-SHA256 с токеном бота, см. dev.max.ru/docs/webapps/validation):
подделать пользователя без токена бота нельзя.

Вся бизнес-логика — та же, что в чате (функции модуля bot передаются в start()):
расчёт, отправка на согласование, решения РОП/ДДЦ/УК/ПАЦ, история, админ-панель.
"""
import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
import time
from types import SimpleNamespace
from typing import Optional
from urllib.parse import parse_qsl

from aiohttp import web

logger = logging.getLogger("webapp")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
INIT_DATA_MAX_AGE = 24 * 3600
DRAFT_TTL = 6 * 3600

B = None      # модуль bot (передаётся в start, чтобы не импортировать его второй раз)
BOT = None    # объект Bot
_drafts: dict = {}   # user_id -> {"data": {...}, "ts": time}


class ApiError(Exception):
    def __init__(self, text: str, status: int = 400):
        super().__init__(text)
        self.text, self.status = text, status


# ---------------------------------------------------------------- авторизация
def validate_init_data(init_data: str, bot_token: str, now: Optional[float] = None) -> Optional[dict]:
    """Проверка initData мини-приложения. Возвращает пользователя {"id", "first_name", ...} или None."""
    if not init_data or not bot_token:
        return None
    pairs = parse_qsl(init_data, keep_blank_values=True)
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):  # каждый параметр — ровно один раз
        return None
    params = dict(pairs)
    received = params.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received.lower()):
        return None
    try:
        auth_date = int(params.get("auth_date") or 0)
        if auth_date > 10 ** 11:  # на случай миллисекунд
            auth_date //= 1000
        if (now or time.time()) - auth_date > INIT_DATA_MAX_AGE:
            return None
        user = json.loads(params.get("user") or "{}")
    except (ValueError, TypeError):
        return None
    if not user.get("id"):
        return None
    return user


def _user_ns(user: dict) -> SimpleNamespace:
    return SimpleNamespace(user_id=int(user["id"]), id=int(user["id"]),
                           first_name=user.get("first_name") or "", last_name=user.get("last_name") or "",
                           name=user.get("username") or "")


class WebCallback:
    """Нажатие кнопки из мини-приложения в виде, который понимают обработчики бота."""

    def __init__(self, payload: str, user: SimpleNamespace):
        self.payload, self.user, self.messages = payload, user, []

    async def answer(self, text=None, notification=None, keyboard=None, **_kw):
        if text or notification:
            self.messages.append(text or notification)


# ---------------------------------------------------------------- помощники
def _fmt(v) -> str:
    return B._fmt(v) if v is not None else "—"


def _draft(user_id: int) -> dict:
    d = _drafts.get(user_id)
    if not d or time.time() - d["ts"] > DRAFT_TTL:
        raise ApiError("Оценка не найдена — начните заново: вставьте ссылку или VIN.")
    d["ts"] = time.time()
    return d["data"]


def _options() -> dict:
    return {
        "reception_type": list(B.RECEPTION_TYPES),
        "salon": [{"value": s, "label": B.salon_label(s)} for s in B.get_salon_options()],
        "colors": B.get_colors_options(),
        "condition": B.get_condition_options(),
        "extra": B.get_extra_options(),
    }


def _draft_view(data: dict) -> dict:
    car = " ".join(str(x) for x in [data.get("brand"), data.get("model"), data.get("year")] if x)
    return {
        "car": car or "—", "vin": data.get("vin"), "appraiser": data.get("appraiser_name"),
        "dealer_name": data.get("dealer_name"), "salon": data.get("salon"),
        "salon_label": B.salon_label(data["salon"]) if data.get("salon") else None,
        "avito_price": data.get("avito_price"), "accidents": data.get("accidents_count"),
        "repainted": data.get("repainted_parts_count"), "autohub_ptsp": data.get("autohub_ptsp"),
        "maxposter_url": B._maxposter_link(data.get("appraisal_id")), "autoteka_url": data.get("autoteka_url"),
        "fields": {k: data.get(k) for k in ("reception_type", "salon", "year", "mileage", "colors", "condition",
                                             "extra", "presale_preparation_cost")},
        "planned": data.get("manager_resale_forecast", data.get("appraisal_sale_cost")),
        "negotiated": data.get("negotiated_price", data.get("appraisal_purchase_cost")),
        "warnings": B._appraisal_warnings(data),
        "options": _options(),
    }


def _missing(data: dict) -> list:
    need = []
    if not data.get("reception_type"):
        need.append("тип приёма")
    if not data.get("salon") or data["salon"] not in B.get_salon_options():
        need.append("автосалон")
    if not data.get("year"):
        need.append("год выпуска")
    if data.get("mileage") is None:
        need.append("пробег")
    for key, label in (("colors", "окрасы"), ("condition", "тех. состояние"), ("extra", "доп. данные")):
        if not data.get(key):
            need.append(label)
    return need


def _num(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return B._parse_amount(str(v))
    except Exception:  # noqa: BLE001
        raise ApiError(f"Не понял число: {v}")


def _apply_fields(data: dict, fields: dict) -> None:
    for key in ("reception_type", "salon", "colors", "condition", "extra"):
        if key in fields and fields[key]:
            data[key] = fields[key]
    if fields.get("extra"):
        data["_extra_manual"] = True
    for key in ("year", "mileage"):
        if fields.get(key) not in (None, ""):
            data[key] = int(_num(fields[key]))
    for key, target in (("planned", "manager_resale_forecast"), ("negotiated", "negotiated_price"),
                        ("presale_preparation_cost", "presale_preparation_cost")):
        if key in fields:
            data[target] = _num(fields[key])
    # «Более 3х ДТП» — автоматически, как в чате
    if (not data.get("extra") and (data.get("accidents_count") or 0) > B.ACCIDENTS_AUTO_THRESHOLD
            and B.ACCIDENTS_AUTO_OPTION in B.get_extra_options()):
        data["extra"] = B.ACCIDENTS_AUTO_OPTION


def _calc_view(data: dict) -> dict:
    appraisal_input = B._appraisal_input(data)
    result = B.calc_appraisal(appraisal_input)
    metrics = B._metrics(result)
    try:  # журнал «Мои оценки»: одна запись на оценку
        fields = B._journal_fields(data, result, appraisal_input)
        if data.get("_record_id"):
            B.appraisals_log.update(data["_record_id"], **fields)
        else:
            data["_record_id"] = B.appraisals_log.add(fields)
    except Exception as e:  # noqa: BLE001
        logger.error("журнал: %s", e)
    rec = B._recommendation(data, result, appraisal_input)
    price = appraisal_input.negotiated_price
    return {
        "status": result.approval_status,
        "status_text": B._status_text(result.approval_status),
        "status_line": B._card_status_line(result.approval_status, price, metrics) if price is not None else None,
        "category": result.category,
        "limits": {"rop": result.purchase_price_rop, "ddc": result.purchase_price_ddc, "ceiling": result.ceiling_price},
        "ptsp": result.planned_sale_price_rop, "gross_margin_plan": result.gross_margin_rop,
        "sale": result.manager_resale_forecast, "margin": result.margin_negotiated if price is not None else None,
        "refurbishment": result.refurbishment,
        "ladder": B._price_ladder(result),
        "recommendation": B._recommendation_text(data, result, appraisal_input) if rec else "",
        "card_preview": B._approval_card_text(
            B._vehicle_info(data), appraisal_input, result, data.get("_manager_name") or "", data.get("vin"),
            reception_type=appraisal_input.reception_type, appraiser_sale_cost=result.manager_resale_forecast,
            presale_preparation_cost=data.get("presale_preparation_cost"), metrics=metrics,
            warnings=B._card_warnings(data)
            + ([B.recommend.format_recommendation_short(rec, B._fmt)] if rec else []),
            autohub_ptsp=data.get("autohub_ptsp")) if price is not None else None,
    }


# ---------------------------------------------------------------- согласования
def _deal_actions(user_id: int, deal: dict) -> list:
    stage, dealer = deal.get("stage"), deal.get("dealer_name")
    if stage == "salon_pending":
        approvers = B.approval_hierarchy.get_first_stage_approvers(dealer, deal.get("approval_status"))
        return ["approve", "adjust", "decline"] if B._can_approve(user_id, approvers) else []
    if stage == "uk_pending":
        return ["approve", "adjust", "decline"] if B._can_approve(
            user_id, B.approval_hierarchy.get_uk_approvers(dealer)) else []
    if stage == "pats_pending":
        return ["pats_done", "pats_req_diag", "pats_req_cat"] if B._can_approve(
            user_id, B.approval_hierarchy.get_pats_approvers()) else []
    return []


def _deal_view(token: str, deal: dict, actions: list) -> dict:
    vehicle = deal.get("vehicle") or {}
    return {
        "token": token, "stage": deal.get("stage"), "stage_text": B.STAGE_TEXT.get(deal.get("stage"), deal.get("stage")),
        "status_text": B._status_text(deal.get("approval_status")),
        "salon": B._deal_salon_label(deal),
        "car": " ".join(str(x) for x in [vehicle.get("brand"), vehicle.get("model"), vehicle.get("year")] if x) or "—",
        "price": deal.get("negotiated_price"), "created": deal.get("created_at") or deal.get("created"),
        "card": B._pats_card_text(deal, deal.get("final_approved_by_role")) if deal.get("stage") == "pats_pending"
        else B._deal_card_text(deal),
        "actions": actions,
    }


# ---------------------------------------------------------------- API
async def api_me(user, body):
    uid = user.user_id
    allowed = B._allowed(uid)
    approvals = 0
    if allowed:
        approvals = sum(1 for _, d in B.deals_store.list_deals().items() if _deal_actions(uid, d))
    return {"user_id": uid, "name": B._full_name(user) or user.first_name, "allowed": allowed,
            "bot_enabled": B.access_store.is_bot_enabled(), "admin": allowed and B._is_admin(uid),
            "history": allowed and B._history_scope(uid)[0], "approvals": approvals,
            "email_login": B.email_auth.enabled(), "email_domains": B.email_auth.domains_text()}


async def api_login(user, body):
    """Вход по корпоративной почте прямо из приложения: {"email"} -> код, {"code"} -> доступ."""
    uid = user.user_id
    if B._allowed(uid):
        return {"ok": True, "allowed": True}
    if not B.email_auth.enabled():
        is_new = await B._register_access_request(BOT, uid, B._full_name(user))
        return {"ok": True, "text": "Заявка отправлена администратору." if is_new else "Заявка уже отправлена, ожидайте."}
    if body.get("email"):
        ok, text = await asyncio.to_thread(B.email_auth.send_code, uid, str(body["email"]))
        return {"ok": ok, "text": text}
    if body.get("code"):
        email, text = B.email_auth.check_code(uid, str(body["code"]))
        if not email:
            return {"ok": False, "text": text}
        name = B._full_name(user)
        B.access_store.approve(uid, name, email=email)
        for admin_id in {B.OWNER_USER_ID, *B.ADMIN_USER_IDS} - {None}:
            await B._send_to_user(BOT, admin_id, f"🔑 Вошёл по корпоративной почте (приложение): {name or 'без имени'}, "
                                                 f"{email}, ID {uid}.")
        return {"ok": True, "allowed": True}
    raise ApiError("Введите почту или код.")


async def api_lookup(user, body):
    q = (body.get("q") or "").strip()
    if not q:
        raise ApiError("Вставьте ссылку на оценку из MaxPoster, VIN или номер сделки.")
    try:
        found = await asyncio.to_thread(B.fetch_appraisal, q)
    except Exception as e:  # noqa: BLE001
        raise ApiError(f"Не получилось найти оценку ({e}).")
    data = B._maxposter_state(found, user.first_name or str(user.user_id), user.user_id)
    _apply_fields(data, {})
    _drafts[user.user_id] = {"data": data, "ts": time.time()}
    return {"draft": _draft_view(data), "missing": _missing(data)}


async def api_calc(user, body):
    data = _draft(user.user_id)
    _apply_fields(data, body.get("fields") or {})
    missing = _missing(data)
    view = {"draft": _draft_view(data), "missing": missing}
    if not missing:
        view["result"] = _calc_view(data)
    return view


async def api_submit(user, body):
    if not B._allowed(user.user_id):
        raise ApiError("Нет доступа к боту.", 403)
    data = _draft(user.user_id)
    _apply_fields(data, body.get("fields") or {})
    if _missing(data):
        raise ApiError("Заполните: " + ", ".join(_missing(data)))
    if data.get("negotiated_price") is None:
        raise ApiError("Укажите «Запрос бизнеса» — цену выкупа, согласованную с клиентом.")
    open_deals, _ = B._vin_duplicates(data.get("vin"), data.get("_record_id"))
    if open_deals and not body.get("dup_ok"):
        return {"duplicate": True, "text": "\n".join(B._appraisal_warnings(data))
                + "\n\nОтправить ещё одну заявку по этому VIN?"}
    _drafts.pop(user.user_id, None)  # повторное нажатие не создаст вторую карточку
    note = await B._submit_appraisal(BOT, data)
    return {"ok": True, "text": "✅ Отправлено на согласование." + note}


async def api_approvals(user, body):
    uid = user.user_id
    items = []
    for token, deal in B.deals_store.list_deals().items():
        actions = _deal_actions(uid, deal)
        if actions:
            items.append(_deal_view(token, deal, actions))
    items.sort(key=lambda x: str(x.get("created") or ""))
    return {"items": items}


async def api_decide(user, body):
    token, action = body.get("token") or "", body.get("action") or ""
    deal = B.deals_store.get_deal(token)
    if not deal:
        raise ApiError("Заявка уже обработана или закрыта.")
    if action not in _deal_actions(user.user_id, deal):
        raise ApiError("У вас нет прав на это действие или заявка уже на другом этапе.", 403)
    uk = deal.get("stage") == "uk_pending"
    if action == "adjust":
        price, forecast = _num(body.get("price")), _num(body.get("forecast"))
        if not price or not forecast:
            raise ApiError("Укажите новую цену выкупа и ПЦП.")
        B.deals_store.update_deal(token, negotiated_price=price, manager_resale_forecast=forecast)
        deal = B.deals_store.get_deal(token)
        role = B.approval_hierarchy.resolve_role(deal.get("dealer_name"), user.user_id)
        text = await B._return_for_correction(BOT, token, deal, role)
        return {"ok": True, "text": text}
    handler = {
        ("approve", False): B.on_deal_approve, ("decline", False): B.on_deal_decline,
        ("approve", True): B.on_uk_approve, ("decline", True): B.on_uk_decline,
    }.get((action, uk))
    payload_prefix = {"pats_done": "pats_done", "pats_req_diag": "pats_req_diag", "pats_req_cat": "pats_req_cat"}
    if action in payload_prefix:
        handler = {"pats_done": B.on_pats_done, "pats_req_diag": B.on_pats_req_diag,
                   "pats_req_cat": B.on_pats_req_cat}[action]
        payload = f"{payload_prefix[action]}:{token}"
    else:
        payload = f"{'uk' if uk else 'deal'}_{action}:{token}"
    cb = WebCallback(payload, user)
    await handler(cb, BOT)
    return {"ok": True, "text": "\n\n".join(m for m in cb.messages if m) or "Готово."}


async def api_my(user, body):
    items = []
    for rec in B.appraisals_log.for_user(user.user_id, limit=30):
        items.append({
            "ts": rec.get("ts"), "car": rec.get("vehicle") or "—", "vin": rec.get("vin"),
            "salon": B.salon_label(rec["salon"]) if rec.get("salon") else rec.get("dealer_name"),
            "price": rec.get("negotiated_price"), "status_text": B._status_text(rec.get("status")),
            "outcome": B.appraisals_log.OUTCOME_TEXT.get(rec.get("outcome"), rec.get("outcome") or "посчитана"),
            "last_event": rec.get("last_event"), "maxposter_url": B._maxposter_link(rec.get("appraisal_id")),
        })
    return {"items": items}


async def api_history(user, body):
    ok, scope = B._history_scope(user.user_id)
    if not ok:
        raise ApiError("История доступна администраторам, РОП, ДДЦ и УК.", 403)
    salons = [{"value": "all", "label": "Все салоны"}] if len(B._history_salon_options(scope)) != 1 else []
    salons += [{"value": str(i), "label": B.salon_label(s)} for i, s in B._history_salon_options(scope)]
    if body.get("only_options"):
        return {"salons": salons}
    salon = B._history_salon_by_arg(str(body.get("salon") or "all"))
    if salon and scope is not None and B.dealer_chats.normalize_salon_name(salon) not in scope:
        raise ApiError("Этот салон вам недоступен.", 403)
    try:
        if body.get("period") == "custom":
            start = datetime.date.fromisoformat(body["from"])
            end = datetime.date.fromisoformat(body["to"])
        else:
            start, end = B._history_period(body.get("period") or "today")
    except (KeyError, ValueError):
        raise ApiError("Не понял период.")
    if (end - start).days > 92:
        raise ApiError("Период не больше 3 месяцев.")
    rows, note = await B._history_rows(salon, scope, start, end)
    summary = B._history_messages(rows, note, salon, scope, start, end)[0].split("\n\n")[0]
    items = []
    for r in reversed(rows):
        items.append({
            "ts": r.get("ts"), "car": r.get("vehicle") or "—", "vin": r.get("vin"), "mileage": r.get("mileage"),
            "salon": B.salon_label(r["salon"]) if r.get("salon") else r.get("dealer_name"),
            "avito": r.get("avito_price"), "price": r.get("negotiated_price"), "category": r.get("category"),
            "reception_type": r.get("reception_type"), "appraiser": r.get("appraiser"),
            "sent": r.get("outcome") not in (None, "not_sent"), "status_text": B._status_text(r.get("status")),
            "outcome": B.appraisals_log.OUTCOME_TEXT.get(r.get("outcome"), r.get("outcome")) if r.get("outcome") else None,
            "mp_status": r.get("mp_status"), "maxposter_url": B._maxposter_link(r.get("appraisal_id")),
        })
    return {"summary": summary, "note": note, "items": items, "salons": salons}


def _require_admin(user):
    if not B._is_admin(user.user_id):
        raise ApiError("Только для администраторов.", 403)


async def api_admin(user, body):
    _require_admin(user)
    allowed = B.access_store.list_allowed()
    pending = B.access_store.list_pending()
    return {
        "bot_enabled": B.access_store.is_bot_enabled(),
        "users": [{"id": k, "name": v.get("name") or "", "email": v.get("email") or "",
                   "approved_at": v.get("approved_at") or ""} for k, v in allowed.items()],
        "pending": [{"id": k, "name": v.get("name") or "", "requested_at": v.get("requested_at") or ""}
                    for k, v in pending.items()],
        "salons": [{"name": B.salon_label(n) if B.find_salon(n) else n, "chat_id": c}
                   for n, c in B.dealer_chats.list_salons().items()],
        "pats_chat": B.dealer_chats.get_pats_chat_id(),
        "queue": len(B.deals_store.list_deals()),
    }


async def api_admin_action(user, body):
    _require_admin(user)
    action, target = body.get("action"), body.get("id")
    if action == "approve_user":
        B.access_store.approve(int(target))
        await B._send_to_user(BOT, int(target), B.ACCESS_GRANTED_TEXT, keyboard=B.main_menu_kb(int(target)))
        return {"ok": True, "text": "Доступ открыт."}
    if action == "deny_user":
        B.access_store.deny(int(target))
        return {"ok": True, "text": "Заявка отклонена."}
    if action == "revoke_user":
        B.access_store.revoke(int(target))
        return {"ok": True, "text": "Доступ закрыт."}
    if action in ("bot_stop", "bot_start"):
        text = await B._set_bot_running(BOT, action == "bot_start")
        return {"ok": True, "text": text or ("Бот запущен." if action == "bot_start" else "Бот остановлен.")}
    if action == "report":
        days = int(body.get("days") or 7)
        return {"ok": True, "text": B._build_report(B.appraisals_log.since(days), days)}
    raise ApiError("Неизвестное действие.")


API = {"me": api_me, "login": api_login, "lookup": api_lookup, "calc": api_calc, "submit": api_submit,
       "approvals": api_approvals, "decide": api_decide, "my": api_my, "history": api_history,
       "admin": api_admin, "admin_action": api_admin_action}
OPEN_METHODS = {"me", "login"}  # доступны и без доступа к боту


async def handle_api(request: web.Request) -> web.Response:
    method = request.match_info["method"]
    fn = API.get(method)
    if fn is None:
        return web.json_response({"error": "Не найдено"}, status=404)
    raw_user = validate_init_data(request.headers.get("X-Init-Data", ""), B.MAX_BOT_TOKEN)
    if raw_user is None:
        return web.json_response({"error": "Откройте приложение из чата с ботом в MAX."}, status=401)
    user = _user_ns(raw_user)
    if method not in OPEN_METHODS and not B._allowed(user.user_id):
        return web.json_response({"error": "Нет доступа к боту. Войдите по корпоративной почте."}, status=403)
    try:
        body = await request.json() if request.can_read_body else {}
    except Exception:  # noqa: BLE001
        body = {}
    try:
        return web.json_response(await fn(user, body or {}), dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))
    except ApiError as e:
        return web.json_response({"error": e.text}, status=e.status)
    except Exception as e:  # noqa: BLE001
        logger.exception("[webapp] %s: %s", method, e)
        return web.json_response({"error": "Ошибка на сервере. Попробуйте ещё раз."}, status=500)


async def handle_index(request: web.Request) -> web.Response:
    resp = web.FileResponse(os.path.join(STATIC_DIR, "index.html"))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def make_app() -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/{method}", handle_api)
    app.router.add_static("/static/", STATIC_DIR)
    return app


async def start(bot_module, bot, host: str = "127.0.0.1", port: int = 8080) -> None:
    global B, BOT
    B, BOT = bot_module, bot
    runner = web.AppRunner(make_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("[webapp] мини-приложение слушает %s:%s", host, port)
