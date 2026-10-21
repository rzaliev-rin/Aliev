# -*- coding: utf-8 -*-
"""
Рекомендация по цене (подсказка, на статус согласования НЕ влияет):
  • за сколько выставить авто в продажу (переоценка — по регламенту салона, здесь не даём);
  • ожидаемая цена и срок продажи (цель — продать за TARGET_DAYS дней);
  • до какой цены выкупать и с какой цены начинать торг.

Версия 1 — правила на данных оценки MaxPoster (Авито-оценка и её коридор
minImv–maxImv, ликвидность, конкуренты, ДТП, владельцы, пробег к среднему
по рынку). Коэффициенты собраны в начале файла: их будем уточнять по
фактическим продажам (версия 2, калибровка — см. /collect_sales в bot.py).
"""
from dataclasses import dataclass
from typing import Optional

from pricing_engine import calc_gross_margin

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


def recommend(market: dict, avito_price: float, category: str, reception_type: str,
              refurbishment: float = 0.0, ceiling_price: Optional[float] = None) -> Optional[Recommendation]:
    m = dict(market or {})
    imv = m.get("imv") or avito_price
    if not imv:
        return None
    reasons = []
    lo = m.get("min_imv") or imv * CORRIDOR_DEFAULT[0]
    hi = m.get("max_imv") or imv * CORRIDOR_DEFAULT[1]
    if hi < lo:
        lo, hi = hi, lo

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

    # цена выставления: позиция в рыночном коридоре по ликвидности, минус риски
    list_price = _round((lo + (hi - lo) * (0.35 + 0.45 * liq)) * (1 + risk), 10_000)
    bargain = BARGAIN[0] + (BARGAIN[1] - BARGAIN[0]) * liq
    expected_sale = _round(list_price * (1 - bargain), 5_000)
    market_days = m.get("avg_sale_days") or DEFAULT_SALE_DAYS
    expected_days = int(round(market_days * (1 + max(0.0, list_price / imv - 1) * 4)))

    # выкуп: цена продажи − плановая ВП по метрике − переподготовка − стоянка сверх цели − запас на риск
    margin = calc_gross_margin(expected_sale, category, reception_type)
    holding = expected_sale * HOLDING_COST_PER_10_DAYS * max(0, expected_days - TARGET_DAYS) / 10
    conf = confidence(m)
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
    reasons = liq_text + reasons
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
