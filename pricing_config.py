# -*- coding: utf-8 -*-
"""
Параметры расчёта (баллы категорий, коэффициенты ПЦП, ценовые диапазоны
доходности) читаются с вкладки "Параметры Метрики" в той же Google
Таблице, что и остальные настройки бота (история оценок, иерархия
согласования). Так бизнес-правила можно менять прямо в таблице — без
правки кода и без перезапуска бота.

Формат вкладки — плоская таблица, ОДНА строка = один параметр, колонки
(первая строка — заголовки, должны совпадать буквально):

Раздел | Параметр | Значение1 | Значение2

Разделы (буквально, как в "Раздел"):
  Окрасы                        | вариант окраса           | баллы            | —
  Тех.состояние                 | вариант состояния        | баллы            | —
  Доп.данные                    | вариант                  | баллы            | —
  Порог категории                | "Категория А"/"Категория В" | порог баллов | —
  ПЦП коэффициент                | "Категория А/В/С"        | % РОП, целым числом (напр. 100 = 100%) | % ДДЦ (напр. 105 = 105%)
  Доходность Категория А         | "0-500000" (диапазон цены) | доходность в % (напр. 25 = 25%) | мин.доход в рублях (70000, можно пусто)
  Доходность Категория В и С     | "0-500000"                | доходность в %   | мин.доход в рублях
  Плоская ставка по типу приёма  | "Trade-Up"/"Выкуп с улицы"/"Trade-In на ПИ" | доходность в % | мин.доход в рублях
  GM2 формула                    | "Фикс. расход 1"           | сумма в рублях (15000) | —
  GM2 формула                    | "% от цены выкупа"         | процент (1.75)          | множитель (1.5)
  GM2 формула                    | "Фикс. расход 2"           | сумма в рублях (40000) | —

Если вкладку не удалось прочитать (нет сети/доступа, опечатка, вкладка
ещё пустая) — используются дефолты ниже (последние известные правила),
чтобы расчёт в боте никогда не падал и не вставал из-за таблицы.
"""
import time
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEET_ID

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TAB_NAME = "Параметры Метрики"
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 часов — не дёргаем Google лишний раз

_client_cache = None
_params_cache = None
_params_cache_at = 0.0

# --- дефолты (последние известные правила; используются как запасной вариант) ---
_DEFAULTS = {
    "colors_score": {"Не более 1": 10, "2 - 3 окраса": 5, "4 и более": -5},
    "condition_score": {
        "Не требует вложений": 10,
        "Легко устранимые тех. недостатки.": 10,
        "Серьезные недостатки. Не корректная работа ДВС, КПП и тд": -15,
    },
    "extra_score": {"Не применимо": 0, "Использовался в такси, каршеринге": -20, "Более 3х ДТП": -11},
    "category_thresholds": [("Категория А", 20), ("Категория В", 10), ("Категория С", float("-inf"))],
    "ptsp_coef": {
        "Категория А": {"РОП": 1.00, "ДДЦ": 1.05},
        "Категория В": {"РОП": 0.95, "ДДЦ": 0.99},
        "Категория С": {"РОП": 0.90, "ДДЦ": 0.949},
    },
    "tradein_cat_a_tiers": [
        (0, 500_000, 0.25, 70_000),
        (500_001, 800_000, 0.20, 130_000),
        (800_001, 1_200_000, 0.33, 150_000),
        (1_200_001, 1_500_000, 0.145, None),
        (1_500_001, 2_000_000, 0.13, None),
        (2_000_001, float("inf"), 0.12, None),
    ],
    "tradein_cat_bc_tiers": [
        (0, 500_000, 0.25, 70_000),
        (500_001, 800_000, 0.20, 130_000),
        (800_001, 1_200_000, 0.16, 150_000),
        (1_200_001, 1_500_000, 0.145, None),
        (1_500_001, 2_000_000, 0.13, None),
        (2_000_001, float("inf"), 0.12, None),
    ],
    "flat_rates": {
        "Trade-Up": {"доходность": 0.12, "мин.доход": 100_000},
        "Выкуп с улицы": {"доходность": 0.14, "мин.доход": 200_000},
        "Trade-In на ПИ": {"доходность": 0.14, "мин.доход": 200_000},
    },
    "gm2_formula": {
        "fixed_1": 15_000,          # фикс. расход №1
        "purchase_rate": 0.0175,    # доля от цены выкупа (1.75%)
        "purchase_rate_multiplier": 1.5,  # множитель на эту долю
        "fixed_2": 40_000,          # фикс. расход №2
    },
}


def _get_worksheet():
    global _client_cache
    if _client_cache is None:
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _client_cache = gspread.authorize(creds)
    sh = _client_cache.open_by_key(GOOGLE_SHEET_ID)
    return sh.worksheet(TAB_NAME)


def _get_records():
    """get_all_records() с value_render_option='UNFORMATTED_VALUE' — читаем
    настоящие числа из ячеек, а не их локализованное текстовое отображение.
    Без этого библиотека трактует "1,00" (русский формат, точка = 1.00) как
    "1,00" в американском формате (запятая = разделитель тысяч) и превращает
    его в число 100 — именно так родился баг с суммами, завышенными в 100 раз."""
    ws = _get_worksheet()
    return ws.get_all_records(value_render_option="UNFORMATTED_VALUE")


