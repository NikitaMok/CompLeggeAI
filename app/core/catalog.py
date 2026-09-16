"""Каталог источников из config/providers.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.core.config import PROJECT_ROOT, Settings, get_settings

CATALOG_PATH = PROJECT_ROOT / "config" / "providers.yaml"


class LlmSettings(BaseModel):
    provider: str
    model: str
    base_url: str


class Source(BaseModel):
    id: str
    enabled: bool
    # free — работает без ключа; paid — ключ клиента обязателен;
    # self_hosted — сервис поднимается рядом, ключ не нужен, нужен адрес.
    tier: str
    what: str = ""
    base_url: str | None = None
    env_key: str | None = None
    module: str | None = None
    # Бесплатный источник, который без ключа всё равно не отвечает
    # (OpenCorporates отдаёт 401 анонимному запросу).
    required_key: bool = False


class Catalog(BaseModel):
    llm: LlmSettings
    wallet: list[Source] = Field(default_factory=list)
    counterparty: list[Source] = Field(default_factory=list)
    sanctions: list[Source] = Field(default_factory=list)

    def enabled(self, group: str, *, tier: str | None = None) -> list[Source]:
        items = getattr(self, group)
        return [
            item
            for item in items
            if item.enabled and (tier is None or item.tier == tier)
        ]

    def source(self, group: str, source_id: str) -> Source | None:
        for item in getattr(self, group):
            if item.id == source_id:
                return item
        return None

    def secret(self, source: Source, settings: Settings | None = None) -> str:
        """Ключ из `.env` по имени `env_key`. Пустая строка, если ключа нет."""
        if not source.env_key:
            return ""
        cfg = settings or get_settings()
        return str(getattr(cfg, source.env_key.lower(), "") or "").strip()

    def ready(self, group: str) -> list[Source]:
        """Включённые источники, у которых для платного тарифа задан ключ."""
        cfg = get_settings()
        ready: list[Source] = []
        for item in self.enabled(group):
            if item.tier == "paid" and not self.secret(item, cfg):
                continue
            ready.append(item)
        return ready

    def uses(self, group: str, source_id: str) -> bool:
        return any(item.id == source_id for item in self.ready(group))


def _read_catalog(path: Path) -> Catalog:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Catalog.model_validate(payload)


@lru_cache
def load_catalog(path: Path | None = None) -> Catalog:
    catalog = _read_catalog(path or CATALOG_PATH)
    settings = get_settings()
    # `.env` перекрывает yaml: так меняют модель, не трогая каталог.
    llm = catalog.llm.model_copy(
        update={
            "provider": settings.llm_provider or catalog.llm.provider,
            "model": settings.ollama_model or catalog.llm.model,
            "base_url": str(settings.ollama_base_url or catalog.llm.base_url),
        }
    )
    # Адрес сервера скрининга перекрывается переменной окружения: в Docker
    # он виден под другим именем, чем на хосте, а yaml один на оба случая.
    sanctions = list(catalog.sanctions)
    if settings.opensanctions_base_url:
        sanctions = [
            item.model_copy(update={"base_url": str(settings.opensanctions_base_url)})
            if item.id == "opensanctions"
            else item
            for item in sanctions
        ]
    return catalog.model_copy(update={"llm": llm, "sanctions": sanctions})
