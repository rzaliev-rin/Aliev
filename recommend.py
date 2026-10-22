# -*- coding: utf-8 -*-
"""
Рекомендация по цене (подсказка, на статус согласования НЕ влияет):
  • за сколько выставить авто в продажу (переоценка — по регламенту салона, здесь не даём);
  • ожидаемая цена и срок продажи (цель — продать за TARGET_DAYS дней);
  • до какой цены выкупать и с какой цены начинать торг.

Версия 2 — калибровка по фактическим продажам. Ожидаемая цена продажи =
Авито-оценка × коэффициент «продажа к Авито» и срок продажи берутся из истории:
  1) этой модели в этом салоне (если продано от MIN_MODEL_SALES авто),
  2) иначе этой модели по всей сети,
  3) иначе салона в целом,
с плавным смешиванием (чем больше продаж модели, тем больше её вес). История
пересчитывается при каждой выгрузке «Данные продаж» (calibration.json); до
первой выгрузки — коэффициенты салонов из анализа 24.04–21.10.2026.
Без данных по салону — версия 1: правила по рынку MaxPoster (коридор
minImv–maxImv, ликвидность, конкуренты).
"""
import datetime
import json
import os
from dataclasses import dataclass
from typing import Optional

from pricing_engine import calc_gross_margin

CALIBRATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
MIN_MODEL_SALES = 5      # с какого числа продаж модель считается отдельно
MODEL_PRIOR = 10         # вес истории салона при смешивании с моделью (в «продажах»)
MAX_SALE_TO_IMV = 1.3    # выше — транзит/подбор под клиента (BMW 5 в Краснодаре: 145%), в калибровку не берём
MIN_SALE_TO_IMV = 0.4

# Коэффициенты салонов по анализу 24.04–21.10.2026 (1 687 продаж) — до первой выгрузки.
# Ключ — название салона без номера и «!», в нижнем регистре (как dealer_chats.normalize_salon_name).
SALON_DEFAULTS = {
    "асп тюмень toyota действующий": {"n": 85, "sale_ratio": 1.058, "days": 31},
    "асп toyota - екатеринбург": {"n": 78, "sale_ratio": 1.004, "days": 32},
    "асп волгоград chery": {"n": 176, "sale_ratio": 0.914, "days": 17},
    "асп казань chery": {"n": 224, "sale_ratio": 0.895, "days": 21},
    "асп краснодар changan": {"n": 219, "sale_ratio": 1.005, "days": 29},
    "асп нн chery": {"n": 293, "sale_ratio": 0.908, "days": 24},
    "асп новосибирск chery": {"n": 500, "sale_ratio": 0.943, "days": 22},
    "асп тюмень exeed": {"n": 73, "sale_ratio": 0.968, "days": 32},
}

TARGET_DAYS = 30                 # целевой срок продажи
DEFAULT_SALE_DAYS = 35           # если MaxPoster не дал средний срок продажи
CORRIDOR_DEFAULT = (0.93, 1.05)  # коридор к Авито-оценке, если нет minImv/maxImv
BARGAIN = (0.05, 0.03)           # скидка при продаже (торг): низкая -> высокая ликвидность
# План снижения цены НЕ даём: переоценка идёт по регламенту салона (каждые 5 дней без звонков).
HOLDING_COST_PER_10_DAYS = 0.005  # стоимость стоянки/денег сверх TARGET_DAYS, доля цены продажи
RISK_BUFFER = {"высокая": 0.01, "средняя": 0.02, "низкая": 0.03}
TORG_START = 0.04                # начинать торг на столько ниже максимальной цены выкупа


@dataclass
class Recommendation:
    list_price: float
    expected_sale: float
    expected_days: int
    buyout_max: float
    torg_start: float
    confidence: str
    reasons: list


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _linear(value: Optional[float], bad: float, good: float) -> Optional[float]:
    """0 при значении bad, 1 при good, линейно между (bad может быть больше good)."""
    if value is None:
        return None
    return _clip((value - bad) / (good - bad))


def liquidity_score(m: dict) -> float:
    parts = [(0.5, _linear(m.get("avg_sale_days"), 60, 20)),
             (0.3, _linear(m.get("liquidity_pct"), 10, 50)),
             (0.2, _linear(m.get("competitors"), 30, 5))]
    known = [(w, v) for w, v in parts if v is not None]
    if not known:
        return 0.5
    return sum(w * v for w, v in known) / sum(w for w, _ in known)


def confidence(m: dict) -> str:
    competitors = m.get("competitors") or 0
    sales = m.get("market_sales_90") or 0
    if m.get("avg_sale_days") is None or competitors < 3:
        return "низкая"
    if competitors >= 10 and sales >= 5:
        return "высокая"
    return "средняя"


