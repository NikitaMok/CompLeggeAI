from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

# Адреса кошельков. Наличие «сырого» адреса вместо адреса-идентификатора
# цифрового депозитария — нарушение правила ADR-001.
_TRON_ADDRESS = re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")
_EVM_ADDRESS = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

_TICKERS = {
    "USDT": ("usdt", "tether"),
    "USDC": ("usdc",),
    "DAI": ("dai",),
    "BTC": ("btc", "bitcoin", "биткойн", "биткоин"),
    "ETH": ("eth", "ethereum", "эфир"),
}

_NETWORKS = {
    "TRC-20": ("trc-20", "trc20", "tron", "трон"),
    "ERC-20": ("erc-20", "erc20", "ethereum", "эфириум"),
    "BEP-20": ("bep-20", "bep20", "bsc", "binance smart chain"),
    "TON": ("ton", "the open network"),
}

_MULTIPLIERS = {
    "тыс": Decimal(1_000),
    "тысяч": Decimal(1_000),
    "млн": Decimal(1_000_000),
    "миллион": Decimal(1_000_000),
    "млрд": Decimal(1_000_000_000),
}

_CURRENCIES = {
    "RUB": ("руб", "рубл", "rub", "₽"),
    "USD": ("долл", "usd", "$"),
    "EUR": ("евро", "eur", "€"),
    "CNY": ("юан", "cny", "юаней"),
}

# Тикеры стоят перед фиатными обозначениями, иначе из «USDT» альтернатива «USD»
# отхватит только начало. Замыкающий lookahead не даёт совпасть с префиксом.
_AMOUNT = re.compile(
    r"(?P<number>\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"\s*(?:\([^)]*\)\s*)?"
    r"(?P<multiplier>тыс(?:\.|яч[а-я]*)?|млн\.?|миллион[а-я]*|млрд\.?)?\s*"
    r"(?P<currency>USDT|USDC|DAI|BTC|ETH"
    r"|рубл[а-я]*|руб\.?|RUB|₽|долл[а-я.]*|USD|\$|евро|EUR|€|юан[а-я]*|CNY)"
    r"(?![A-Za-zА-Яа-яЁё])",
    re.IGNORECASE,
)

_INN = re.compile(r"ИНН[\s:№]*(\d{10}|\d{12})\b", re.IGNORECASE)
# Наименование организации может быть разорвано переносом строки: извлечение
# из PDF ставит перенос там, где в DOCX стоял пробел. Поэтому внутри
# наименования допускается любой пробельный символ, а найденное потом
# схлопывается в одну строку.
# В преамбуле договора организационно-правовую форму пишут словами
# («Общество с ограниченной ответственностью «Ромашка»»), а дальше по тексту —
# аббревиатурой. Ловим обе записи: иначе сторона остаётся без наименования
# и в заключение попадает голый ИНН.
_RU_ORG = re.compile(
    r"(?:"
    r"Обществ\w*\s+с\s+ограниченной\s+ответственностью"
    r"|Акционерн\w*\s+обществ\w*"
    r"|Публичн\w*\s+акционерн\w*\s+обществ\w*"
    r"|Непубличн\w*\s+акционерн\w*\s+обществ\w*"
    r"|Закрыт\w*\s+акционерн\w*\s+обществ\w*"
    r"|Открыт\w*\s+акционерн\w*\s+обществ\w*"
    r"|Индивидуальн\w*\s+предпринимател\w*"
    r"|ООО|АО|ПАО|НАО|ЗАО|ОАО|ИП"
    r")\s*[«\"'][^»\"']{2,160}[»\"']",
    re.IGNORECASE,
)
# Формы иностранных компаний. Точка после Ltd и Inc ставится не всегда,
# а партнёрства и индийские частные компании пишутся LLP и Pvt Ltd —
# без этих вариантов иностранная сторона из договора просто исчезала.
_EN_SUFFIX = (
    r"Co\.,?\s*Ltd\.?"
    r"|(?:Pte|Pvt|Pty|Sdn)\.?\s*(?:Ltd|Bhd)\.?"
    r"|Ltd\.?|Limited|LLP\.?|L\.?L\.?C\.?|LLC"
    r"|Inc\.?|Incorporated|Corp\.?|Corporation"
    r"|GmbH|mbH|AG|KG|S\.?A\.?S?\.?|S\.?p\.?A\.?|S\.?R\.?L\.?"
    r"|B\.?V\.?|N\.?V\.?|A/S|ApS|AB|Oy|OyJ"
    r"|PLC|plc|JSC|OJSC|CJSC"
)
_EN_ORG = re.compile(
    r"\b[A-Z][A-Za-z0-9&.,'\-\s]{2,80}?"
    rf"(?:{_EN_SUFFIX})\b"
)

