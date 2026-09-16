import yaml

from app.core import provider
from app.core.catalog import CATALOG_PATH, load_catalog


def test_catalog_loads():
    catalog = load_catalog()

    assert CATALOG_PATH.is_file()
    assert catalog.llm.provider == "ollama"
    assert catalog.llm.model
    assert catalog.wallet
    assert catalog.counterparty
    assert catalog.sanctions


def test_default_model_in_yaml():
    payload = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))

    assert payload["llm"]["model"] == "llama3.3:70b"
    assert payload["llm"]["provider"] == "ollama"


def test_free_sources_are_on_by_default():
    catalog = load_catalog()

    assert {item.id for item in catalog.enabled("wallet", tier="free")} >= {
        "trongrid",
        "etherscan",
        "goplus",
    }
    assert {item.id for item in catalog.enabled("counterparty", tier="free")} >= {
        "dadata",
        "egrul",
        "rusprofile",
        "saby",
        "opencorporates",
        "gleif",
    }


def test_paid_sources_are_listed_and_keyed():
    """Платный источник живёт в каталоге включённым: клиент вписывает ключ
    в `.env`, а не правит yaml. Без ключа он просто не готов."""
    catalog = load_catalog()

    paid = [item for item in catalog.wallet + catalog.counterparty if item.tier == "paid"]
    assert paid

    for item in paid:
        assert item.env_key, f"{item.id}: у платного источника нет env_key"


def test_enabled_paid_sources_have_a_connector():
    """Включённая запись обязана быть реализованной: иначе клиент купит доступ
    и получит в заключении «коннектор не реализован»."""
    catalog = load_catalog()

    for group in ("wallet", "counterparty", "sanctions"):
        for item in catalog.enabled(group):
            assert item.id in provider.IMPLEMENTED[group], (
                f"{group}/{item.id}: запись включена, а коннектора нет"
            )


def test_paid_without_key_is_not_ready():
    catalog = load_catalog()

    assert catalog.uses("wallet", "trongrid")
    assert catalog.uses("counterparty", "egrul")
    assert not catalog.uses("wallet", "chainalysis")
    assert not catalog.uses("counterparty", "kontur_focus")


def test_provider_status_separates_missing_key_from_missing_connector():
    assert provider.status_of("counterparty", "kontur_focus") is provider.ProviderStatus.NO_KEY
    assert provider.status_of("counterparty", "spark") is provider.ProviderStatus.DISABLED
    assert provider.status_of("counterparty", "egrul") is provider.ProviderStatus.READY
    assert provider.status_of("wallet", "goplus") is provider.ProviderStatus.READY
    assert provider.status_of("counterparty", "nonexistent") is provider.ProviderStatus.UNKNOWN


def test_blocked_reports_only_actionable_sources():
    """Выключенный источник в заключении не упоминается: клиент выключил его сам."""
    ids = {item.id for item in provider.blocked("counterparty")}

    assert "kontur_focus" in ids
    assert "spark" not in ids
    assert "egrul" not in ids


def test_self_hosted_sanctions_needs_no_key():
    assert provider.status_of("sanctions", "opensanctions") is provider.ProviderStatus.READY
    assert provider.base_url("sanctions", "opensanctions", "http://fallback").startswith("http")
