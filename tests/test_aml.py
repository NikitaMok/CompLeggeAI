from datetime import date, datetime, timezone
from decimal import Decimal

import httpx

from app.aml.fatf import load_fatf_snapshot
from app.aml.score import AddressSnapshot, score_snapshot
from app.parsing.document import Document, SourceFormat
from app.rules.contract import ContractView
from app.rules.engine import FindingStatus, evaluate
from app.rules.predicates import Verdict, fatf_jurisdiction_addressed

LAW_IN_FORCE = date(2026, 9, 1)


def _view(text: str) -> ContractView:
    lines = [line for line in text.strip().split("\n") if line.strip()]
    return ContractView.from_document(Document(SourceFormat.DOCX, lines))


class TestFatfSnapshot:
    def test_contains_three_call_for_action_states(self):
        snapshot = load_fatf_snapshot()

        assert snapshot.as_of == "2026-06-19"
        assert {item.iso2 for item in snapshot.jurisdictions} == {"KP", "IR", "MM"}
        assert {item.iso2 for item in snapshot.jurisdictions if item.in_rf_order_361} == {
            "KP",
            "IR",
        }

    def test_myanmar_is_only_in_fatf_snapshot(self):
        snapshot = load_fatf_snapshot()
        myanmar = next(item for item in snapshot.jurisdictions if item.iso2 == "MM")

        assert not myanmar.in_rf_order_361
        assert snapshot.match_in("биржа в Мьянме")

    def test_matches_iran_and_ignores_china(self):
        snapshot = load_fatf_snapshot()

        assert snapshot.match_in("биржа зарегистрирована в Иране")
        assert not snapshot.match_in("поставщик из Китайской Народной Республики")


class TestFatfPredicate:
    def test_unresolved_when_jurisdiction_is_unnamed(self):
        outcome = fatf_jurisdiction_addressed(_view("1.1. Оплата USDT через депозитарий."))

        assert outcome.verdict is Verdict.UNRESOLVED
        assert "Правительства РФ" in outcome.evidence

    def test_fails_when_listed_state_is_named_without_clause(self):
        outcome = fatf_jurisdiction_addressed(
            _view("1.1. Адрес администрирует организация, зарегистрированная в Иране.")
        )

        assert outcome.verdict is Verdict.FAILED
        assert "Иран" in outcome.evidence
        assert "361" in outcome.evidence

    def test_fails_when_myanmar_named_without_clause(self):
        outcome = fatf_jurisdiction_addressed(
            _view("1.1. Адрес администрирует организация, зарегистрированная в Мьянме.")
        )

        assert outcome.verdict is Verdict.FAILED
        assert "Мьянма" in outcome.evidence
        assert "отсутствует" in outcome.evidence

    def test_passes_when_fatf_clause_present(self):
        outcome = fatf_jurisdiction_addressed(
            _view(
                "1.1. Адрес-идентификатор не администрируется организацией из государства, "
                "не выполняющего рекомендации ФАТФ. Если иное будет установлено, "
                "операция подлежит обязательному контролю независимо от суммы."
            )
        )

        assert outcome.verdict is Verdict.PASSED

    def test_compliant_contract_passes_fatf_clause(self, compliant_docx):
        report = evaluate(ContractView.from_file(compliant_docx), moment=LAW_IN_FORCE)

        assert report.by_code("AML-002").status is FindingStatus.PASSED
        assert report.status.value == "green"


class TestAddressScore:
    def test_new_empty_address_is_high_risk(self):
        snapshot = AddressSnapshot(
            address="TTEST",
            network="TRON",
            created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
            tx_count=0,
            usdt_balance=Decimal(0),
        )

        result = score_snapshot(
            snapshot,
            threshold=50,
            now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )

        assert result.score is not None and result.score >= 50
        assert result.band == "высокий"
        assert result.disclaimer

    def test_lookup_error_does_not_invent_a_score(self):
        result = score_snapshot(
            AddressSnapshot(address="0x1", network="EVM", error="ключ Etherscan не задан")
        )

        assert result.score is None
        assert result.band == "нет данных"


class TestGoPlusLabels:
    def test_public_labels_raise_the_band(self):
        snapshot = AddressSnapshot(
            address="0xabc",
            network="EVM",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            tx_count=50,
            usdt_balance=Decimal(100),
            risk_labels=("фишинг", "миксер"),
        )

        result = score_snapshot(
            snapshot,
            threshold=50,
            now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )

        assert result.band == "высокий"
        assert "фишинг" in result.factors[0] or any("фишинг" in item for item in result.factors)
        assert result.labels == ("фишинг", "миксер")


