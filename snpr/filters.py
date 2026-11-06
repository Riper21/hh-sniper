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

# 06.11: токен-гейт названия (title) — вторая ступень префильтра до LLM.
# Паттерн jobhunt/YAMAKA: в названии обязан быть ≥1 include-токен роли
# (короткие — только целым словом, границы (?<![a-zа-яё])token(?![a-zа-яё])),
# стоп-слова не-профессий режут сразу. Данные аудита: 96% LLM-вызовов
# уходит в мусор, 71% мусорных названий не содержат НИ ОДНОГО токена роли,
# 15% содержат стоп-слово профессии.
DEFAULT_TITLE_INCLUDE = [
    "ai", "llm", "ml", "nlp", "ии", "gpt", "rag", "agent", "агент",
    "prompt", "промпт", "machine learning", "machin", "машинн", "обучен",
    "data", "данных", "дата", "data-scientist", "science", "scientist",
    "нейросет", "python", "инженер", "developer", "разработчик",
    "backend", "фронт", "cv", "vision", "верификац", "ops", "engineer",
    "инференс", "интеграц", "mlops", "llmops", "devops", "sre",
    "платформ", "когнитивн",
]
DEFAULT_TITLE_EXCLUDE = [
    "врач", "стоматолог", "медицинск", "hr", "рекрутер", "smm",
    "маркетолог", "бухгалтер", "1с", "юрист", "адвокат", "повар",
    "водитель", "продавец", "кассир", "офис-менеджер", "администратор",
    "менеджер по продажам", "сборщик", "оператор", "кладовщик",
    "клиинг", "уборщ", "сметчик", "прораб", "охранник", " риелтор",
    "риэлтор", "преподавател", "учител", "воспитател", "стоматолог",
]
# regex-трюк jobhunt: «стаж 3+ лет» прямо в названии (API-experience
# не всегда честен: middle+ публикуют с between1And3)
_EXPERIENCE_RE = re.compile(
    r"([3-9]\s?[–—-]\s?\d+\s?(?:лет|года|лет\.)|от\s[5-9]\s?(?:лет|года)|"
    r"[5-9]\+\s?(?:лет|years|года))",
    re.IGNORECASE,
)


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

        # 06.11: title-гейт (вторая ступень префильтра до LLM).
        # include: ≥1 токен роли в названии (короткие — целым словом);
        # exclude: стоп-слова не-профессий; experience: «5+ лет» из текста.
        title = config.get("title", {})
        self.title_enabled = _enabled(title)
        self.title_include: list[str] = [
            str(x).lower()
            for x in (title.get("include") or DEFAULT_TITLE_INCLUDE)
        ]
        self.title_exclude: list[str] = [
            str(x).lower()
            for x in (title.get("exclude") or DEFAULT_TITLE_EXCLUDE)
        ]
        self.title_experience_max = int(title.get("experience_max_years", 0) or 0)

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
        if self.title_enabled:
            verdict.rules.append(self._check_title(vacancy))
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

    def _check_title(self, vacancy: Mapping[str, Any]) -> RuleVerdict:
        """Токен-гейт названия (06.11): include-токен роли обязан быть.

        Мотивация — аудит логов: 96% LLM-скоров = мусор; 71% мусорных
        названий без единого токена роли, 15% со стоп-словом профессии.
        Короткие include-токены (ai/ml/ии/cv) — только целым словом
        (регресс «ai в detail» из outreach-опытa), длинные и многословные —
        подстрокой. exclude — всегда подстрокой (стоп-слова уникальны).
        """
        name = _txt(vacancy.get("name")).lower()
        if not name:
            return RuleVerdict("title", True, "название пустое — решит скоринг")

        for stop in self.title_exclude:
            stop = stop.strip().lower()
            if not stop:
                continue
            # 06.11 фикс: однословные стопы — границей слова СЛЕВА:
            # «водитель» не режет «Руководитель», «учител» — «Деятель».
            # Многословные («менеджер по продажам») — подстрокой.
            if " " in stop:
                if stop in name:
                    return RuleVerdict(
                        "title", False, f"стоп-слово «{stop}» в названии"
                    )
            elif re.search(rf"(?<![a-zа-яё0-9]){re.escape(stop)}", name):
                return RuleVerdict(
                    "title", False, f"стоп-слово «{stop}» в названии"
                )

        if self.title_experience_max > 0:
            m = _EXPERIENCE_RE.search(name)
            if m:
                return RuleVerdict(
                    "title",
                    False,
                    f"стаж в названии: «{m.group(0)}» > {self.title_experience_max}",
                )

        for token in self.title_include:
            token = token.strip().lower()
            if not token:
                continue
            if " " in token or len(token) > 4:
                if token in name:
                    return RuleVerdict("title", True, f"токен «{token}»")
            else:
                # короткий токен — граница слова (кириллица+латиница)
                pattern = rf"(?<![a-zа-яё0-9]){re.escape(token)}(?![a-zа-яё0-9])"
                if re.search(pattern, name):
                    return RuleVerdict("title", True, f"токен «{token}»")
        return RuleVerdict(
            "title", False, "нет include-токена роли в названии"
        )

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
