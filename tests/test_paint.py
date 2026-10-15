# -*- coding: utf-8 -*-
"""Окрасы по осмотру MaxPoster: детали в защитной плёнке не считаются окрашенными
из-за повышенной толщины ЛКП. Запуск: python tests/test_paint.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from maxposter_client import analyze_paint  # noqa: E402


def _part(thickness_to, defects=None, comment=None):
    return {"layerThicknessFrom": 100, "layerThicknessTo": thickness_to, "defects": defects or [], "comment": comment}


def test_thick_paint_without_film_counts():
    assert analyze_paint([_part(320), _part(120)]) == (1, 0)


def test_film_in_comment_or_defect_is_not_paint():
    parts = [_part(350, comment="Деталь в плёнке"), _part(300, [{"name": "Оклеено пленкой"}]),
             _part(500, [{"name": "Скол", "comment": "антигравийная плёнка"}])]
    assert analyze_paint(parts) == (0, 3)


def test_whole_body_film_from_resume():
    assert analyze_paint([_part(320), _part(400)], "Автомобиль полностью в пленке") == (0, 2)
    assert analyze_paint([_part(320)], "максималка, тряпка, капот косметика") == (1, 0)


def test_painted_tag_counts_even_under_film():
    assert analyze_paint([_part(150, [{"name": "Покрашено"}], comment="в плёнке")]) == (1, 0)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
