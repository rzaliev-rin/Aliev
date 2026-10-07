# -*- coding: utf-8 -*-
"""
Параметры расчёта метрики закупки АСП v2.0 (баллы категорий, пробег в год,
ПЦП по автосалонам, доходность, потолок закупки, GM2) читаются с вкладки
"Параметры Метрики v2" в той же Google Таблице, что и остальные настройки
бота (история оценок, иерархия согласования). Так бизнес-правила можно
менять прямо в таблице — без правки кода и без перезапуска бота.

Вкладка v2 — новая (старая "Параметры Метрики" с нормами v1 больше не
читается, чтобы старые цифры не смешались с новыми). Готовое содержимое
для вставки — файл params_table_v2.csv в репозитории.

Формат вкладки — плоская таблица, ОДНА строка = один параметр, колонки
(первая строка — заголовки, должны совпадать буквально):

Раздел | Параметр | Значение1 | Значение2 | Значение3 | Значение4 | Значение5

Разделы (буквально, как в "Раздел"):
  Окрасы                    | вариант окраса      | баллы
  Тех.состояние             | вариант состояния   | баллы
  Доп.данные                | вариант             | баллы
  Пробег в год              | любое название      | до, км в год (пусто = «более») | баллы
  Порог категории           | "Категория А"/"Категория В" | порог баллов (С — всё, что ниже)
  ПЦП автосалона            | название салона как в MaxPoster ("3047: АСП Краснодар Changan")
                              | % кат. А | % кат. В | % кат. С (напр. 96 = 96%) | Значение4: город | Значение5: бренд
                              | Новый салон = новая строка; бот подхватит её в течение CACHE_TTL_SECONDS.
  Выкуп с улицы: поправка ПЦП | "Категория А/В/С" | минус п.п. к ПЦП салона (напр. 6)
  Полномочия ДДЦ            | "+ п.п. к ПЦП"      | п.п. (напр. 3)
  Доходность Категория А    | "0-500000" (диапазон ПЦП) | доходность в % | мин. ВП в рублях
  Доходность Категория В и С| "0-500000"          | доходность в %   | мин. ВП в рублях
  Плоская ставка по типу приёма | "Выкуп с улицы"/"Trade-In на ПИ" | доходность в % | мин. ВП в рублях
  Потолок к Авито           | "Категория А/В/С"   | % для Trade-In/Up/ПИ | % для выкупа с улицы
  GM2 формула               | "Фикс. расход 1"    | сумма в рублях (15000)
  GM2 формула               | "% от цены выкупа"  | процент в месяц (1.75) | срок до продажи, мес. (1.5)
  GM2 формула               | "Фикс. расход 2"    | сумма в рублях (40000)

Если вкладку не удалось прочитать (нет сети/доступа, опечатка, вкладка
ещё пустая) — используются дефолты ниже (= метрика v2.0 от 24.09.2026),
чтобы расчёт в боте никогда не падал и не вставал из-за таблицы.
"""
import time
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEET_ID

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TAB_NAME = "Параметры Метрики v2"
METRIC_VERSION = "2.0 от 24.09.2026"
CACHE_TTL_SECONDS = 15 * 60  # 15 минут: новый салон или норма из таблицы применяются быстро
RETRY_AFTER_ERROR_SECONDS = 5 * 60  # после ошибки чтения — повторная попытка через 5 минут

INF = float("inf")

_client_cache = None
_params_cache = None
_params_cache_at = 0.0

