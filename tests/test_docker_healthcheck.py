"""HEALTHCHECK контейнера (scripts/docker/healthcheck.py, этап 3).

Скрипт не пакет — гоняем субпроцессом, как настоящую Docker-команду.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "docker" / "healthcheck.py"


def _run(marker: Path) -> int:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), str(marker)],
        capture_output=True,
        check=False,
    )
    return proc.returncode


def test_fresh_marker_healthy(tmp_path: Path) -> None:
    marker = tmp_path / "last_run.txt"
    marker.write_text(f"{time.time():.0f}\n", encoding="utf-8")
    assert _run(marker) == 0


def test_missing_marker_healthy_first_boot(tmp_path: Path) -> None:
    assert _run(tmp_path / "last_run.txt") == 0


def test_garbage_marker_healthy(tmp_path: Path) -> None:
    marker = tmp_path / "last_run.txt"
    marker.write_text("мусор", encoding="utf-8")
    assert _run(marker) == 0


def test_stale_marker_unhealthy(tmp_path: Path) -> None:
    marker = tmp_path / "last_run.txt"
    marker.write_text(f"{time.time() - 4 * 3600:.0f}\n", encoding="utf-8")
    assert _run(marker) == 1


def test_negative_age_is_fresh(tmp_path: Path) -> None:
    # скачок часов контейнера вперёд: маркер «из будущего» — здоров
    marker = tmp_path / "last_run.txt"
    marker.write_text(f"{time.time() + 600:.0f}\n", encoding="utf-8")
    assert _run(marker) == 0
