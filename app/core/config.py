from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    llm_provider: str = "ollama"
    llm_temperature: float = 0.0
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.3:70b"
    # 70B на первом токене может думать минуты; меньше — обрыв посреди ответа.
    ollama_timeout_s: float = 300.0
    # Длинный договор идёт в модель окнами по 24 000 знаков с перекрытием.
    # Потолок нужен, чтобы договор на тысячу страниц не превратил один прогон
    # в часы работы модели: непрочитанная часть честно названа в отчёте.
    llm_max_windows: int = 6

    # --- ключи клиента: платный скоринг криптоадреса ---
    amlbot_api_key: str = ""
    # Публичной спецификации у AMLBot нет: адрес запроса выдаётся вместе
    # с доступом. Шаблон подставляет {address}, {asset} и {key}.
    amlbot_api_url: str = ""
    misttrack_api_key: str = ""
    chainalysis_api_key: str = ""
    elliptic_api_key: str = ""
    bitok_api_key: str = ""

    # --- ключи клиента: проверка контрагента ---
    # Бесплатный ключ dadata.ru, 10 000 запросов в сутки: заменяет прямой
    # запрос к ЕГРЮЛ, который с рабочей машины часто не проходит.
    dadata_api_key: str = ""
    kontur_focus_key: str = ""
    spark_api_key: str = ""
    sbis_api_key: str = ""
    # Обязателен: анонимный запрос к OpenCorporates v0.4 отвечает 401.
    opencorporates_api_token: str = ""

    # --- санкционный скрининг ---
    # Пусто для своего yente рядом с сервисом; ключ — только для облачного API.
    opensanctions_api_key: str = ""
    # Перекрывает адрес из providers.yaml: в Docker сервер скрининга виден
    # как http://yente:8000, на хосте — как http://127.0.0.1:8001.
    opensanctions_base_url: str = ""
    # Сколько ждать ответа скрининга: свой yente отвечает за миллисекунды,
    # облачный — дольше.
    sanctions_timeout_s: float = 15.0
    # Порог совпадения имени, с которого запись считается попаданием.
    sanctions_match_threshold: float = 0.75

    chroma_persist_dir: Path = PROJECT_ROOT / "data" / "chroma"
    chroma_collection: str = "legal_norms"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    dense_search: bool = False

    aml_risk_threshold: int = 50
    trongrid_api_key: str = ""
    etherscan_api_key: str = ""

    upload_tmp_dir: Path = PROJECT_ROOT / "tmp" / "uploads"
    max_upload_mb: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