# --- дефолты = «Метрики приёма v2.0 24.09.2026 Экспокар.xlsx», лист «Метрика оценки» ---
_DEFAULTS = {
    "colors_score": {"Не более 1": 10, "2 - 3 окраса": 5, "4 и более": -5},
    "condition_score": {
        "Не требует вложений": 10,
        "Легко устранимые тех. недостатки.": 10,
        "Серьезные недостатки. Не корректная работа ДВС, КПП и тд": -15,
    },
    "extra_score": {
        "Не применимо": 0,
        "Использовался в такси, каршеринге": -20,
        "Возраст авто до 2х лет": -2,
        "Более 3х ДТП": -11,
    },
    # (до, км в год включительно; баллы) — по возрастанию, последняя строка — «более»
    "mileage_score": [(13_000, 10), (18_000, 5), (INF, 0)],
    "category_thresholds": [("Категория А", 30), ("Категория В", 20), ("Категория С", -INF)],
    # ПЦП от Авито Оценки по автосалонам (РОП) — Trade-In, Trade-Up, ПИ.
    # Ключ — название салона как в MaxPoster (номер «3047:» и «!» в начале
    # при сравнении не учитываются). Коэффициенты — по городу салона
    # (B88:D93 метрики v2.0); салоны Toyota — по коэффициентам Краснодара.
    "salons": {
        "3047: АСП Краснодар Changan": {"ptsp": {"Категория А": 0.99, "Категория В": 0.97, "Категория С": 0.92},
                                        "city": "Краснодар", "brand": "Changan"},
        "3265: АСП Волгоград Chery": {"ptsp": {"Категория А": 0.95, "Категория В": 0.92, "Категория С": 0.82},
                                      "city": "Волгоград", "brand": "Chery"},
        "3515: АСП Казань Chery": {"ptsp": {"Категория А": 0.96, "Категория В": 0.91, "Категория С": 0.78},
                                   "city": "Казань", "brand": "Chery"},
        "3896: АСП Новосибирск Chery": {"ptsp": {"Категория А": 0.94, "Категория В": 0.94, "Категория С": 0.84},
                                        "city": "Новосибирск", "brand": "Chery"},
        "8991: АСП НН CHERY": {"ptsp": {"Категория А": 0.97, "Категория В": 0.93, "Категория С": 0.81},
                               "city": "Нижний Новгород", "brand": "Chery"},
        "10088: АСП Тюмень Exeed": {"ptsp": {"Категория А": 0.98, "Категория В": 0.93, "Категория С": 0.89},
                                    "city": "Тюмень", "brand": "Exeed"},
        "10089: !АСП Тюмень Toyota действующий": {"ptsp": {"Категория А": 0.99, "Категория В": 0.97, "Категория С": 0.92},
                                                  "city": "Тюмень", "brand": "Toyota"},
        "10120: АСП Toyota - Екатеринбург": {"ptsp": {"Категория А": 0.99, "Категория В": 0.97, "Категория С": 0.92},
                                             "city": "Екатеринбург", "brand": "Toyota"},
    },
    # Выкуп с улицы: ПЦП салона минус (строки 20-22 − строки 56-58)
    "street_ptsp_minus": {"Категория А": 0.0, "Категория В": 0.01, "Категория С": 0.06},
    "ddc_ptsp_bonus": 0.03,  # B95: полномочия ДДЦ +3 п.п. к ПЦП
    # (от, до, доходность к ПЦП, мин. ВП) — ценовые группы по ПЦП
    "tradein_cat_a_tiers": [
        (0, 500_000, 0.24, 90_000),
        (500_001, 800_000, 0.19, 120_000),
        (800_001, 1_200_000, 0.16, 150_000),
        (1_200_001, 1_500_000, 0.14, 180_000),
        (1_500_001, 2_000_000, 0.13, 210_000),
        (2_000_001, INF, 0.11, 250_000),
    ],
    "tradein_cat_bc_tiers": [
        (0, 500_000, 0.27, 90_000),
        (500_001, 800_000, 0.22, 130_000),
        (800_001, 1_200_000, 0.18, 160_000),
        (1_200_001, 1_500_000, 0.16, 190_000),
        (1_500_001, 2_000_000, 0.15, 230_000),
        (2_000_001, INF, 0.12, 250_000),
    ],
    "flat_rates": {
        "Выкуп с улицы": {"доходность": 0.17, "мин.доход": 200_000},
        "Trade-In на ПИ": {"доходность": 0.17, "мин.доход": 200_000},
    },
    # Потолок закупки к Авито Оценке (A75:C77)
    "ceiling": {
        "Категория А": {"default": 0.85, "street": 0.85},
        "Категория В": {"default": 0.80, "street": 0.80},
        "Категория С": {"default": 0.72, "street": 0.70},
    },
    "gm2_formula": {
        "fixed_1": 15_000,          # B80: расходы на сделку
        "purchase_rate": 0.0175,    # B82: стоимость денег, % в месяц
        "purchase_rate_multiplier": 1.5,  # B83: срок до продажи, мес.
        "fixed_2": 40_000,          # B81: прочие расходы
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
        return float(str(v).strip().replace(" ", "").replace("%", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def _parse_range_key(key: str):
    """'0-500000' -> (0.0, 500000.0); '2000001-∞' -> (2000001.0, inf)"""
    parts = str(key).split("-")
    lo = _num(parts[0], 0) or 0
    hi_raw = parts[1].strip() if len(parts) > 1 else ""
    hi = INF if hi_raw in ("", "∞", "inf") else _num(hi_raw, INF)
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


def _pp_to_fraction(v) -> float:
    """Процентные пункты (поправки 0..10 п.п.): «3» -> 0.03, но если ячейка
    отформатирована как % и внутри уже лежит 0.03 — оставляем как есть."""
    n = _num(v, 0) or 0
    return n / 100 if abs(n) >= 0.5 else n


def _load_from_sheet() -> Optional[dict]:
    try:
        rows = _get_records()
    except Exception as e:  # noqa: BLE001
        print(f"[pricing_config] Не удалось прочитать вкладку «{TAB_NAME}»: {e}", flush=True)
        return None
    if not rows:
        return None

    colors_score, condition_score, extra_score = {}, {}, {}
    mileage_score = []
    category_thresholds = []
    salons = {}
    street_ptsp_minus = {}
    ddc_ptsp_bonus = None
    tradein_cat_a, tradein_cat_bc = [], []
    flat_rates = {}
    ceiling = {}
    gm2_formula = dict(_DEFAULTS["gm2_formula"])

    for row in rows:
        section = str(row.get("Раздел", "")).strip()
        param = str(row.get("Параметр", "")).strip()
        v1 = row.get("Значение1")
        v2 = row.get("Значение2")
        v3 = row.get("Значение3")
        v4 = row.get("Значение4")
        v5 = row.get("Значение5")

        if section == "Окрасы":
            colors_score[param] = int(_num(v1, 0))
        elif section == "Тех.состояние":
            condition_score[param] = int(_num(v1, 0))
        elif section == "Доп.данные":
            extra_score[param] = int(_num(v1, 0))
        elif section == "Пробег в год":
            mileage_score.append((_num(v1, INF), int(_num(v2, 0))))
        elif section == "Порог категории":
            category_thresholds.append((param, _num(v1, 0)))
        elif section == "ПЦП автосалона":
            if param:
                salons[param] = {
                    "ptsp": {
                        "Категория А": _pct_to_fraction(v1),
                        "Категория В": _pct_to_fraction(v2),
                        "Категория С": _pct_to_fraction(v3),
                    },
                    "city": str(v4 or "").strip(),
                    "brand": str(v5 or "").strip(),
                }
        elif section == "Выкуп с улицы: поправка ПЦП":
            street_ptsp_minus[param] = _pp_to_fraction(v1)
        elif section == "Полномочия ДДЦ":
            ddc_ptsp_bonus = _pp_to_fraction(v1)
        elif section == "Доходность Категория А":
            lo, hi = _parse_range_key(param)
            tradein_cat_a.append((lo, hi, _pct_to_fraction(v1), _num(v2)))
        elif section == "Доходность Категория В и С":
            lo, hi = _parse_range_key(param)
            tradein_cat_bc.append((lo, hi, _pct_to_fraction(v1), _num(v2)))
        elif section == "Плоская ставка по типу приёма":
            flat_rates[param] = {"доходность": _pct_to_fraction(v1), "мин.доход": _num(v2, 0)}
        elif section == "Потолок к Авито":
            default = _pct_to_fraction(v1)
            ceiling[param] = {"default": default, "street": _pct_to_fraction(v2) if _num(v2) is not None else default}
        elif section == "GM2 формула":
            if param == "Фикс. расход 1":
                gm2_formula["fixed_1"] = _num(v1, _DEFAULTS["gm2_formula"]["fixed_1"])
            elif param == "% от цены выкупа":
                gm2_formula["purchase_rate"] = _pct_to_fraction(v1, big_threshold=1)
                gm2_formula["purchase_rate_multiplier"] = _num(v2, _DEFAULTS["gm2_formula"]["purchase_rate_multiplier"])
            elif param == "Фикс. расход 2":
                gm2_formula["fixed_2"] = _num(v1, _DEFAULTS["gm2_formula"]["fixed_2"])

    if category_thresholds:
        category_thresholds.append(("Категория С", -INF))
        category_thresholds.sort(key=lambda x: -x[1])
    else:
        category_thresholds = _DEFAULTS["category_thresholds"]

    return {
        "colors_score": colors_score or _DEFAULTS["colors_score"],
        "condition_score": condition_score or _DEFAULTS["condition_score"],
        "extra_score": extra_score or _DEFAULTS["extra_score"],
        "mileage_score": sorted(mileage_score) or _DEFAULTS["mileage_score"],
        "category_thresholds": category_thresholds,
        "salons": salons or _DEFAULTS["salons"],
        "street_ptsp_minus": street_ptsp_minus or _DEFAULTS["street_ptsp_minus"],
        "ddc_ptsp_bonus": ddc_ptsp_bonus if ddc_ptsp_bonus is not None else _DEFAULTS["ddc_ptsp_bonus"],
        # _vlookup_tier ожидает диапазоны по возрастанию — сортируем на случай,
        # если строки в таблице переставили местами
        "tradein_cat_a_tiers": sorted(tradein_cat_a, key=lambda t: t[0]) or _DEFAULTS["tradein_cat_a_tiers"],
        "tradein_cat_bc_tiers": sorted(tradein_cat_bc, key=lambda t: t[0]) or _DEFAULTS["tradein_cat_bc_tiers"],
        "flat_rates": flat_rates or _DEFAULTS["flat_rates"],
        "ceiling": ceiling or _DEFAULTS["ceiling"],
        "gm2_formula": gm2_formula,
    }


def get_params() -> dict:
    """Актуальные параметры расчёта, с кешем на CACHE_TTL_SECONDS. При ошибке
    чтения — последние прочитанные из таблицы параметры (или дефолты, если
    таблицу ещё ни разу не удалось прочитать); расчёт никогда не падает."""
    global _params_cache, _params_cache_at
    now = time.time()
    if _params_cache is not None and (now - _params_cache_at) < CACHE_TTL_SECONDS:
        return _params_cache

    fresh = _load_from_sheet()
    if fresh is not None:
        _params_cache, _params_cache_at = fresh, now
    elif _params_cache is not None:
        # Google временно недоступен — оставляем последние прочитанные из
        # таблицы параметры (а не откатываемся на дефолты) и пробуем снова
        # через RETRY_AFTER_ERROR_SECONDS, а не через полный срок кеша.
        _params_cache_at = now - CACHE_TTL_SECONDS + RETRY_AFTER_ERROR_SECONDS
    else:
        _params_cache = dict(_DEFAULTS)
        _params_cache_at = now - CACHE_TTL_SECONDS + RETRY_AFTER_ERROR_SECONDS
    return _params_cache
