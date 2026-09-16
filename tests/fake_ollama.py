"""Поддельная Ollama на настоящем HTTP-сокете.

Нужна, чтобы проверять поведение сервиса на ответах модели, которых в жизни
дожидаться дорого: битый JSON, выдуманная цитата, совет обойти требование,
обрыв соединения, молчание до таймаута. Подмены функции `complete` здесь мало:
она не проверила бы ни разбор HTTP-ответа, ни таймауты, ни то, как httpx
ведёт себя при разрыве.

Сервер отвечает на два адреса Ollama, которыми пользуется сервис:
`GET /api/tags` и `POST /api/chat`.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Behaviour:
    """Что сервер сделает с очередным запросом к /api/chat.

    status   — код ответа
    body     — тело ответа; строка отдаётся как есть, объект сериализуется
    delay_s  — задержка перед ответом (для проверки таймаута)
    drop     — закрыть соединение, не ответив
    raw      — отдать тело без валидного JSON-конверта Ollama
    """

    status: int = 200
    body: Any = None
    delay_s: float = 0.0
    drop: bool = False
    raw: str | None = None


def content(message: str) -> Behaviour:
    """Обычный ответ Ollama: JSON модели лежит в message.content строкой."""
    return Behaviour(body={"message": {"role": "assistant", "content": message}})


def payload(obj: Any) -> Behaviour:
    return content(json.dumps(obj, ensure_ascii=False))


@dataclass
class FakeOllama:
    behaviours: list[Behaviour] = field(default_factory=list)
    default: Behaviour | None = None
    tags: list[str] = field(default_factory=lambda: ["llama3.3:70b"])
    requests: list[dict] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def base_url(self) -> str:
        assert self._server is not None, "сервер не запущен"
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}" if host in ("0.0.0.0", "127.0.0.1") else f"http://{host}:{port}"

    @property
    def calls(self) -> int:
        return len(self.requests)

    def _next(self) -> Behaviour:
        with self._lock:
            if self.behaviours:
                return self.behaviours.pop(0)
        return self.default or content("{}")

    def __enter__(self) -> FakeOllama:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # тишина в выводе тестов
                return

            def _send(self, status: int, data: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
                if self.path.startswith("/api/tags"):
                    models = [{"name": name} for name in outer.tags]
                    self._send(200, json.dumps({"models": models}).encode("utf-8"))
                    return
                self._send(404, b"{}")

            def do_POST(self) -> None:  # noqa: N802 — имя задано базовым классом
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    request = json.loads(raw.decode("utf-8"))
                except ValueError:
                    request = {"_unparsed": raw.decode("utf-8", "replace")}
                with outer._lock:
                    outer.requests.append(request)

                behaviour = outer._next()
                if behaviour.delay_s:
                    threading.Event().wait(behaviour.delay_s)
                if behaviour.drop:
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    return
                if behaviour.raw is not None:
                    self._send(behaviour.status, behaviour.raw.encode("utf-8"))
                    return
                body = behaviour.body if behaviour.body is not None else {}
                self._send(behaviour.status, json.dumps(body, ensure_ascii=False).encode("utf-8"))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def script(self, behaviours: Iterable[Behaviour]) -> None:
        with self._lock:
            self.behaviours = list(behaviours)

    def prompts(self) -> list[str]:
        """Тексты, которые сервис отправил модели."""
        out: list[str] = []
        for request in self.requests:
            for message in request.get("messages") or []:
                if message.get("role") == "user":
                    out.append(str(message.get("content") or ""))
        return out


def run_against(
    server: FakeOllama,
    call: Callable[[], Any],
    *,
    model: str = "llama3.3:70b",
    timeout_s: float = 5.0,
) -> Any:
    """Выполняет `call`, направив клиент модели на поддельный сервер."""
    from app.core.catalog import load_catalog
    from app.core.config import get_settings

    settings = get_settings()
    catalog = load_catalog()
    saved = (
        settings.ollama_base_url,
        settings.ollama_model,
        settings.ollama_timeout_s,
        catalog.llm.base_url,
        catalog.llm.model,
    )
    settings.ollama_base_url = server.base_url
    settings.ollama_model = model
    settings.ollama_timeout_s = timeout_s
    catalog.llm.base_url = server.base_url
    catalog.llm.model = model
    try:
        return call()
    finally:
        (
            settings.ollama_base_url,
            settings.ollama_model,
            settings.ollama_timeout_s,
            catalog.llm.base_url,
            catalog.llm.model,
        ) = saved
