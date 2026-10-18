"""HEALTHCHECK контейнера hh-snpr (этап 3, 18.10): свежесть маркера тика.

Маркер data/last_run.txt (epoch-секунды) трогает каждый УСПЕШНЫЙ прогон
sne (sne.py: _touch_marker) — тик демона гоняет sne каждую минуту, так
что протухший на часы маркер = все тики мимо (сеть/токен/крэш) — это и
есть «нездоров». Отсутствие/мусор маркера = НЕ болен (первый тик ещё не
дошёл, старый формат): false positive дороже false negative. Аргумент —
путь к маркеру (для тестов), дефолт — путь внутри контейнера.

Запуск: python scripts/docker/healthcheck.py [marker-path]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

STALE_SECONDS = 3 * 60 * 60
DEFAULT_MARKER = Path("/app/data/last_run.txt")


def healthy(marker: Path, now: float | None = None) -> bool:
    try:
        stamp = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    moment = time.time() if now is None else now
    return (moment - stamp) < STALE_SECONDS


def main(argv: list[str]) -> int:
    marker = Path(argv[1]) if len(argv) > 1 else DEFAULT_MARKER
    return 0 if healthy(marker) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
