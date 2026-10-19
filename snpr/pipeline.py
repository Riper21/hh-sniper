from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .cover_letter import LetterDraft
from .filters import PipelineFilter, Verdict
from .scoring import LLMUnavailable, ScoreResult, VacancyScorer

DescriptionSource = Callable[[Mapping[str, Any]], str]


class PipelineAbort(RuntimeError):
    """Прогон прерван: эндпоинт LLM упал подряд на нескольких вакансиях."""


class LetterWriter(Protocol):
    """Любой источник письма: StaticLetterWriter, CoverLetterWriter."""

    def write(
        self,
        vacancy: Mapping[str, Any],
        description: str = "",
        score: ScoreResult | None = None,
    ) -> LetterDraft: ...


@dataclass
class Candidate:
    vacancy: Mapping[str, Any]
    verdict: Verdict | None = None
    score: ScoreResult | None = None
    letter: LetterDraft | None = None
    reject_reasons: list[str] = field(default_factory=list)
    error: str = ""
    deferred: bool = False
    parked: bool = False
    duplicate: bool = False

    @property
    def vacancy_id(self) -> str:
        return str(self.vacancy.get("id") or "")

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if self.deferred:
            return "deferred"
        if self.parked:
            return "parked"
        if self.duplicate:
            return "duplicate"
        if self.reject_reasons:
            return "filtered_out"
        if self.score is None:
            return "unscored"
        if self.letter is None:
            return self.score.verdict
        if self.letter.text:
            return "ready"
        return self.score.verdict


class SnePipeline:
    def __init__(
        self,
        prefilter: PipelineFilter,
        scorer: VacancyScorer | None = None,
        writer: LetterWriter | None = None,
        letter_min_verdict: str = "maybe",
        llm_error_abort: int = 4,
        score_budget: int = 0,
        scorers: Sequence[VacancyScorer] | None = None,
    ):
        self.prefilter = prefilter
        self.scorer = scorer
        self.writer = writer
        self.letter_min_verdict = letter_min_verdict
        self.llm_error_abort = llm_error_abort
        self.score_budget = score_budget
        # 17.10: скоринг волнами. У ChatOpenAI лок держится весь HTTP-запрос,
        # один инстанс = строго последовательные вызовы. Поэтому параллелизм
        # это N независимых скореров (у cli каждый со своим клиентом);
        # один скорер = прежнее последовательное поведение.
        self.scorers: list[VacancyScorer] = list(scorers) if scorers else (
            [scorer] if scorer is not None else []
        )

    def run(
        self,
        vacancies: Iterable[Mapping[str, Any]],
        descriptions: Mapping[str, str] | DescriptionSource | None = None,
        now: Any = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Candidate]:
        """Прогон: дешёвые гейты → карточки+гейты описания → скоринг → письма.

        Порядок кандидатов на выходе — порядок вакансий на входе.
        Скоринг идёт волнами по len(self.scorers) параллельных вызовов;
        ошибки LLM бюджет не съедают (как и раньше), 4 провала подряд
        (в порядке завершения) — PipelineAbort, сторы останутся нетронутыми.
        """
        candidates: list[Candidate] = []
        pending: list[tuple[Candidate, Callable[[str], str]]] = []

        for vacancy in vacancies:
            candidate = Candidate(vacancy=vacancy)
            candidates.append(candidate)

            get_desc: Callable[[str], str]
            if descriptions is None:
                get_desc = lambda _vid: ""
            elif callable(descriptions):
                source: DescriptionSource = descriptions
                # vacancy биндим дефолт-аргументом: замыкание по переменной
                # цикла ловит ПОСЛЕДНЮЮ вакансию (живой баг 17.10: весь пул
                # получил описание последней вакансии — Rambler Data Engineer)
                get_desc = lambda _vid, _vac=vacancy: source(_vac) or ""
            else:
                get_desc = lambda vid: str(descriptions.get(vid, ""))

            verdict = self.prefilter.check(vacancy, now=now)
            if not verdict.passed:
                candidate.verdict = verdict
                candidate.reject_reasons = verdict.reject_reasons
                continue
            pending.append((candidate, get_desc))

        if self.scorers:
            self._run_waves(pending, now=now, on_progress=on_progress)
        else:
            for candidate, get_desc in pending:
                description = get_desc(candidate.vacancy_id)
                full_verdict = self.prefilter.check(
                    candidate.vacancy, description=description, now=now
                )
                candidate.verdict = full_verdict
                if not full_verdict.passed:
                    candidate.reject_reasons = full_verdict.reject_reasons
        return candidates

    def _run_waves(
        self,
        pending: list[tuple[Candidate, Callable[[str], str]]],
        now: Any = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        scored = 0
        attempts = 0
        consecutive_errors = 0
        workers = max(1, len(self.scorers))
        limit = self.score_budget if self.score_budget > 0 else len(pending)
        total = len(pending)
        idx = 0
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            while idx < total and scored < limit:
                # размер волны: свободные слоты бюджета; ошибки бюджет
                # не съедают — следующая волна доберёт хвост
                chunk = pending[idx : idx + min(workers, limit - scored)]
                idx += len(chunk)
                wave: list[tuple[Candidate, str]] = []
                for candidate, get_desc in chunk:
                    # карточки ленивые: только те, кто реально идёт в скоринг
                    description = get_desc(candidate.vacancy_id)
                    full_verdict = self.prefilter.check(
                        candidate.vacancy, description=description, now=now
                    )
                    candidate.verdict = full_verdict
                    if full_verdict.passed:
                        wave.append((candidate, description))
                    else:
                        candidate.reject_reasons = full_verdict.reject_reasons
                if not wave:
                    continue

                futures: dict[Any, tuple[Candidate, str]] = {}
                for i, (candidate, description) in enumerate(wave):
                    scorer = self.scorers[i % workers]
                    futures[
                        executor.submit(
                            scorer.score, candidate.vacancy, description
                        )
                    ] = (candidate, description)

                for fut in as_completed(futures):
                    candidate, description = futures[fut]
                    attempts += 1
                    try:
                        candidate.score = fut.result()
                        scored += 1
                        consecutive_errors = 0
                        if (
                            self.writer is not None
                            and self._wants_letter(candidate.score)
                        ):
                            candidate.letter = self.writer.write(
                                candidate.vacancy,
                                description=description,
                                score=candidate.score,
                            )
                    except LLMUnavailable as ex:
                        candidate.error = str(ex)
                        consecutive_errors += 1
                        if consecutive_errors >= self.llm_error_abort:
                            raise PipelineAbort(
                                f"LLM-эндпоинт не отвечает "
                                f"{consecutive_errors} раз подряд — прогон "
                                f"прерван, сторы не тронуты: {ex}"
                            ) from ex
                    if on_progress is not None:
                        on_progress(attempts, limit)

            # хвост за бюджетом — отложен, вернёмся в следующем прогоне
            for candidate, _get_desc in pending[idx:]:
                candidate.deferred = True
        finally:
            executor.shutdown(wait=True)

    def _wants_letter(self, score: ScoreResult) -> bool:
        order = {"pass": 0, "maybe": 1, "target": 2}
        min_level = order.get(self.letter_min_verdict, 1)
        return order.get(score.verdict, 0) >= min_level
