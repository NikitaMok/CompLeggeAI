"""Диагностика доступности источников.

Скрипт нужен при установке в корпоративной сети: пока источники не опрошены
с той самой машины, неизвестно, что увидит юрист — карточку организации
или «сверка не выполнена». Тесты проверяют, что скрипт не выдаёт молчание
источника за успех и что он зовёт настоящие функции прогона.
"""

from __future__ import annotations

import json

import pytest

from app.aml.score import AddressSnapshot
from app.counterparty.models import SourceHit, skipped
from scripts import check_sources

pytestmark = pytest.mark.no_pipeline_stub


def _answered_party(source_id: str) -> SourceHit:
    return SourceHit(
        source_id=source_id,
        performed=True,
        found=True,
        legal_name="ПАО «Пример»",
        detail="ЕГРЮЛ: ПАО «Пример», статус: действует",
    )


class TestVerdictOnSilentSources:
    def test_all_silent_gives_exit_code_one(self, monkeypatch, capsys):
        monkeypatch.setattr(
            check_sources,
            "fetch_snapshot",
            lambda address, client=None: AddressSnapshot(
                address=address.value, network=address.network, error="нет соединения с источником"
            ),
        )
        monkeypatch.setattr(
            check_sources,
            "lookup_free_sources",
            lambda inn, name, client=None, **_: (skipped("egrul", "нет соединения с источником"),),
        )

        code = check_sources.main(["--skip-model"])
        out = capsys.readouterr()

        assert code == 1
        assert "НЕ отвечает" in out.out
        assert "сверка не выполнена" in out.err

    def test_answering_sources_give_exit_code_zero(self, monkeypatch, capsys):
        from datetime import datetime, timezone

        monkeypatch.setattr(
            check_sources,
            "fetch_snapshot",
            lambda address, client=None: AddressSnapshot(
                address=address.value,
                network=address.network,
                created_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
                tx_count=412,
            ),
        )
        monkeypatch.setattr(
            check_sources,
            "lookup_free_sources",
            lambda inn, name, client=None, **_: (_answered_party("egrul"),),
        )

        code = check_sources.main(["--skip-model"])
        out = capsys.readouterr()

        assert code == 0
        assert "отвечает" in out.out
        assert "операций 412" in out.out

    def test_one_answering_source_is_enough_for_the_group(self, monkeypatch):
        from datetime import datetime, timezone

        monkeypatch.setattr(
            check_sources,
            "fetch_snapshot",
            lambda address, client=None: AddressSnapshot(
                address=address.value,
                network=address.network,
                created_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
            ),
        )
        monkeypatch.setattr(
            check_sources,
            "lookup_free_sources",
            lambda inn, name, client=None, **_: (
                skipped("egrul", "нет соединения с источником"),
                _answered_party("rusprofile"),
            ),
        )

        assert check_sources.main(["--skip-model"]) == 0


class TestJsonOutput:
    def test_json_is_machine_readable(self, monkeypatch, capsys):
        monkeypatch.setattr(
            check_sources,
            "fetch_snapshot",
            lambda address, client=None: AddressSnapshot(
                address=address.value, network=address.network, error="нет соединения с источником"
            ),
        )
        monkeypatch.setattr(
            check_sources,
            "lookup_free_sources",
            lambda inn, name, client=None, **_: (skipped("gleif", "нет соединения с источником"),),
        )

        check_sources.main(["--skip-model", "--json"])
        rows = json.loads(capsys.readouterr().out)

        assert rows
        assert all({"group", "source", "answered", "detail", "seconds"} <= set(r) for r in rows)
        assert all(r["answered"] is False for r in rows)


class TestNoContractLeaves:
    def test_only_identifiers_are_passed_outwards(self, monkeypatch):
        """Наружу уходят адрес, ИНН и наименование — и ничего больше."""
        seen: list[tuple] = []

        monkeypatch.setattr(
            check_sources,
            "fetch_snapshot",
            lambda address, client=None: seen.append(("wallet", address.value))
            or AddressSnapshot(address=address.value, network=address.network, error="нет данных"),
        )
        monkeypatch.setattr(
            check_sources,
            "lookup_free_sources",
            lambda inn, name, client=None, **kw: seen.append(("party", inn, name))
            or (skipped("egrul", "нет данных"),),
        )

        check_sources.main(["--skip-model", "--address", "TTest", "--inn", "7707083893",
                            "--name", "Пример", "--foreign-name", "Example Ltd"])

        assert ("wallet", "TTest") in seen
        assert ("party", "7707083893", "Пример") in seen
        assert ("party", None, "Example Ltd") in seen
