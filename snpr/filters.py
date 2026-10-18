from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

NET_RATE = 0.87
CURRENCY_RATE = {"RUR": 1.0, "RUB": 1.0, "USD": 95.0, "EUR": 105.0, "KZT": 0.19}

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")
DEFAULT_BLACKLIST_PATH = Path(__file__).resolve().parent.parent / "data" / "blacklist.txt"


def _norm_name(name: str) -> str:
    """Нормализованное имя работодателя для сопоставления с блэклистом."""
    return re.sub(r"\s+", " ", name.strip().strip('«»"\'').lower())


def load_blacklist(
    path: Path | str | None,
) -> tuple[set[str], set[str], str]:
    """Разбор файла блэклиста: (ids, имена, причина-если-файла-нет).

    Формат — строка на работодателя: числовой id, имя или «id имя».
    Пустые строки и # — комментарии. Нет файла = нет гейта.
    """
    if path is None:
        return set(), set(), "файл не указан"
    p = Path(path)
    if not p.exists():
        return set(), set(), "файла нет"
    ids: set[str] = set()
    names: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        first = line.split()[0]
        if first.isdigit():
            ids.add(first)
            rest = line[len(first):].strip()
            if rest:
                names.add(_norm_name(rest))
        else:
            names.add(_norm_name(line))
    return ids, names, ""


def resolve_blacklist_path(file_value: object) -> Path:
    """Путь файла блэклиста: абсолютный как есть, относительный — от
    корня проекта, пусто — data/blacklist.txt по умолчанию."""
    raw = str(file_value or "").strip()
    path = Path(raw) if raw else DEFAULT_BLACKLIST_PATH
    if not path.is_absolute():
        path = DEFAULT_BLACKLIST_PATH.parent.parent / path
    return path


def _enabled(section: Mapping[str, Any] | None) -> bool:
    """Гейт включён, пока явно не выключен (enabled = false в конфиге)."""
    return bool((section or {}).get("enabled", True))


@dataclass
class RuleVerdict:
    rule: str
    passed: bool
    reason: str = ""


@dataclass
class Verdict:
    vacancy_id: str
    name: str
    employer: str
    passed: bool
    rules: list[RuleVerdict] = field(default_factory=list)

    @property
    def reject_reasons(self) -> list[str]:
        return [f"{v.rule}: {v.reason}" for v in self.rules if not v.passed]


def _txt(value: Any) -> str:
    return str(value or "").strip()