class TestGoPlusFetch:
    def test_merges_labels_onto_tron_snapshot(self):
        from app.aml.providers import fetch_snapshot
        from app.parsing.extract import WalletAddress

        def handler(request: httpx.Request) -> httpx.Response:
            if "gopluslabs" in str(request.url):
                return httpx.Response(
                    200,
                    json={"result": {"phishing_activities": "1", "sanctioned": "0"}},
                )
            if "accounts" in str(request.url) and "transactions" not in str(request.url):
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"data": []})

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            snapshot = fetch_snapshot(
                WalletAddress(value="TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE", network="TRON"),
                client,
            )

        assert "фишинг" in snapshot.risk_labels


class TestUnknownIsNotZero:
    """Отсутствие сведений не должно превращаться в измерение.

    Живой прогон 15.09.2026 показал ровно это: адрес с историей получал
    «USDT 0» и «нет сведений об истории», потому что пустой список балансов
    и неудачный подсчёт операций записывались как нули.
    """

    def test_missing_trc20_block_is_not_a_zero_balance(self):
        from app.aml.providers import _tron_usdt

        assert _tron_usdt({"address": "T..."}) is None

    def test_present_but_empty_trc20_block_is_a_measured_zero(self):
        from app.aml.providers import _tron_usdt

        assert _tron_usdt({"trc20": []}) == 0

    def test_usdt_amount_is_read_from_the_list(self):
        from decimal import Decimal

        from app.aml.providers import USDT_TRC20_CONTRACT, _tron_usdt

        row = {
            "trc20": [
                {"TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj": "900"},
                {USDT_TRC20_CONTRACT: "2500000"},
            ]
        }
        assert _tron_usdt(row) == Decimal("2.5")

    def test_unknown_balance_adds_no_risk_points(self):
        from app.aml.score import score_snapshot

        known_zero = score_snapshot(
            AddressSnapshot(address="T1", network="TRON", usdt_balance=Decimal(0), tx_count=7)
        )
        unknown = score_snapshot(
            AddressSnapshot(address="T1", network="TRON", usdt_balance=None, tx_count=7)
        )

        assert known_zero.score > unknown.score
        assert any("нулевой баланс" in f for f in known_zero.factors)
        assert any("не вернул" in f for f in unknown.factors)

    def test_inactive_address_is_named_as_such(self):
        from app.aml.score import score_snapshot

        score = score_snapshot(
            AddressSnapshot(
                address="T1", network="TRON", account_found=False, has_activity=False
            )
        )

        assert any("не активирован" in f for f in score.factors)
        assert not any("баланс" in f for f in score.factors)

    def test_busy_address_is_not_punished_for_missing_history(self):
        from app.aml.score import score_snapshot

        score = score_snapshot(
            AddressSnapshot(
                address="T1",
                network="TRON",
                account_found=True,
                tx_count=200,
                tx_count_is_floor=True,
                has_activity=True,
                usdt_balance=Decimal("12500.5"),
            )
        )

        assert not any("не вернул историю" in f for f in score.factors)
        assert any("не менее 200" in f for f in score.factors)
        assert score.band == "низкий"


class TestTronActivity:
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_empty_page_means_no_operations(self):
        from app.aml.providers import _tron_activity

        with self._client(lambda r: httpx.Response(200, json={"data": []})) as client:
            activity = _tron_activity(client, "T1", {})

        assert activity.count == 0
        assert activity.count_is_floor is False
        assert activity.has_activity is False

    def test_partial_page_gives_an_exact_count(self):
        from app.aml.providers import _tron_activity

        rows = [{"txID": str(i), "block_timestamp": 1_789_000_000_000} for i in range(7)]
        with self._client(lambda r: httpx.Response(200, json={"data": rows})) as client:
            activity = _tron_activity(client, "T1", {})

        assert activity.count == 7
        assert activity.count_is_floor is False
        assert activity.has_activity is True

    def test_full_page_gives_a_floor_not_silence(self):
        from app.aml.providers import _TRON_PAGE, _tron_activity

        rows = [{"txID": str(i), "block_timestamp": 1_789_000_000_000} for i in range(_TRON_PAGE)]
        with self._client(lambda r: httpx.Response(200, json={"data": rows})) as client:
            activity = _tron_activity(client, "T1", {})

        assert activity.count == _TRON_PAGE
        assert activity.count_is_floor is True
        assert activity.has_activity is True

    def test_http_error_stays_unknown(self):
        from app.aml.providers import _tron_activity

        with self._client(lambda r: httpx.Response(503, text="offline")) as client:
            activity = _tron_activity(client, "T1", {})

        assert activity.count is None
        assert activity.has_activity is None
        assert activity.first_at is None


