"""Outreach — прямые контакты целевых компаний (06.11).

Порт изолированного прогона research/outreach_mvp (03-05.11, 5 итераций)
по ПЛАН_ИНТЕГРАЦИИ_OUTREACH.md. Вердикты каналов — АУДИТ_КАНАЛА.md
(archive): GitHub-коммиты и карьер-сайты — рабочие основные,
hh — триггер приоритизации, мост email→Habr→TG закрыт.

Слои:
- http/store/text/rank/config — фундамент (клиент, сторы, утилиты);
- github/habr/hh_source/career/ — адаптеры каналов;
- base — единая база outreach.jsonl; scoring — LLM-скоринг вакансий;
- trigger — тихий хук тика снайпера (подсказки контактов);
- webui/ui_sidecar/job_control/settings — UI-слой (localhost);
- cli — подкоманда `python -m snpr outreach …`;
- pipeline/migrate/report — оркестрация и отчёты.

Данные: data/outreach/ (gitignored). Роль конфигурируема:
[outreach].role_keywords (пусто = ML-дефолт).
"""

from snpr.outreach.config import outreach_config
from snpr.outreach.store import CampaignLog, JsonlStore

__all__ = ["CampaignLog", "JsonlStore", "outreach_config"]
