# -*- coding: utf-8 -*-
"""Площадки: Авито, Авто.ру, Дром."""
from buyout.platforms.base import IncomingMessage, Platform, PlatformError  # noqa: F401

_instances = {}


def get_platform(name: str) -> Platform:
    if name not in _instances:
        from buyout.platforms.browser import BrowserPlatform
        _instances[name] = BrowserPlatform(name)
    return _instances[name]


def register(platform: Platform) -> None:
    """Подменить площадку (тесты, демо-режим)."""
    _instances[platform.name] = platform


async def close_all() -> None:
    for p in list(_instances.values()):
        await p.close()
    _instances.clear()
