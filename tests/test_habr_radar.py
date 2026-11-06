"""Хабр-радар: разбор RSS, префильтры до LLM, прогон со сторами.

Живой Хабр не трогается: _http_get подменяется заготовками XML, скорер —
фейком с готовыми вердиктами, Telegram — рекордером send_message. Сторы —
tmp-пути через RadarPaths (как в такте, только в tmp).
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from snpr import habr_radar, notify_tg
from snpr.habr_radar import (
    RadarPaths,
    extract_tags,
    fetch_rss_items,
    map_item,
    maybe_habr_radar,
    parse_title,
    prefilter,
    run_radar,
)
from snpr.scoring import LLMUnavailable, ScoreResult

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _paths(tmp_path):
    return RadarPaths(
        seen=tmp_path / "seen.jsonl",
        scores=tmp_path / "scores.jsonl",
        applied=tmp_path / "applied.jsonl",
        parked=tmp_path / "parked.jsonl",
        marker=tmp_path / "habr_last.txt",
    )


def _item_xml(
    guid,
    title,
    author="Компания",
    pub=None,  # 06.11: динамически «3 часа назад» (дата-дрейф ломал тесты)
    desc="Компания «Компания» ищет специалиста на вакансию «Вакансия». "
    "Москва (Россия). Полный рабочий день. Можно удалённо. "
    "Требуемые навыки: #middle, #Python, #LLM.",
):
    if pub is None:
        from email.utils import format_datetime as _fmt

        pub = _fmt(
            (NOW - timedelta(hours=3)).astimezone(timezone(timedelta(hours=3)))
        )
    return (
        "<item>"
        f"<title>{title}</title>"
        f"<author>{author}</author>"
        f"<pubDate>{pub}</pubDate>"
        f"<link>https://career.habr.com/vacancies/{guid}</link>"
        f"<guid>{guid}</guid>"
        f"<description>{desc}</description>"
        "</item>"
    )


def _feed(*items):
    body = "".join(items)
    return f'<?xml version="1.0" encoding="utf-8"?><rss><channel>{body}</channel></rss>'


class FakeScorer:
    """Скорер с заготовленными вердиктами по id (или общей ошибкой LLM)."""

    def __init__(self, results=None, error=None):
        self.results = results or {}
        self.error = error

    def score(self, vacancy, description=""):
        if self.error:
            raise LLMUnavailable(self.error)
        vid = str(vacancy.get("id") or "")
        verdict = self.results.get(vid, {"score": 20, "verdict": "pass"})
        return ScoreResult(
            vacancy_id=vid,
            score=int(verdict.get("score", 20)),
            verdict=str(verdict.get("verdict", "pass")),
            reasons=list(verdict.get("reasons", ["причина-раз", "причина-два"])),
        )


# --- разбор фида ---------------------------------------------------------------


class TestParseTitle:
    def test_name_and_salary_from(self):
        name, salary = parse_title(
            "Требуется «Python backend» (Москва, от 230 000 ₽)"
        )
        assert name == "Python backend"
        assert salary == {"from": 230000, "to": None, "currency": "RUR", "gross": True}

    def test_salary_range_usd(self):
        _, salary = parse_title("Требуется «Support» (от 800 до 1 120 $)")
        assert salary["from"] == 800
        assert salary["to"] == 1120
        assert salary["currency"] == "USD"

    def test_salary_to_only(self):
        _, salary = parse_title("Требуется «Ops» (до 120 $)")
        assert salary["from"] is None
        assert salary["to"] == 120
        assert salary["currency"] == "USD"

    def test_no_salary(self):
        name, salary = parse_title("Требуется «DevOps инженер» (Москва)")
        assert name == "DevOps инженер"
        assert salary is None

    def test_name_without_guillemets(self):
        name, _ = parse_title("Требуется NLP-разработчик (Москва)")
        assert name == "NLP-разработчик"


class TestExtractTags:
    def test_tags_lowercased(self):
        tags = extract_tags(
            "Требуемые навыки: #middle, #REST, #ApacheSpark, #LLM."
        )
        assert tags == ["middle", "rest", "apachespark", "llm"]

    def test_no_tags(self):
        assert extract_tags("Компания X ищет специалиста.") == []


class TestMapItem:
    def test_hh_shaped(self):
        item = {
            "guid": "10001",
            "title": "Требуется «ML инженер» (от 250 000 ₽)",
            "author": "ТестоКомп",
            "pubDate": "Tue, 20 Oct 2026 21:18:23 +0300",
            "link": "https://career.habr.com/vacancies/10001",
            "description": "Компания «ТестоКом» ищет. Москва (Россия). "
            "Можно удалённо. Требуемые навыки: #senior, #Python.",
        }
        vacancy = map_item(item)
        assert vacancy["id"] == "habr:10001"
        assert vacancy["name"] == "ML инженер"
        assert vacancy["employer"]["name"] == "ТестоКомп"
        assert vacancy["salary"]["from"] == 250000
        assert vacancy["published_at"].startswith("2026-10-20T")
        assert vacancy["apply_url"].endswith("/10001")
        assert vacancy["source"] == "habr"
        assert [s["name"] for s in vacancy["key_skills"]] == ["senior", "python"]

    def test_remote_work_format(self):
        vacancy = map_item(
            {
                "guid": "1",
                "title": "Требуется «X»",
                "description": "Можно удалённо. #middle.",
            }
        )
        assert vacancy["work_format"] == [{"id": "REMOTE"}]

    def test_office_no_remote(self):
        vacancy = map_item(
            {
                "guid": "2",
                "title": "Требуется «Y» (Москва)",
                "description": "Компания «ТестоКом» ищет специалиста на "
                "вакансию «Y». Москва (Россия). Офис. #middle.",
            }
        )
        assert vacancy["work_format"] == []
        assert vacancy["area"]["name"] == "Москва"


class TestFetchRss:
    def test_merge_dedup_and_order(self, monkeypatch):
        feeds = {
            "LLM": _feed(_item_xml("1", "Требуется «A»", pub="Tue, 21 Oct 2026 11:00:00 +0300")),
            "Python": _feed(
                _item_xml("1", "Требуется «A»", pub="Tue, 21 Oct 2026 11:00:00 +0300"),
                _item_xml("2", "Требуется «B»", pub="Tue, 21 Oct 2026 09:00:00 +0300"),
            ),
        }

        def fake_get(url, timeout=30.0):
            query = url.split("q=")[-1]
            return feeds[query]

        monkeypatch.setattr(habr_radar, "_http_get", fake_get)
        items = fetch_rss_items(["LLM", "Python"])
        assert [i["guid"] for i in items] == ["1", "2"]

    def test_partial_failure_ok(self, monkeypatch):
        feeds = {"LLM": _feed(_item_xml("1", "Требуется «A»"))}

        def fake_get(url, timeout=30.0):
            query = url.split("q=")[-1]
            if query not in feeds:
                raise RuntimeError("network down")
            return feeds[query]

        monkeypatch.setattr(habr_radar, "_http_get", fake_get)
        items = fetch_rss_items(["LLM", "Python"])
        assert [i["guid"] for i in items] == ["1"]

    def test_total_failure_raises(self, monkeypatch):
        def fake_get(url, timeout=30.0):
            raise RuntimeError("network down")

        monkeypatch.setattr(habr_radar, "_http_get", fake_get)
        with pytest.raises(RuntimeError):
            fetch_rss_items(["LLM"])


# --- префильтры ------------------------------------------------------------------


def _vac(guid, name="ML инженер", employer="Компания", tags=None, published=None):
    return {
        "id": f"habr:{guid}",
        "name": name,
        "employer": {"id": "", "name": employer},
        "salary": {},
        "area": {"id": "", "name": "Москва"},
        "published_at": published
        or (NOW - timedelta(hours=1)).isoformat(),
        "key_skills": [{"name": tag} for tag in (tags or ["middle", "python"])],
        "work_format": [],
        "archived": False,
        "apply_url": f"https://career.habr.com/vacancies/{guid}",
        "source": "habr",
    }


class TestPrefilter:
    def test_seen_skipped(self):
        vac = _vac("1")
        accepted, stats = prefilter([vac], {"habr:1"}, set())
        assert accepted == []
        assert stats["seen"] == 1

    def test_cross_dup_skipped(self):
        vac = _vac("1", name="ml инженер", employer="компания")
        accepted, stats = prefilter([vac], set(), {("ml инженер", "компания")})
        assert accepted == []
        assert stats["кросс-дедуп"] == 1

    def test_junior_tag_skipped(self):
        vac = _vac("1", tags=["junior", "python"])
        accepted, stats = prefilter([vac], set(), set())
        assert accepted == []
        assert stats["грейд"] == 1

    def test_stale_skipped(self):
        vac = _vac("1", published=(NOW - timedelta(hours=30)).isoformat())
        accepted, stats = prefilter(
            [vac], set(), set(), {"max_age_hours": 26.0}, now=NOW
        )
        assert accepted == []
        assert stats["старьё"] == 1

    def test_budget_caps(self):
        vacs = [_vac(str(i)) for i in range(5)]
        accepted, stats = prefilter(vacs, set(), set(), budget=3)
        assert len(accepted) == 3
        assert stats["бюджет"] == 2

    def test_accepted_passes_all_gates(self):
        vac = _vac("1")
        accepted, _ = prefilter([vac], set(), set(), now=NOW)
        assert accepted == [vac]


# --- прогон радара -----------------------------------------------------------------


CONFIG = {
    "floor_net_rub": 0,
    "grade": {"enabled": False},
    "vendor": {"enabled": False},
    "gov": {"enabled": False},
    "habr": {"enabled": True},
}


def _run_feed(monkeypatch, tmp_path, items, results=None, config=None):
    paths = _paths(tmp_path)
    xml = _feed(*items)

    def fake_get(url, timeout=30.0):
        return xml

    monkeypatch.setattr(habr_radar, "_http_get", fake_get)
    cards: list[str] = []
    monkeypatch.setattr(notify_tg, "tg_enabled", lambda cfg: True)
    monkeypatch.setattr(
        notify_tg, "send_message", lambda text, keyboard=None, paths=None: cards.append(text)
    )
    config = config or CONFIG
    result = run_radar(
        config,
        [FakeScorer(results or {})],
        paths,
        dry_run=False,
    )
    return paths, cards, result


class TestRunRadar:
    def test_maybe_card_seen_and_scores(self, monkeypatch, tmp_path):
        items = [
            _item_xml(
                "100",
                "Требуется «LLM инженер» (от 250 000 ₽)",
                author="НейроКомп",
            )
        ]
        paths, cards, result = _run_feed(
            monkeypatch,
            tmp_path,
            items,
            results={"habr:100": {"score": 62, "verdict": "maybe"}},
        )
        assert result == 1
        assert len(cards) == 1
        assert "ХАБР-РАДАР" in cards[0]
        assert "62" in cards[0]
        assert "https://career.habr.com/vacancies/100" in cards[0]
        # seen и scores записаны, маркер потроган
        seen = [json.loads(l) for l in paths.seen.read_text(encoding="utf-8").splitlines()]
        assert seen[0]["vacancy_id"] == "habr:100"
        scores = [json.loads(l) for l in paths.scores.read_text(encoding="utf-8").splitlines()]
        assert scores[0]["score"] == 62
        assert scores[0]["employer"] == "НейроКомп"
        assert paths.marker.exists()

    def test_pass_no_card(self, monkeypatch, tmp_path):
        paths, cards, result = _run_feed(
            monkeypatch,
            tmp_path,
            [_item_xml("101", "Требуется «Ops» (Москва)")],
            results={"habr:101": {"score": 20, "verdict": "pass"}},
        )
        assert result == 0
        assert cards == []
        seen = [json.loads(l) for l in paths.seen.read_text(encoding="utf-8").splitlines()]
        assert seen[0]["status"] == "pass"

    def test_dry_run_writes_nothing(self, monkeypatch, tmp_path):
        paths = _paths(tmp_path)
        xml = _feed(_item_xml("102", "Требуется «X» (от 250 000 ₽)"))

        def fake_get(url, timeout=30.0):
            return xml

        monkeypatch.setattr(habr_radar, "_http_get", fake_get)
        cards: list[str] = []
        monkeypatch.setattr(notify_tg, "tg_enabled", lambda cfg: True)
        monkeypatch.setattr(
            notify_tg, "send_message", lambda text, keyboard=None, paths=None: cards.append(text)
        )
        result = run_radar(
            CONFIG,
            [FakeScorer({"habr:102": {"score": 50, "verdict": "maybe"}})],
            paths,
            dry_run=True,
        )
        assert result == 1
        assert cards == []
        assert not paths.seen.exists()
        assert not paths.scores.exists()
        assert not paths.marker.exists()

    def test_salary_floor_filters_out(self, monkeypatch, tmp_path):
        # вилка в долларах сильно ниже floor — гейт конвейера режет до LLM
        paths, cards, result = _run_feed(
            monkeypatch,
            tmp_path,
            [_item_xml("103", "Требуется «Cheap» (от 100 до 200 $)")],
            results={"habr:103": {"score": 90, "verdict": "target"}},
            config={
                "floor_net_rub": 200000,
                "grade": {"enabled": False},
                "vendor": {"enabled": False},
                "gov": {"enabled": False},
                "habr": {"enabled": True},
            },
        )
        assert result == 0
        assert cards == []
        seen = [json.loads(l) for l in paths.seen.read_text(encoding="utf-8").splitlines()]
        assert seen[0]["status"] == "filtered_out"
        assert not paths.scores.exists()

    def test_cross_dedup_vs_scores_history(self, monkeypatch, tmp_path):
        paths = _paths(tmp_path)
        # hh-история: такая же вакансия уже проскорена снайпером —
        # радар её даже не скорит и ничего не пишет (hh уже ведёт её)
        paths.scores.parent.mkdir(parents=True, exist_ok=True)
        paths.scores.write_text(
            json.dumps(
                {
                    "vacancy_id": "123456",
                    "name": "llm инженер",
                    "employer": "нейрокомп",
                    "score": 80,
                    "verdict": "target",
                    "ts": 1,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        xml = _feed(
            _item_xml("104", "Требуется «LLM инженер»", author="НейроКомп")
        )

        def fake_get(url, timeout=30.0):
            return xml

        monkeypatch.setattr(habr_radar, "_http_get", fake_get)
        cards: list[str] = []
        monkeypatch.setattr(notify_tg, "send_message", lambda text, keyboard=None, paths=None: cards.append(text))
        result = run_radar(
            CONFIG,
            [FakeScorer({"habr:104": {"score": 70, "verdict": "maybe"}})],
            paths,
        )
        assert result == 0
        assert cards == []
        assert not paths.seen.exists()

    def test_llm_abort_keeps_stores(self, monkeypatch, tmp_path):
        paths = _paths(tmp_path)
        items = [_item_xml(str(i), f"Требуется «V{i}»") for i in range(5)]
        xml = _feed(*items)

        def fake_get(url, timeout=30.0):
            return xml

        monkeypatch.setattr(habr_radar, "_http_get", fake_get)
        result = run_radar(
            {
                "floor_net_rub": 0,
                "grade": {"enabled": False},
                "vendor": {"enabled": False},
                "gov": {"enabled": False},
                "habr": {"enabled": True, "max_per_run": 5},
            },
            [FakeScorer(error="endpoint down")],
            paths,
        )
        assert result == 3
        assert not paths.seen.exists()
        assert not paths.scores.exists()
        assert not paths.marker.exists()


class TestMaybeHabrRadar:
    def test_disabled_no_network(self, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise AssertionError("сеть не должна трогаться")

        monkeypatch.setattr(habr_radar, "fetch_rss_items", boom)
        import argparse

        args = argparse.Namespace(seen_store=str(tmp_path / "seen.jsonl"))
        assert maybe_habr_radar({"habr": {"enabled": False}}, [FakeScorer()], args) == 0

    def test_interval_not_due(self, monkeypatch, tmp_path):
        import argparse

        args = argparse.Namespace(seen_store=str(tmp_path / "seen.jsonl"))
        marker = tmp_path / "habr_last.txt"
        marker.write_text(f"{datetime.now(timezone.utc).timestamp():.0f}\n", encoding="utf-8")

        def boom(*a, **k):
            raise AssertionError("фид не должен забираться")

        monkeypatch.setattr(habr_radar, "fetch_rss_items", boom)
        assert maybe_habr_radar(
            {"habr": {"enabled": True, "interval_minutes": 15.0}},
            [FakeScorer()],
            args,
        ) == 0

    def test_no_scorers_skips(self, monkeypatch, tmp_path):
        import argparse

        args = argparse.Namespace(seen_store=str(tmp_path / "seen.jsonl"))
        assert maybe_habr_radar({"habr": {"enabled": True}}, None, args) == 0