def _round(x: float, step: int, down: bool = False) -> float:
    return (x // step) * step if down else round(x / step) * step


# ---------------------------------------------------------------------------
# Калибровка по истории продаж
# ---------------------------------------------------------------------------
def _norm(text) -> str:
    from dealer_chats import normalize_salon_name
    return normalize_salon_name(text)


def _model_key(model) -> str:
    return " ".join(str(model or "").lower().replace("(ваз)", "").split())


def _days_between(start, end) -> Optional[int]:
    try:
        d1 = datetime.date.fromisoformat(str(start)[:10])
        d2 = datetime.date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    days = (d2 - d1).days
    return days if 0 <= days <= 365 else None


def build_calibration(rows: list) -> dict:
    """Коэффициенты из выгрузки оценок MaxPoster (строки sales_export): только «Продано»,
    без транзита (продажа выше MAX_SALE_TO_IMV Авито) и явных ошибок данных."""
    groups = {"salons": {}, "models": {}, "net_models": {}}

    def add(bucket: dict, key: str, imv: float, buy: float, sale: float, days):
        g = bucket.setdefault(key, {"n": 0, "imv": 0.0, "buy": 0.0, "sale": 0.0, "days": 0, "nd": 0})
        g["n"] += 1
        g["imv"] += imv
        g["buy"] += buy
        g["sale"] += sale
        if days is not None:
            g["days"] += days
            g["nd"] += 1

    for r in rows:
        try:
            imv, buy, sale = float(r.get("imv") or 0), float(r.get("purchase_cost") or 0), float(r.get("sale_cost") or 0)
        except (TypeError, ValueError):
            continue
        if r.get("status") != "Продано" or imv <= 0 or buy <= 0 or sale <= 0:
            continue
        if not MIN_SALE_TO_IMV <= sale / imv <= MAX_SALE_TO_IMV:
            continue
        salon = _norm(r.get("dealer"))
        model = _model_key(f"{r.get('brand') or ''} {r.get('model') or ''}")
        days = _days_between(r.get("created"), r.get("completion_date"))
        add(groups["salons"], salon, imv, buy, sale, days)
        add(groups["models"], f"{salon}|{model}", imv, buy, sale, days)
        add(groups["net_models"], model, imv, buy, sale, days)

    def finish(g: dict) -> dict:
        return {"n": g["n"], "sale_ratio": round(g["sale"] / g["imv"], 4), "buy_ratio": round(g["buy"] / g["imv"], 4),
                "margin_pct": round((g["sale"] - g["buy"]) / g["sale"], 4),
                "days": round(g["days"] / g["nd"], 1) if g["nd"] else None}
    result = {k: {key: finish(g) for key, g in v.items()} for k, v in groups.items()}
    result["built_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    return result


def save_calibration(calibration: dict) -> None:
    tmp = CALIBRATION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(calibration, f, ensure_ascii=False)
    os.replace(tmp, CALIBRATION_FILE)


_cal_cache = {"mtime": None, "data": None}


def load_calibration() -> dict:
    try:
        mtime = os.path.getmtime(CALIBRATION_FILE)
    except OSError:
        return {"salons": SALON_DEFAULTS, "models": {}, "net_models": {}}
    if _cal_cache["mtime"] != mtime:
        try:
            with open(CALIBRATION_FILE, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("salons", {})
            for key, value in SALON_DEFAULTS.items():  # салон без продаж в выгрузке — по умолчанию
                data["salons"].setdefault(key, value)
            _cal_cache.update(mtime=mtime, data=data)
        except (OSError, ValueError):
            return {"salons": SALON_DEFAULTS, "models": {}, "net_models": {}}
    return _cal_cache["data"]


def _po_prodazham(n: int) -> str:
    """«по 21 продаже», «по 74 продажам»."""
    return f"по {n} продаже" if n % 10 == 1 and n % 100 != 11 else f"по {n} продажам"


def calibrated_stats(salon: Optional[str], model: Optional[str]) -> Optional[dict]:
    """{'sale_ratio', 'days', 'source', 'level'} по истории продаж или None (нет данных по салону)."""
    cal = load_calibration()
    s_key, m_key = _norm(salon), _model_key(model)
    base = cal["salons"].get(s_key)
    if not base or not base.get("sale_ratio"):
        return None
    ratio, days = base["sale_ratio"], base.get("days")
    source, level = f"{_po_prodazham(base['n'])} салона", "salon"
    model_stats, prior = cal["models"].get(f"{s_key}|{m_key}"), MODEL_PRIOR
    where = "в салоне"
    if not model_stats or model_stats["n"] < MIN_MODEL_SALES:
        model_stats, prior, where = cal["net_models"].get(m_key), MODEL_PRIOR * 2, "по сети"
    if model_stats and model_stats["n"] >= MIN_MODEL_SALES and m_key:
        w = model_stats["n"] / (model_stats["n"] + prior)
        ratio = w * model_stats["sale_ratio"] + (1 - w) * ratio
        if model_stats.get("days") is not None and days is not None:
            days = w * model_stats["days"] + (1 - w) * days
        source = (f"{_po_prodazham(model_stats['n'])} {model} {where} и "
                  f"{_po_prodazham(base['n'])[3:]} салона")
        level = "model" if where == "в салоне" else "net_model"
    return {"sale_ratio": ratio, "days": days, "source": source, "level": level}


def recommend(market: dict, avito_price: float, category: str, reception_type: str,
              refurbishment: float = 0.0, ceiling_price: Optional[float] = None,
              salon: Optional[str] = None, model: Optional[str] = None) -> Optional[Recommendation]:
    m = dict(market or {})
    imv = m.get("imv") or avito_price
    if not imv:
        return None
    reasons = []
    lo = m.get("min_imv") or imv * CORRIDOR_DEFAULT[0]
    hi = m.get("max_imv") or imv * CORRIDOR_DEFAULT[1]
    if hi < lo:
        lo, hi = hi, lo
    history = calibrated_stats(salon, model) if salon else None

    liq = liquidity_score(m)
    risk = 0.0
    if (m.get("accidents") or 0) >= 3:
        risk -= 0.02
        reasons.append(f"ДТП: {int(m['accidents'])}")
    if (m.get("owners") or 0) >= 4:
        risk -= 0.01
        reasons.append(f"владельцев: {int(m['owners'])}")
    if m.get("mileage") and m.get("avg_market_mileage") and m["mileage"] > m["avg_market_mileage"] * 1.2:
        risk -= 0.02
        reasons.append("пробег выше среднего по рынку")

    bargain = BARGAIN[0] + (BARGAIN[1] - BARGAIN[0]) * liq
    market_days = m.get("avg_sale_days") or DEFAULT_SALE_DAYS
    if history:
        # по истории салона/модели: за сколько такие авто реально продаём к Авито-оценке
        expected_sale = _round(imv * history["sale_ratio"] * (1 + risk), 5_000)
        list_price = _round(expected_sale / (1 - bargain), 10_000)
        expected_days = int(round(history["days"] if history.get("days") is not None else market_days))
        conf = {"model": "высокая", "net_model": "средняя"}.get(history["level"], "средняя")
        reasons.insert(0, history["source"])
    else:
        # цена выставления: позиция в рыночном коридоре по ликвидности, минус риски
        list_price = _round((lo + (hi - lo) * (0.35 + 0.45 * liq)) * (1 + risk), 10_000)
        expected_sale = _round(list_price * (1 - bargain), 5_000)
        expected_days = int(round(market_days * (1 + max(0.0, list_price / imv - 1) * 4)))
        conf = confidence(m)

    # выкуп: цена продажи − плановая ВП по метрике − переподготовка − стоянка сверх цели − запас на риск
    margin = calc_gross_margin(expected_sale, category, reception_type)
    holding = expected_sale * HOLDING_COST_PER_10_DAYS * max(0, expected_days - TARGET_DAYS) / 10
    buyout = (expected_sale - margin - (refurbishment or 0) - holding) * (1 - RISK_BUFFER[conf])
    if ceiling_price:
        buyout = min(buyout, ceiling_price)
    buyout = max(0.0, _round(buyout, 5_000, down=True))
    torg = max(0.0, _round(buyout * (1 - TORG_START), 10_000, down=True))

    liq_text = []
    if m.get("avg_sale_days"):
        liq_text.append(f"ср. срок продажи {int(m['avg_sale_days'])} дн.")
    if m.get("competitors") is not None:
        liq_text.append(f"конкурентов {int(m['competitors'])}")
    if m.get("market_sales_90") is not None and m.get("market_total_90"):
        liq_text.append(f"продано {int(m['market_sales_90'])} из {int(m['market_total_90'])} за 90 дн.")
    reasons = ([reasons[0]] + liq_text + reasons[1:]) if history else liq_text + reasons
    return Recommendation(list_price, expected_sale, expected_days, buyout, torg, conf, reasons)


def format_recommendation(r: Optional[Recommendation], fmt, negotiated_price: Optional[float] = None,
                          limit_rop: Optional[float] = None) -> str:
    if r is None:
        return ""
    lines = [
        "💡 РЕКОМЕНДАЦИЯ ПО ЦЕНЕ (подсказка, на согласование не влияет)",
        f"Выставить: {fmt(r.list_price)} ₽",
        f"Ожидаемая продажа: ~{fmt(r.expected_sale)} ₽ за ~{r.expected_days} дн."
        + (f" ⚠️ дольше цели {TARGET_DAYS} дн." if r.expected_days > TARGET_DAYS else ""),
        f"Выкуп: до {fmt(r.buyout_max)} ₽, торг начинать с {fmt(r.torg_start)} ₽",
        f"Уверенность: {r.confidence}" + (f" ({'; '.join(r.reasons)})" if r.reasons else ""),
    ]
    if negotiated_price:
        diff = negotiated_price - r.buyout_max
        lines.append(f"Запрошенная цена {fmt(negotiated_price)} ₽ — "
                     + (f"выше рекомендации на {fmt(diff)} ₽" if diff > 0 else "в пределах рекомендации"))
    elif limit_rop:
        lines.append(f"Лимит РОП по метрике: {fmt(limit_rop)} ₽")
    return "\n".join(lines)
