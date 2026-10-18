"""Обёртка над hh-applicant-tool: обход бага потери токена при успехе.

Баг базы (пиннинг 833cb658, tool.py:1039-1066): блок
save_token()/save_cookies()/save_config() выполняется только если операция
упала исключением (или запущена с --loop) — при успехе run() возвращает код
раньше. authorize проходит успешно, OAuth-токен получен и потерян:
auth.json не создаётся, whoami даёт 403.

Обход без патча базы: подкласс с сохранением состояния в finally —
независимо от исхода операции. Этап 5 использует этот же класс.

Запуск авторизации: python -m sniper.hh_tool <email_или_телефон>
"""

from __future__ import annotations

import sys
from typing import Sequence

import urllib3

from hh_applicant_tool.tool import HHApplicantTool


class SavingHHApplicantTool(HHApplicantTool):
    """HHApplicantTool, который сохраняет токен/cookies при любом исходе."""

    def run(self, argv: Sequence[str] | None = None) -> int | None:
        try:
            return super().run(argv)
        finally:
            self._persist()

    def _persist(self) -> None:
        try:
            if self.save_token():
                print("Токен сохранён в auth.json")
        except Exception as ex:
            print(f"Не удалось сохранить токен: {ex}")
        try:
            self.save_cookies()
        except Exception as ex:
            print(f"Не удалось сохранить cookies: {ex}")
        try:
            self._save_config_if_needed()
        except Exception:
            pass


def build_auth_argv(argv: Sequence[str]) -> list[str]:
    # Капча вручную: AI-креды базы не настроены, наш SNIPE_AI_* ей не читается.
    return ["--captcha-manual", "authorize", *argv]


def main(argv: Sequence[str] | None = None) -> int:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    args = list(argv) if argv is not None else sys.argv[1:]
    tool = SavingHHApplicantTool()
    ret = tool.run(build_auth_argv(args))
    return ret if isinstance(ret, int) else 0


if __name__ == "__main__":
    sys.exit(main())