class TestRealTronGridShape:
    """Разбор ответа TronGrid по форме, снятой с живого API 15.09.2026.

    Карточка адреса: ключи ['account_name', 'account_resource', 'address',
    'assetV2', 'balance', 'free_asset_net_usageV2', 'frozenV2',
    'net_window_optimized', 'net_window_size', 'trc20', 'type'].
    Поля `create_time` в ней нет. Балансы TRC-20 перечислены по адресу
    контракта, а не по тикеру.
    Операция: ключи ['blockNumber', 'block_timestamp', ..., 'txID'].
    """

    ACCOUNT_KEYS = [
        "account_name",
        "account_resource",
        "address",
        "assetV2",
        "balance",
        "free_asset_net_usageV2",
        "frozenV2",
        "net_window_optimized",
        "net_window_size",
        "trc20",
        "type",
    ]

    def _account(self, trc20):
        row = {key: "" for key in self.ACCOUNT_KEYS}
        row["trc20"] = trc20
        row["balance"] = 1_000_000
        return {"data": [row], "meta": {"at": 1789486944112, "page_size": 1}, "success": True}

    def _transactions(self, count, *, oldest_ms=1600000000000, newest_ms=1789000000000):
        rows = []
        for index in range(count):
            rows.append(
                {
                    "blockNumber": 1000 + index,
                    "block_timestamp": newest_ms - index * 86_400_000,
                    "txID": f"tx{index}",
                    "ret": [{"contractRet": "SUCCESS"}],
                }
            )
        return rows, oldest_ms

    def test_usdt_balance_is_read_by_contract_address(self):
        from app.aml.providers import USDT_TRC20_CONTRACT, _tron_usdt

        row = {"trc20": [{USDT_TRC20_CONTRACT: "12500000"}]}
        assert _tron_usdt(row) == Decimal("12.5")

    def test_balance_was_unreadable_before_the_fix(self):
        """Ключ — адрес контракта, подстроки USDT в нём нет."""
        from app.aml.providers import USDT_TRC20_CONTRACT

        assert "USDT" not in USDT_TRC20_CONTRACT.upper()

    def test_snapshot_from_the_real_shape(self):
        from app.aml.providers import USDT_TRC20_CONTRACT, fetch_snapshot
        from app.parsing.extract import WalletAddress

        rows, oldest = self._transactions(200)

        def handler(request: httpx.Request) -> httpx.Response:
            if "goplus" in request.url.host:
                return httpx.Response(200, json={"result": {}})
            if request.url.path.endswith("/transactions"):
                if request.url.params.get("order_by", "").startswith("block_timestamp,asc"):
                    return httpx.Response(
                        200,
                        json={"data": [{"block_timestamp": oldest, "txID": "first"}]},
                    )
                return httpx.Response(200, json={"data": rows})
            return httpx.Response(200, json=self._account([{USDT_TRC20_CONTRACT: "2500000"}]))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            snapshot = fetch_snapshot(
                WalletAddress(value="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", network="TRON"),
                client,
            )

        assert snapshot.error is None
        assert snapshot.account_found is True
        assert snapshot.usdt_balance == Decimal("2.5")
        assert snapshot.tx_count == 200
        assert snapshot.tx_count_is_floor is True
        assert snapshot.has_activity is True
        assert snapshot.created_at is not None, "возраст берётся по первой операции"
        assert snapshot.last_activity_at is not None

    def test_missing_create_time_does_not_lose_the_age(self):
        """В карточке нет create_time — возраст всё равно определяется."""
        from app.aml.providers import _tron_activity

        oldest_ms = 1_600_000_000_000

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("order_by", "").startswith("block_timestamp,asc"):
                return httpx.Response(200, json={"data": [{"block_timestamp": oldest_ms}]})
            return httpx.Response(200, json={"data": [{"block_timestamp": 1_789_000_000_000}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            activity = _tron_activity(client, "T1", {})

        assert activity.first_at is not None
        assert activity.first_at.year == 2020
        assert activity.last_at is not None

    def test_dormant_address_is_flagged(self):
        from app.aml.score import score_snapshot

        score = score_snapshot(
            AddressSnapshot(
                address="T1",
                network="TRON",
                account_found=True,
                tx_count=12,
                has_activity=True,
                usdt_balance=Decimal("1000"),
                created_at=datetime(2019, 1, 1, tzinfo=timezone.utc),
                last_activity_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
            ),
            now=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )

        assert any("последняя операция" in f for f in score.factors)

    def test_failed_ascending_request_does_not_invent_an_age(self):
        from app.aml.providers import _tron_activity

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("order_by", "").startswith("block_timestamp,asc"):
                return httpx.Response(503, text="offline")
            return httpx.Response(200, json={"data": [{"block_timestamp": 1_789_000_000_000}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            activity = _tron_activity(client, "T1", {})

        assert activity.first_at is None
        assert activity.has_activity is True
