# -*- coding: utf-8 -*-
"""Калибровка подсказки по цене по истории продаж. Запуск: python tests/test_recommend.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_FILE", "/nonexistent.json")

import pricing_config  # noqa: E402
pricing_config._load_from_sheet = lambda: None
import recommend  # noqa: E402

recommend.CALIBRATION_FILE = os.path.join(tempfile.mkdtemp(), "calibration.json")


def _rows(dealer, brand, model, imv, buy, sale, n, status="Продано"):
    return [{"dealer": dealer, "brand": brand, "model": model, "status": status, "imv": imv,
             "purchase_cost": buy, "sale_cost": sale, "created": "2026-08-01T10:00:00+03:00",
             "completion_date": "2026-08-21T10:00:00+03:00"} for _ in range(n)]


def test_build_excludes_transit_and_unsold():
    rows = (_rows("АСП Новосибирск Chery", "Kia", "Rio", 900_000, 700_000, 850_000, 10)
            + _rows("АСП Краснодар Changan", "BMW", "5 серии", 3_446_000, 4_978_000, 5_003_000, 8)
            + _rows("АСП Новосибирск Chery", "Kia", "Rio", 900_000, 0, 0, 30, status="Завершена по сроку давности"))
    cal = recommend.build_calibration(rows)
    assert cal["salons"]["асп новосибирск chery"]["n"] == 10
    assert abs(cal["salons"]["асп новосибирск chery"]["sale_ratio"] - 850 / 900) < 1e-3
    assert cal["salons"]["асп новосибирск chery"]["days"] == 20
    assert "асп краснодар changan" not in cal["salons"]


def test_defaults_before_first_export():
    stats = recommend.calibrated_stats("3515: АСП Казань Chery", "Kia Rio")
    assert stats["level"] == "salon" and abs(stats["sale_ratio"] - 0.895) < 1e-6


def test_model_history_outweighs_salon():
    rows = (_rows("АСП Новосибирск Chery", "Chery", "Tiggo 7 Pro Max", 1_000_000, 900_000, 1_000_000, 20)
            + _rows("АСП Новосибирск Chery", "Kia", "Rio", 1_000_000, 700_000, 900_000, 20))
    recommend.save_calibration(recommend.build_calibration(rows))
    stats = recommend.calibrated_stats("3896: АСП Новосибирск Chery", "Chery Tiggo 7 Pro Max")
    assert stats["level"] == "model" and stats["sale_ratio"] > 0.96  # салон 0,95, модель 1,0 (вес 20/30)
    assert recommend.calibrated_stats("3896: АСП Новосибирск Chery", "Lada Granta")["level"] == "salon"
    os.remove(recommend.CALIBRATION_FILE)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
