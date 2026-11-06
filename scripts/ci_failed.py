"""CI-хелпер: печатает имена упавших тестов из junit-xml.

Вызывается из ci.yml после падения pytest — имена падений видны прямо
в логе шага Actions (артефакт-логи не всем доступны). exit 1 всегда:
шаг уже упал, мы лишь доносим детали.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_FAIL_RE = re.compile(
    r'<testcase[^>]*\bname="([^"]+)"[^>]*>\s*'
    r'<(?:failure|error)\b[^>]*\bmessage="([^"]{0,300})"'
)
_FALLBACK_RE = re.compile(
    r'<testcase[^>]*\bname="([^"]+)"[^>]*>\s*<(?:failure|error)\b'
)
_TOTALS_RE = re.compile(
    r'\btests="(\d+)"[^>]*\bfailures="(\d+)"[^>]*\berrors="(\d+)"'
)


def main() -> int:
    path = Path("report.xml")
    if not path.exists():
        print("report.xml нет — pytest упал до записи junit")
        return 1
    xml = path.read_text(encoding="utf-8", errors="replace")
    pairs = _FAIL_RE.findall(xml)
    if not pairs:
        pairs = [(name, "") for name in _FALLBACK_RE.findall(xml)]
    for name, msg in pairs:
        print(f"FAIL: {name}")
        clean = (
            msg.replace("&#10;", " ").replace("&quot;", chr(34)).replace("&amp;", "&")
        )
        if clean:
            print(f"  msg: {clean[:220]}")
    totals = _TOTALS_RE.search(xml)
    print("TOTALS (tests/failures/errors):", totals.groups() if totals else "?")
    # job summary: виден на странице прогона БЕЗ токенов (GITHUB_STEP_SUMMARY)
    import os

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path and pairs:
        lines = ["## Упавшие тесты\n"]
        for name, msg in pairs:
            clean = (
                msg.replace("&#10;", " ").replace("&quot;", chr(34)).replace("&amp;", "&")
            )
            lines.append(f"- **{name}** — {clean[:200]}")
        Path(summary_path).write_text("\n".join(lines), encoding="utf-8")
    return 1


if __name__ == "__main__":
    sys.exit(main())
