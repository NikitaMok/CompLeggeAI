"""Человеческое описание сетевой ошибки.

Заключение читает юрист, а не разработчик. `ProxyError` и `ReadTimeout`
в отчёте ничего ему не сообщают, а выглядят как сбой продукта. При этом
смысл важен: «источник не ответил» и «источник ответил отказом» — разные
основания, и подменять их общим «ошибка» тоже нельзя.
"""

from __future__ import annotations

import httpx

NO_CONNECTION = "нет соединения с источником"
CONNECT_TIMED_OUT = "соединение с источником не установилось"
TIMED_OUT = "источник не ответил за отведённое время"
DISCONNECTED = "источник оборвал соединение"
PROXY = "соединение с источником не прошло через прокси"
REDIRECTS = "источник увёл запрос по кругу переадресаций"
REFUSED = "источник ответил отказом"
UNAVAILABLE = "источник недоступен"


def describe(error: Exception) -> str:
    """Короткая причина, пригодная для отчёта."""
    if isinstance(error, httpx.ProxyError):
        return PROXY
    # Порядок важен: ConnectTimeout — подкласс TimeoutException. Раньше оба
    # случая писались как «не ответил за N с», и по отчёту нельзя было отличить
    # медленный источник от недостижимого.
    if isinstance(error, httpx.ConnectTimeout):
        return CONNECT_TIMED_OUT
    if isinstance(error, httpx.TimeoutException):
        return TIMED_OUT
    if isinstance(error, httpx.ConnectError):
        return NO_CONNECTION
    if isinstance(error, httpx.RemoteProtocolError):
        return DISCONNECTED
    if isinstance(error, httpx.TooManyRedirects):
        return REDIRECTS
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        if code == 429:
            return "источник ограничил частоту запросов"
        if code in (401, 403):
            return "источник отказал в доступе"
        if 500 <= code < 600:
            return "на стороне источника ошибка"
        return f"{REFUSED} (код {code})"
    return UNAVAILABLE


def source_unavailable(source: str, error: Exception) -> str:
    """Строка вида «ЕГРЮЛ: источник не ответил за отведённое время»."""
    return f"{source}: {describe(error)}"
