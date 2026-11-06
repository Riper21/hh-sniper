"""Сторы outreach-модуля: единая upsert-конвенция JSONL (ПЛАН §3.3).

Порт паттерна сторов снайпера (tmp-replace атомарность, sne.py SeenStore и др.):
- JsonlStore — справочники, upsert по явному ключу (append запрещён);
- CampaignLog — единственный append-only стор (события кампании).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


class JsonlStore:
    """JSONL-справочник с upsert по ключу. Атомарность: tmp + replace."""

    def __init__(self, path: Path, key: str) -> None:
        self.path = Path(path)
        self.key = key
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.path.exists():
            return records
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # тихий пропуск битой строки (конвенция снайпера)
            if isinstance(rec, dict):
                records.append(rec)
        return records

    def load_map(self) -> dict[str, dict[str, Any]]:
        return {str(r.get(self.key, "")): r for r in self.load() if r.get(self.key)}

    def keys(self) -> set[str]:
        return {
            str(r.get(self.key))
            for r in self.load()
            if r.get(self.key) is not None
        }

    def upsert(self, record: dict[str, Any]) -> bool:
        """Вставка/обновление по ключу. True — запись изменена.

        06.11 P1-фикс: обновляет ВСЕ вхождения ключа (могут остаться
        дубли от старых прогонов/batch-кейсов), а не только первое.
        """
        if self.key not in record or record[self.key] is None:
            raise KeyError(f"запись без ключа {self.key!r}")
        records = self.load()
        k = str(record[self.key])
        found = False
        changed = False
        for i, existing in enumerate(records):
            if str(existing.get(self.key)) == k:
                merged = dict(existing)
                merged.update(record)
                records[i] = merged
                found = True
                changed = changed or merged != existing
        if not found:
            records.append(record)
            changed = True
        if changed:
            self._write(records)
        return changed

    def upsert_many(self, records: list[dict[str, Any]]) -> int:
        """Bulk-upsert. Возврат: число изменённых записей. Полный rewrite.

        06.11 P1-фикс: преддедуп батча по ключу (внутрибатчевые дубли
        раньше писались в файл несколькими записями).
        """
        batch: dict[str, dict[str, Any]] = {}
        for record in records:
            if record.get(self.key) is None:
                continue
            k = str(record[self.key])
            if k in batch:
                merged = dict(batch[k])
                merged.update(record)
                batch[k] = merged
            else:
                batch[k] = dict(record)
        existing = self.load_map()
        changed = 0
        result: list[dict[str, Any]] = list(self.load())
        for k, record in batch.items():
            if k in existing:
                merged = dict(existing[k])
                merged.update(record)
                if merged != existing[k]:
                    idx = next(
                        i
                        for i, r in enumerate(result)
                        if str(r.get(self.key)) == k
                    )
                    result[idx] = merged
                    changed += 1
            else:
                result.append(record)
                changed += 1
        if changed:
            self._write(result)
        return changed

    def _write(self, records: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


class CampaignLog:
    """Append-only журнал событий кампании (контакт → событие → заметка)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        contact_key: str,
        event: str,
        note: str = "",
    ) -> dict[str, Any]:
        record = {
            "contact_key": contact_key,
            "event": event,
            "note": note,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def events_for(self, contact_key: str) -> list[dict[str, Any]]:
        return [e for e in self.load() if e.get("contact_key") == contact_key]


def touch_checked_at(record: dict[str, Any]) -> dict[str, Any]:
    """Штамп провенанса — обязателен для каждой записи каналов (ПЛАН §3.3)."""
    record["checked_at"] = datetime.now().isoformat(timespec="seconds")
    return record


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Читатель JSONL research-скриптов (одна копия вместо шести)."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_tsv(path: Path) -> list[dict[str, str]]:
    """companies.tsv: name, group, priority, conferences, github_hint."""
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    header: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        cols = line.split("\t")
        if not header:
            header = [c.strip() for c in cols]
            continue
        rows.append(
            {header[i]: (cols[i].strip() if i < len(cols) else "") for i in range(len(header))}
        )
    return rows
