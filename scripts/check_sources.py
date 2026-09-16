"""Проверка доступности внешних источников из этой сети.

Сервис ставится в периметре компании, а корпоративная сеть часто режет
исходящие соединения. Пока источник не опрошен с той самой машины, неизвестно,
что увидит юрист: карточку организации или «сверка не выполнена». Скрипт
вызывает те же функции, что и рабочий прогон, и показывает, кто отвечает.

Текст договора здесь ни при чём: наружу уходят только адрес, ИНН
и наименование, которые вы передали параметрами.

    python -m scripts.check_sources
    python -m scripts.check_sources --inn 7707083893 --name "Сбербанк"
    python -m scripts.check_sources --address TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE
    python -m scripts.check_sources --json

Код возврата: 0 — ответил хотя бы один источник в каждой группе;
1 — какая-то группа не ответила целиком. Годится для проверки после установки.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from app.aml import kyt
from app.aml.providers import fetch_snapshot
from app.core import provider
from app.core.catalog import load_catalog
from app.counterparty.providers import lookup_free_sources
from app.llm.client import ollama_reachable
from app.llm.tiers import classify
from app.parsing.extract import WalletAddress

# Значения по умолчанию — публичные и безобидные. Адрес: контракт USDT TRC-20.
# У него заведомо есть история, поэтому видно не только «источник ответил»,
# но и что разбор ответа читает дату, число операций и баланс. Свой адрес
# подставляется параметром --address.
DEFAULT_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
DEFAULT_INN = "7707083893"
DEFAULT_NAME = "Сбербанк"
DEFAULT_FOREIGN = "Tether Limited"

RULE = "-" * 78


def _check_wallet(address: str) -> dict:
    network = "TRON" if address.startswith("T") else "EVM"
    started = time.perf_counter()
    snapshot = fetch_snapshot(WalletAddress(value=address, network=network))
    elapsed = time.perf_counter() - started

    facts = []
    if snapshot.account_found is False:
        facts.append("адрес не активирован в сети")
    if snapshot.created_at:
        facts.append(f"первая операция {snapshot.created_at.date().isoformat()}")
    if snapshot.last_activity_at:
        facts.append(f"последняя {snapshot.last_activity_at.date().isoformat()}")
    if snapshot.tx_count is not None:
        floor = "не менее " if snapshot.tx_count_is_floor else ""
        facts.append(f"операций {floor}{snapshot.tx_count}")
    elif snapshot.has_activity is True:
        facts.append("операции есть, число не определено")
    if snapshot.usdt_balance is not None:
        facts.append(f"USDT {snapshot.usdt_balance}")
    if snapshot.risk_labels:
        facts.append("метки: " + ", ".join(snapshot.risk_labels))

    # «Отвечает» — это когда из ответа что-то разобралось. Источник, вернувший
    # 200 и ничего пригодного, отвечающим считать нельзя: по такому ответу
    # в заключении будет пусто, а установщик решит, что всё в порядке.
    answered = snapshot.error is None and bool(facts)
    if snapshot.error:
        detail = snapshot.error
    elif facts:
        detail = "; ".join(facts)
    else:
        detail = "источник ответил, но ничего пригодного не вернул: проверьте --raw"

    return {
        "group": "кошелёк",
        "source": f"{network} (TronGrid / Etherscan / GoPlus)",
        "answered": answered,
        "detail": detail,
        "seconds": round(elapsed, 2),
    }


def _check_kyt(address: str) -> list[dict]:
    """Коммерческий скоринг адреса: отдельной строкой на каждый сервис."""
    network = "TRON" if address.startswith("T") else "EVM"
    started = time.perf_counter()
    verdicts = kyt.screen(address, network)
    elapsed = round(time.perf_counter() - started, 2)

    rows: list[dict] = []
    for verdict in verdicts:
        if verdict.performed:
            parts = [f"риск {verdict.risk_level}"] if verdict.risk_level else []
            if verdict.risk_score is not None:
                parts.append(f"балл {verdict.risk_score:.0f}")
            if verdict.signals:
                parts.append(verdict.signals[0])
            detail = "; ".join(parts) or "оценка получена"
        else:
            detail = verdict.detail
        rows.append(
            {
                "group": "кошелёк",
                "source": f"{verdict.provider} (скоринг)",
                "answered": verdict.performed,
                "detail": detail,
                "seconds": elapsed,
            }
        )
    return rows


def _inventory() -> list[tuple[str, str, str, str]]:
    """Что подключено: источник, тариф, состояние, что с этим делать."""
    titles = {"wallet": "КОШЕЛЁК", "counterparty": "КОНТРАГЕНТ", "sanctions": "САНКЦИИ"}
    advice = {
        provider.ProviderStatus.READY: "готов",
        provider.ProviderStatus.NO_KEY: "ключ не задан в .env",
        provider.ProviderStatus.NOT_IMPLEMENTED: "коннектора нет",
        provider.ProviderStatus.DISABLED: "выключен в providers.yaml",
        provider.ProviderStatus.UNKNOWN: "нет в каталоге",
    }
    rows: list[tuple[str, str, str, str]] = []
    for group, title in titles.items():
        for item in provider.inventory(group):
            rows.append((title, item.id, item.tier, advice[item.status]))
    return rows


def _print_inventory() -> None:
    print(f"\nЧто подключено\n{RULE}")
    current = ""
    for title, source_id, tier, state in _inventory():
        if title != current:
            current = title
            print(f"\n{title}")
        print(f"  {source_id:<18} {tier:<12} {state}")
    print(
        "\nПлатный источник без ключа — не поломка: заключение соберётся "
        "и без него,\nа в отчёте будет написано, что оценка не выполнена."
    )


def _raw_tron(address: str) -> None:
    """Сырой ответ TronGrid: по нему сверяются имена полей.

    Нужен, когда источник отвечает, а разбор ничего не находит: в ответе не те
    ключи, которых ждёт код.
    """
    import httpx

    from app.core.config import get_settings

    settings = get_settings()
    headers = {}
    if settings.trongrid_api_key:
        headers["TRON-PRO-API-KEY"] = settings.trongrid_api_key

    print(f"\nСырой ответ TronGrid по адресу {address}\n{RULE}")
    with httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0), headers=headers) as client:
        for title, url, params in (
            ("карточка адреса", f"https://api.trongrid.io/v1/accounts/{address}", None),
            (
                "операции",
                f"https://api.trongrid.io/v1/accounts/{address}/transactions",
                {"limit": 5, "only_confirmed": "true"},
            ),
        ):
            print(f"\n[{title}] {url}")
            try:
                response = client.get(url, params=params)
                print(f"  HTTP {response.status_code}")
                payload = response.json()
            except Exception as error:  # noqa: BLE001 — диагностика печатает всё
                print(f"  ошибка: {error.__class__.__name__}: {error}")
                continue
            if not isinstance(payload, dict):
                print(f"  тело не объект: {type(payload).__name__}")
                continue
            print(f"  ключи верхнего уровня: {sorted(payload)}")
            rows = payload.get("data")
            if isinstance(rows, list):
                print(f"  data: список из {len(rows)} элементов")
                if rows and isinstance(rows[0], dict):
                    print(f"  ключи первого элемента: {sorted(rows[0])[:40]}")
            meta = payload.get("meta")
            if isinstance(meta, dict):
                print(f"  meta: {meta}")


def _check_party(inn: str | None, name: str, *, foreign: bool) -> list[dict]:
    started = time.perf_counter()
    hits = lookup_free_sources(inn, name, foreign=foreign)
    elapsed = round(time.perf_counter() - started, 2)

    rows = []
    for hit in hits:
        rows.append(
            {
                "group": "контрагент (иностранный)" if foreign else "контрагент (РФ)",
                "source": hit.source_id,
                "answered": hit.performed,
                "detail": hit.detail or ("запись найдена" if hit.found else "записи нет"),
                "seconds": elapsed,
            }
        )
    if not rows:
        rows.append(
            {
                "group": "контрагент (иностранный)" if foreign else "контрагент (РФ)",
                "source": "—",
                "answered": False,
                "detail": "ни один источник не включён в config/providers.yaml",
                "seconds": elapsed,
            }
        )
    return rows


def _check_model() -> dict:
    catalog = load_catalog()
    started = time.perf_counter()
    reachable = ollama_reachable(catalog.llm.base_url)
    elapsed = round(time.perf_counter() - started, 2)
    tier = classify(catalog.llm.model)
    return {
        "group": "модель",
        "source": f"{catalog.llm.base_url} — {catalog.llm.model}",
        "answered": reachable,
        "detail": (
            f"{tier.label}; разбор оговорок выполняется"
            if reachable
            else "Ollama не отвечает: вердикт по матрице выполнится, раздел оговорок будет пуст"
        ),
        "seconds": elapsed,
    }


def _print_table(rows: list[dict]) -> None:
    print(f"\nДоступность источников из этой сети\n{RULE}")
    current = ""
    for row in rows:
        if row["group"] != current:
            current = row["group"]
            print(f"\n{current.upper()}")
        mark = "отвечает" if row["answered"] else "НЕ отвечает"
        print(f"  {row['source']:<46} {mark:<12} {row['seconds']:>5.2f} с")
        print(f"      {row['detail']}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Проверка доступности внешних источников из этой сети"
    )
    parser.add_argument("--address", default=DEFAULT_ADDRESS, help="адрес кошелька")
    parser.add_argument("--inn", default=DEFAULT_INN, help="ИНН российской стороны")
    parser.add_argument("--name", default=DEFAULT_NAME, help="наименование российской стороны")
    parser.add_argument("--foreign-name", default=DEFAULT_FOREIGN, help="иностранная сторона")
    parser.add_argument("--skip-model", action="store_true", help="не опрашивать Ollama")
    parser.add_argument("--json", action="store_true", help="вывести JSON")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="показать сырой ответ TronGrid: имена полей и размеры выдачи",
    )
    args = parser.parse_args(argv)

    rows: list[dict] = []
    if not args.skip_model:
        rows.append(_check_model())
    rows.append(_check_wallet(args.address))
    rows.extend(_check_kyt(args.address))
    rows.extend(_check_party(args.inn, args.name, foreign=False))
    rows.extend(_check_party(None, args.foreign_name, foreign=True))

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        _print_table(rows)
        _print_inventory()
        if args.raw:
            _raw_tron(args.address)

    groups: dict[str, bool] = {}
    for row in rows:
        if row["group"] == "модель":
            continue  # модель необязательна: без неё прогон идёт по матрице
        groups[row["group"]] = groups.get(row["group"], False) or row["answered"]

    silent = [group for group, ok in groups.items() if not ok]
    if silent:
        print(
            "Не ответила ни одна запись в группах: " + ", ".join(silent) + ".",
            file=sys.stderr,
        )
        print(
            "В заключении это будет «сверка не выполнена» — не «всё в порядке».",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
