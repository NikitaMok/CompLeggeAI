from datetime import date
from hashlib import sha256
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.uploads import stored_upload, validate_upload
from app.core.config import PROJECT_ROOT, get_settings
from app.llm.client import ollama_reachable
from app.llm.tiers import classify
from app.norms.hybrid import hybrid_search
from app.parsing.document import EmptyDocumentError, UnsupportedFormatError
from app.pipeline import CheckResult, run_check
from app.report.pdf import MissingCyrillicFontError, render_pdf
from app.report.serialize import serialize_report
from app.rules.contract import ContractView
from app.rules.engine import default_check_date
from app.rules.guardrail import CircumventionAttempt

STATIC_DIR = PROJECT_ROOT / "app" / "web" / "static"

app = FastAPI(
    title="CompLeggeAI",
    description=(
        "Проверка внешнеторговых контрактов с расчётами в цифровой валюте "
        "на соответствие ФЗ № 282-ФЗ, 283-ФЗ и 115-ФЗ. "
        "Загруженный файл удаляется сразу после формирования заключения."
    ),
    version=__version__,
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Один юрист, один процесс: повторный запрос PDF по тому же файлу не гоняет
# Ollama и реестры второй раз.
_RESULT_LOCK = Lock()
_LAST_RESULT: tuple[str, CheckResult] | None = None


def _result_key(content: bytes, moment: date | None) -> str:
    checked = default_check_date(moment).isoformat()
    return sha256(content + b"|" + checked.encode()).hexdigest()


@app.get("/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    dense = "off"
    try:
        from app.norms.dense import dense_available

        if settings.dense_search and dense_available():
            dense = "on"
    except Exception:
        dense = "off"
    # Опрос Ollama ходит по сети: в асинхронном обработчике он занял бы
    # единственный поток событий на время ожидания.
    reachable = await run_in_threadpool(ollama_reachable)
    tier = classify(settings.ollama_model)
    return {
        "status": "ok",
        "env": settings.app_env,
        "version": app.version,
        "dense": dense,
        "ollama": "on" if reachable else "off",
        "model": tier.to_dict(),
    }


@app.get("/")
async def index() -> FileResponse:
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="интерфейс не собран")
    return FileResponse(page)


def _check_upload(content: bytes, filename: str | None, moment: date | None) -> CheckResult:
    global _LAST_RESULT
    settings = get_settings()
    suffix = validate_upload(filename, content, settings.max_upload_mb * 1024 * 1024)
    source_name = Path(filename or f"document{suffix}").name
    key = _result_key(content, moment)
    with _RESULT_LOCK:
        cached = _LAST_RESULT
    if cached is not None and cached[0] == key:
        return cached[1]
    with stored_upload(content, suffix, root=settings.upload_tmp_dir) as path:
        try:
            contract = ContractView.from_file(path)
        except UnsupportedFormatError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except EmptyDocumentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            result = run_check(
                contract,
                source_name=source_name,
                moment=default_check_date(moment),
            )
        except CircumventionAttempt as error:
            raise HTTPException(
                status_code=500,
                detail="заключение не сформировано",
            ) from error
    with _RESULT_LOCK:
        _LAST_RESULT = (key, result)
    return result


def _payload(result: CheckResult, quote_norms: bool) -> dict:
    return serialize_report(
        result.report,
        source_name=result.source_name,
        quote_norms=quote_norms,
        address_scores=list(result.address_scores),
        counterparties=list(result.counterparties),
        llm=result.llm,
    )


@app.post("/check")
async def check_contract(
    file: UploadFile = File(..., description="контракт в формате PDF или DOCX"),
    on: date | None = Query(None, description="дата проверки, ГГГГ-ММ-ДД"),
    quote_norms: bool = Query(False, description="включить дословный текст нормы"),
) -> dict:
    content = await file.read()
    result = _check_upload(content, file.filename, on)
    return _payload(result, quote_norms)


@app.post("/check/pdf")
async def check_contract_pdf(
    file: UploadFile = File(..., description="контракт в формате PDF или DOCX"),
    on: date | None = Query(None, description="дата проверки, ГГГГ-ММ-ДД"),
    quote_norms: bool = Query(False, description="включить дословный текст нормы"),
) -> Response:
    content = await file.read()
    result = _check_upload(content, file.filename, on)
    try:
        pdf = render_pdf(
            result.report,
            source_name=result.source_name,
            quote_norms=quote_norms,
            address_scores=list(result.address_scores),
            counterparties=list(result.counterparties),
            llm=result.llm,
        )
    except MissingCyrillicFontError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="zaklyuchenie.pdf"'},
    )


@app.get("/search")
def search_norms(
    q: str = Query(..., min_length=1, description="формулировка"),
    limit: int = Query(5, ge=1, le=20),
    act: str | None = Query(None, description="ограничить акт, например 282-ФЗ"),
) -> dict:
    hits = hybrid_search(q, limit=limit, act=act)
    return {
        "query": q,
        "hits": [
            {
                "ref": hit.norm.reference,
                "score": round(hit.score, 4),
                "text": hit.norm.text,
            }
            for hit in hits
        ],
    }