_FOREIGN_TRADE = re.compile(r"внешнеторгов\w*", re.IGNORECASE)
_RESIDENT = re.compile(r"\bрезидент\w*", re.IGNORECASE)
_NON_RESIDENT = re.compile(r"\bнерезидент\w*", re.IGNORECASE)
_IDENTIFIER_ADDRESS = re.compile(r"адрес\w*[\s-]*идентификатор\w*", re.IGNORECASE)
_DEPOSITARY = re.compile(r"цифров\w*\s+депозитари\w*", re.IGNORECASE)
_RECORD_MOMENT = re.compile(r"внесени\w*\s+запис\w*", re.IGNORECASE)


@dataclass(frozen=True)
class WalletAddress:
    value: str
    network: str

    def __str__(self) -> str:
        return f"{self.value} ({self.network})"


@dataclass(frozen=True)
class PartyMention:
    name: str
    inn: str | None = None
    # Кем лицо названо в договоре. Регулярка находит все организации подряд:
    # без роли цифровой депозитарий и эмитент стейблкоина попадали в отчёт
    # наравне со сторонами сделки, под общим заголовком «контрагент».
    role: str | None = None


@dataclass(frozen=True)
class MoneyAmount:
    value: Decimal
    currency: str
    raw: str


@dataclass
class ExtractedFacts:
    wallet_addresses: list[WalletAddress] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    amounts: list[MoneyAmount] = field(default_factory=list)
    inns: list[str] = field(default_factory=list)
    parties: list[PartyMention] = field(default_factory=list)
    mentions_foreign_trade: bool = False
    mentions_resident: bool = False
    mentions_non_resident: bool = False
    mentions_identifier_address: bool = False
    mentions_depositary: bool = False
    mentions_record_moment: bool = False

    def max_amount(self, currency: str = "RUB") -> Decimal | None:
        values = [amount.value for amount in self.amounts if amount.currency == currency]
        return max(values) if values else None

    def max_foreign_amount(self) -> MoneyAmount | None:
        """Наибольшая сумма не в рублях.

        Договор ВЭД чаще всего номинирован в долларах, евро или юанях, а пороги
        115-ФЗ и Инструкции № 181-И установлены в рублях либо в эквиваленте.
        Пересчитать без курса нельзя, но и молчать нельзя: молчание правила
        читается как «порог не достигнут».
        """
        foreign = [amount for amount in self.amounts if amount.currency != "RUB"]
        return max(foreign, key=lambda item: item.value) if foreign else None


def _normalize_currency(token: str) -> str:
    lowered = token.lower().rstrip(".")
    if lowered.upper() in _TICKERS:
        return lowered.upper()
    for code, variants in _CURRENCIES.items():
        if any(lowered.startswith(variant) for variant in variants):
            return code
    return token.upper()


def _normalize_multiplier(token: str | None) -> Decimal:
    if not token:
        return Decimal(1)
    lowered = token.lower().rstrip(".")
    for prefix, factor in _MULTIPLIERS.items():
        if lowered.startswith(prefix):
            return factor
    return Decimal(1)


def extract_amounts(text: str) -> list[MoneyAmount]:
    amounts: list[MoneyAmount] = []

    for match in _AMOUNT.finditer(text):
        digits = match.group("number").replace("\u00a0", "").replace(" ", "").replace(",", ".")
        value = Decimal(digits) * _normalize_multiplier(match.group("multiplier"))
        amounts.append(
            MoneyAmount(
                value=value,
                currency=_normalize_currency(match.group("currency")),
                raw=match.group(0).strip(),
            )
        )

    return amounts


def extract_wallet_addresses(text: str) -> list[WalletAddress]:
    addresses = [
        WalletAddress(value=match.group(0), network="TRON")
        for match in _TRON_ADDRESS.finditer(text)
    ]
    addresses.extend(
        WalletAddress(value=match.group(0), network="EVM")
        for match in _EVM_ADDRESS.finditer(text)
    )
    return addresses


def _find_known(text: str, catalogue: dict[str, tuple[str, ...]]) -> list[str]:
    lowered = text.lower()
    return [
        code
        for code, variants in catalogue.items()
        if any(re.search(rf"\b{re.escape(variant)}\b", lowered) for variant in variants)
    ]