def _num(v, default=None):
    """Безопасный разбор числа. С UNFORMATTED_VALUE ячейки обычно уже
    приходят как настоящие int/float, но на случай, если что-то всё же
    пришло текстом — трактуем запятую как русский десятичный разделитель,
    а не как разделитель тысяч (в этом и была причина бага x100)."""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def _parse_range_key(key: str):
    """'0-500000' -> (0.0, 500000.0); '2000001-∞' -> (2000001.0, inf)"""
    parts = str(key).split("-")
    lo = _num(parts[0], 0) or 0
    hi_raw = parts[1].strip() if len(parts) > 1 else ""
    hi = float("inf") if hi_raw in ("", "∞", "inf") else _num(hi_raw, float("inf"))
    return lo, hi


def _pct_to_fraction(v, big_threshold: float = 3) -> float:
    """Превращает число из ячейки в долю (0.17 = 17%), независимо от того,
    хранит ли Google Таблица его как долю (если ячейка сама отформатирована
    как проценты — тогда 17% уже лежит внутри как 0.17) или как целое число
    процента (17). Если |число| больше big_threshold — считаем, что это
    целый процент, и делим на 100. Иначе — это уже готовая доля."""
    n = _num(v, 0) or 0
    if abs(n) > big_threshold:
        return n / 100
    return n


def _load_from_sheet() -> Optional[dict]:
    try:
        rows = _get_records()
    except Exception as e:  # noqa: BLE001
        print(f"[pricing_config] Не удалось прочитать вкладку «{TAB_NAME}»: {e}", flush=True)
        return None
    if not rows:
        return None

    colors_score, condition_score, extra_score = {}, {}, {}
    category_thresholds = []
    ptsp_coef = {}
    tradein_cat_a, tradein_cat_bc = [], []
    flat_rates = {}
    gm2_formula = dict(_DEFAULTS["gm2_formula"])

    for row in rows:
        section = str(row.get("Раздел", "")).strip()
        param = str(row.get("Параметр", "")).strip()
        v1 = row.get("Значение1")
        v2 = row.get("Значение2")

        if section == "Окрасы":
            colors_score[param] = int(_num(v1, 0))
        elif section == "Тех.состояние":
            condition_score[param] = int(_num(v1, 0))
        elif section == "Доп.данные":
            extra_score[param] = int(_num(v1, 0))
        elif section == "Порог категории":
            category_thresholds.append((param, _num(v1, 0)))
        elif section == "ПЦП коэффициент":
            # Число может лежать и как доля (1.00), и как целый процент (100) —
            # зависит от того, отформатирована ли ячейка в Таблице как "%".
            ptsp_coef[param] = {"РОП": _pct_to_fraction(v1), "ДДЦ": _pct_to_fraction(v2)}
        elif section == "Доходность Категория А":
            lo, hi = _parse_range_key(param)
            tradein_cat_a.append((lo, hi, _pct_to_fraction(v1), _num(v2)))
        elif section == "Доходность Категория В и С":
            lo, hi = _parse_range_key(param)
            tradein_cat_bc.append((lo, hi, _pct_to_fraction(v1), _num(v2)))
        elif section == "Плоская ставка по типу приёма":
            flat_rates[param] = {"доходность": _pct_to_fraction(v1), "мин.доход": _num(v2, 0)}
        elif section == "GM2 формула":
            if param == "Фикс. расход 1":
                gm2_formula["fixed_1"] = _num(v1, _DEFAULTS["gm2_formula"]["fixed_1"])
            elif param == "% от цены выкупа":
                gm2_formula["purchase_rate"] = _pct_to_fraction(v1, big_threshold=1)
                gm2_formula["purchase_rate_multiplier"] = _num(v2, _DEFAULTS["gm2_formula"]["purchase_rate_multiplier"])
            elif param == "Фикс. расход 2":
                gm2_formula["fixed_2"] = _num(v1, _DEFAULTS["gm2_formula"]["fixed_2"])

    if category_thresholds:
        category_thresholds.append(("Категория С", float("-inf")))
        category_thresholds.sort(key=lambda x: -x[1])
    else:
        category_thresholds = _DEFAULTS["category_thresholds"]

    return {
        "colors_score": colors_score or _DEFAULTS["colors_score"],
        "condition_score": condition_score or _DEFAULTS["condition_score"],
        "extra_score": extra_score or _DEFAULTS["extra_score"],
        "category_thresholds": category_thresholds,
        "ptsp_coef": ptsp_coef or _DEFAULTS["ptsp_coef"],
        "tradein_cat_a_tiers": tradein_cat_a or _DEFAULTS["tradein_cat_a_tiers"],
        "tradein_cat_bc_tiers": tradein_cat_bc or _DEFAULTS["tradein_cat_bc_tiers"],
        "flat_rates": flat_rates or _DEFAULTS["flat_rates"],
        "gm2_formula": gm2_formula,
    }


def get_params() -> dict:
    """Актуальные параметры расчёта, с кешем на CACHE_TTL_SECONDS. При любой
    ошибке чтения — тихо откатывается на дефолты, расчёт никогда не падает."""
    global _params_cache, _params_cache_at
    now = time.time()
    if _params_cache is not None and (now - _params_cache_at) < CACHE_TTL_SECONDS:
        return _params_cache

    fresh = _load_from_sheet()
    _params_cache = fresh if fresh is not None else dict(_DEFAULTS)
    _params_cache_at = now
    return _params_cache
