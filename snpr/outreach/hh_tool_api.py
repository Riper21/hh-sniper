"""Мост к api_client снайпера для hh-канала outreach (§4.4).

Ленивая загрузка: hh_tool тянет vendored hh-applicant-tool — в тестах
модуля она не нужна (фейк подставляется напрямую в hh_source.run).
"""
from __future__ import annotations

from typing import Any


def get_hh_api() -> Any | None:
    """api_client снайпера или None, если авторизации нет."""
    try:
        from ..hh_tool import SavingHHApplicantTool

        tool = SavingHHApplicantTool()
        return tool.api_client
    except Exception:  # noqa: BLE001 — нет авторизации/тулкита
        return None