# Слово рядом с наименованием → роль лица в договоре.
_ROLE_MARKERS: tuple[tuple[str, str], ...] = (
    ("депозитар", "цифровой депозитарий"),
    ("эмитент", "эмитент цифровой валюты"),
    ("уполномоченн", "уполномоченный банк"),
    ("банк", "банк"),
    ("покупател", "покупатель"),
    ("поставщик", "поставщик"),
    ("продав", "продавец"),
    ("заказчик", "заказчик"),
    ("исполнител", "исполнитель"),
    ("перевозчик", "перевозчик"),
    ("агент", "агент"),
    ("комиссионер", "комиссионер"),
)


def _role_in(segment: str) -> str | None:
    lowered = segment.lower()
    best: tuple[int, str] | None = None
    for marker, role in _ROLE_MARKERS:
        position = lowered.find(marker)
        if position < 0:
            continue
        if best is None or position < best[0]:
            best = (position, role)
    return best[1] if best else None


def _role_near(before: str, after: str) -> str | None:
    """Роль ищется сначала после наименования, потом перед ним.

    В договорах пишут «…, именуемое в дальнейшем «Поставщик»», то есть роль
    стоит справа. Маркер слева чаще принадлежит предыдущей стороне: в шапке
    «…именуемое «Покупатель», и Berlin Machinen GmbH…» немецкий поставщик
    получал роль покупателя.
    """
    return _role_in(after) or _role_in(before)


def extract_party_mentions(text: str) -> list[PartyMention]:
    """Организации, названные в договоре, с ИНН и ролью, если они рядом.

    ИНН привязывается к ближайшему наименованию слева и только к одному.
    Раньше окно поиска захватывало по 80 знаков в обе стороны, и в типовой
    шапке «ООО «Х», ИНН …, и компанией Y Co., Ltd» один и тот же ИНН
    привязывался к обеим сторонам. Вторая сторона потом пропадала при
    склейке по ИНН — из проверки контрагента исчезал иностранный поставщик,
    ради которого она и нужна.
    """
    spans: list[tuple[int, str]] = []
    for pattern in (_RU_ORG, _EN_ORG):
        for match in pattern.finditer(text):
            spans.append((match.start(), " ".join(match.group(0).split())))
    spans.sort()

    # Одинаковые наименования схлопываем, оставляя первое вхождение.
    unique: list[tuple[int, str]] = []
    seen_names: set[str] = set()
    for start, name in spans:
        if name in seen_names:
            continue
        seen_names.add(name)
        unique.append((start, name))

    inn_at = [(match.start(1), match.group(1)) for match in _INN.finditer(text)]
    taken_names: dict[int, str] = {}
    used_inns: set[str] = set()

    for inn_pos, inn in inn_at:
        if inn in used_inns:
            continue
        # Ближайшее наименование слева, не дальше 150 знаков.
        candidates = [
            (inn_pos - start, start)
            for start, name in unique
            if start < inn_pos and inn_pos - (start + len(name)) <= 150
        ]
        candidates = [item for item in candidates if item[1] not in taken_names]
        if not candidates:
            continue
        _distance, start = min(candidates)
        taken_names[start] = inn
        used_inns.add(inn)

    parties: list[PartyMention] = []
    for start, name in unique:
        end = start + len(name)
        parties.append(
            PartyMention(
                name=name,
                inn=taken_names.get(start),
                role=_role_near(text[max(0, start - 60) : start], text[end : end + 120]),
            )
        )

    for _position, inn in inn_at:
        if inn not in used_inns:
            parties.append(PartyMention(name="", inn=inn))
            used_inns.add(inn)
    return parties


def extract_facts(text: str) -> ExtractedFacts:
    return ExtractedFacts(
        wallet_addresses=extract_wallet_addresses(text),
        tickers=_find_known(text, _TICKERS),
        networks=_find_known(text, _NETWORKS),
        amounts=extract_amounts(text),
        inns=[match.group(1) for match in _INN.finditer(text)],
        parties=extract_party_mentions(text),
        mentions_foreign_trade=bool(_FOREIGN_TRADE.search(text)),
        mentions_resident=bool(_RESIDENT.search(text)),
        mentions_non_resident=bool(_NON_RESIDENT.search(text)),
        mentions_identifier_address=bool(_IDENTIFIER_ADDRESS.search(text)),
        mentions_depositary=bool(_DEPOSITARY.search(text)),
        mentions_record_moment=bool(_RECORD_MOMENT.search(text)),
    )