class PipelineFilter:
    def __init__(self, config: Mapping[str, Any] | None = None):
        if config is None:
            with DEFAULT_CONFIG_PATH.open("rb") as fh:
                config = tomllib.load(fh)

        self.floor_net_rub: int = int(config.get("floor_net_rub", 300_000))
        self.max_age_hours: int = int(config.get("max_age_hours", 0))
        self.min_from_ratio: float = float(config.get("min_from_ratio", 0.5))

        grade = config.get("grade", {})
        self.grade_enabled = _enabled(grade)
        self.grade_pat = re.compile(
            _txt(grade.get("exclude_pattern")), re.IGNORECASE
        )
        self.grade_experience: set[str] = set(
            grade.get("exclude_experience", [])
        )

        vendor = config.get("vendor", {})
        self.vendor_enabled = _enabled(vendor)
        self.vendor_employers = re.compile(
            _txt(vendor.get("employers")), re.IGNORECASE
        )
        self.vendor_keywords = re.compile(
            _txt(vendor.get("keywords")), re.IGNORECASE
        )

        gov = config.get("gov", {})
        self.gov_enabled = _enabled(gov)
        self.gov_employers = re.compile(
            _txt(gov.get("employers")), re.IGNORECASE
        )
        self.gov_keywords = re.compile(_txt(gov.get("keywords")), re.IGNORECASE)

        geo = config.get("geo", {})
        self.geo_enabled = _enabled(geo)
        self.remote_ok: bool = bool(geo.get("remote_ok", True))
        self.areas_ok: set[str] = {
            str(x) for x in geo.get("areas", ["1261"])
        }

        # 18.10: локальный блэклист работодателей (data/blacklist.txt).
        # Нет файла = гейт пуст = вакансии проходят (no-op).
        blacklist = config.get("blacklist", {})
        self.blacklist_enabled = _enabled(blacklist)
        self.blacklist_path = resolve_blacklist_path(blacklist.get("file"))
        self.blacklist_ids: set[str] = set()
        self.blacklist_names: set[str] = set()
        if self.blacklist_enabled:
            self.blacklist_ids, self.blacklist_names, _ = load_blacklist(
                self.blacklist_path
            )

    def check(
        self,
        vacancy: Mapping[str, Any],
        description: str | None = None,
        now: datetime | None = None,
    ) -> Verdict:
        vid = _txt(vacancy.get("id"))
        vname = _txt(vacancy.get("name"))
        employer = _txt((vacancy.get("employer") or {}).get("name"))
        verdict = Verdict(vid, vname, employer, True)

        # 17.10, решение пользователя: vendor/grade/gov/geo выключены —
        # fit по этим осям решает LLM-скоринг. Гейты остаются в коде и
        # включаются обратно enabled = true в config.toml.
        if self.grade_enabled:
            verdict.rules.append(self._check_grade(vacancy))
        if self.vendor_enabled:
            verdict.rules.append(self._check_vendor(employer, description))
        if self.gov_enabled:
            verdict.rules.append(self._check_gov(employer, description))
        if self.blacklist_ids or self.blacklist_names:
            verdict.rules.append(self._check_blacklist(vacancy))
        verdict.rules.append(self._check_salary(vacancy))
        if self.geo_enabled:
            verdict.rules.append(self._check_geo(vacancy))
        if self.max_age_hours > 0:
            verdict.rules.append(
                self._check_freshness(vacancy, now or datetime.now(timezone.utc))
            )

        verdict.passed = all(rule.passed for rule in verdict.rules)
        return verdict

    def _check_grade(self, vacancy: Mapping[str, Any]) -> RuleVerdict:
        name = _txt(vacancy.get("name"))
        if match := self.grade_pat.search(name):
            return RuleVerdict(
                "grade", False, f"название: «{match.group(0)}»"
            )
        if vacancy.get("internship"):
            return RuleVerdict("grade", False, "internship = true")
        experience = _txt((vacancy.get("experience") or {}).get("id"))
        if experience in self.grade_experience:
            return RuleVerdict("grade", False, f"experience: {experience}")
        return RuleVerdict("grade", True)

    def _check_vendor(
        self, employer: str, description: str | None
    ) -> RuleVerdict:
        if match := self.vendor_employers.search(employer):
            return RuleVerdict("vendor", False, f"работодатель: {employer}")
        if description and (
            match := self.vendor_keywords.search(description)
        ):
            return RuleVerdict(
                "vendor", False, f"описание: «{match.group(0)}»"
            )
        return RuleVerdict("vendor", True)

    def _check_blacklist(self, vacancy: Mapping[str, Any]) -> RuleVerdict:
        employer = vacancy.get("employer") or {}
        emp_id = _txt(employer.get("id"))
        name = _txt(employer.get("name"))
        if emp_id and emp_id in self.blacklist_ids:
            return RuleVerdict(
                "blacklist", False, f"работодатель id {emp_id}: {name}"
            )
        if name and _norm_name(name) in self.blacklist_names:
            return RuleVerdict("blacklist", False, f"работодатель: {name}")
        return RuleVerdict("blacklist", True)

    def _check_gov(self, employer: str, description: str | None) -> RuleVerdict:
        if match := self.gov_employers.search(employer):
            return RuleVerdict("gov", False, f"работодатель: {employer}")
        if description and (match := self.gov_keywords.search(description)):
            return RuleVerdict("gov", False, f"описание: «{match.group(0)}»")
        return RuleVerdict("gov", True)

    def _check_freshness(
        self, vacancy: Mapping[str, Any], now: datetime
    ) -> RuleVerdict:
        published = _txt(vacancy.get("published_at"))
        if not published:
            return RuleVerdict("freshness", True, "нет даты публикации")
        try:
            published_dt = datetime.fromisoformat(published)
        except ValueError:
            return RuleVerdict("freshness", True, f"дата не разобрана: {published}")
        if published_dt.tzinfo is None:
            published_dt = published_dt.replace(tzinfo=timezone.utc)
        age_hours = (now - published_dt).total_seconds() / 3600
        if age_hours < 0:
            return RuleVerdict("freshness", True, "дата в будущем")
        if age_hours > self.max_age_hours:
            return RuleVerdict(
                "freshness",
                False,
                f"возраст {age_hours:.1f}ч > {self.max_age_hours}ч",
            )
        return RuleVerdict("freshness", True, f"возраст {age_hours:.1f}ч")

    def _check_salary(self, vacancy: Mapping[str, Any]) -> RuleVerdict:
        salary = vacancy.get("salary") or {}
        value_from = salary.get("from")
        value_to = salary.get("to")
        if value_from is None and value_to is None:
            return RuleVerdict("salary", True, "вилка не указана — решит скоринг")

        currency = _txt(salary.get("currency", "RUR")) or "RUR"
        rate = CURRENCY_RATE.get(currency)
        if rate is None:
            return RuleVerdict(
                "salary", False, f"неизвестная валюта: {currency}"
            )
        gross = salary.get("gross", True)

        def to_net(value: int) -> float:
            net = value * rate
            return net * NET_RATE if gross else net

        if value_to is not None:
            net_to = to_net(value_to)
            if net_to < self.floor_net_rub:
                return RuleVerdict(
                    "salary",
                    False,
                    f"верх вилки ~{net_to / 1000:.0f}К net < floor {self.floor_net_rub / 1000:.0f}К",
                )
            return RuleVerdict("salary", True, f"верх ~{net_to / 1000:.0f}К net")

        net_from = to_net(value_from)
        if net_from < self.floor_net_rub * self.min_from_ratio:
            return RuleVerdict(
                "salary",
                False,
                f"низ вилки ~{net_from / 1000:.0f}К net < {self.min_from_ratio:.0%} floor",
            )
        return RuleVerdict(
            "salary", True, f"низ ~{net_from / 1000:.0f}К net, верха нет"
        )

    def _check_geo(self, vacancy: Mapping[str, Any]) -> RuleVerdict:
        if self.remote_ok:
            work_formats = " ".join(
                _txt(x.get("id")) for x in vacancy.get("work_format") or []
            )
            schedule = _txt((vacancy.get("schedule") or {}).get("id"))
            if "REMOTE" in work_formats or schedule == "remote":
                return RuleVerdict("geo", True, "remote")
            if "HYBRID" in work_formats or schedule == "flexible":
                area = _txt((vacancy.get("area") or {}).get("id"))
                if area in self.areas_ok:
                    return RuleVerdict("geo", True, "hybrid в целевом городе")
        else:
            work_formats = ""

        area_id = _txt((vacancy.get("area") or {}).get("id"))
        area_name = _txt((vacancy.get("area") or {}).get("name"))
        if area_id in self.areas_ok:
            return RuleVerdict("geo", True, f"{area_name}")
        return RuleVerdict(
            "geo", False, f"{area_name or area_id} без remote-формата"
        )
