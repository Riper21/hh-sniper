"""Карьер-сайты: протокол адаптера + реестр (ПЛАН §4.3).

Порт career_managers.py + jobtoo_extract.py. Главный вердикт исследования
(4А): карьер-сайты — «прямой прорыв» (Alfa API: 100% имя+email найм-менеджера).

06.11: добавлены sber (sitemap+SSR, разведка), avito (SSR), ozon
(заглушка: закрыт анти-ботом). Сбер/Авито — матчинговые каналы (email нет,
отклик через сайт): питают ТРИГГЕР (вакансия компании ↔ база контактов),
по схеме hh (вердикт 3А).
"""
from __future__ import annotations

from snpr.outreach.career.alfa import AlfaAdapter
from snpr.outreach.career.avito import AvitoAdapter
from snpr.outreach.career.huntflow import HuntflowAdapter
from snpr.outreach.career.jobtoo import JobtooAdapter
from snpr.outreach.career.ozon import OzonAdapter
from snpr.outreach.career.sber import SberAdapter

ADAPTERS: dict[str, type] = {
    "alfa": AlfaAdapter,
    "huntflow": HuntflowAdapter,
    "jobtoo": JobtooAdapter,
    "sber": SberAdapter,
    "avito": AvitoAdapter,
    "ozon": OzonAdapter,
}


def get_adapter(name: str) -> type:
    if name not in ADAPTERS:
        raise KeyError(f"неизвестный career-адаптер: {name!r}")
    return ADAPTERS[name]
