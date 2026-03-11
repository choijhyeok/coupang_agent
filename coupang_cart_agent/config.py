from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when required runtime configuration is missing."""


@dataclass(slots=True)
class AppConfig:
    telegram_bot_token: str
    coupang_username: str
    coupang_password: str
    coupang_login_url: str = "https://login.coupang.com/login/login.pang"
    coupang_cart_url: str = "https://cart.coupang.com/cartView.pang"
    default_currency: str = "KRW"
    cart_db_path: str = ".data/cart_results.sqlite3"
    coupang_browser_headless: bool = True
    coupang_storage_state_path: str = ".data/coupang-storage-state.json"
    coupang_browser_launch_mode: str = "browser_use"
    coupang_chrome_user_data_dir: str | None = None
    coupang_chrome_profile_directory: str = "Default"
    coupang_chrome_remote_debugging_port: int = 9223
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_deployment: str | None = None
    azure_openai_api_version: str = "2024-12-01-preview"
    postgres_dsn: str | None = None
    coupang_search_endpoint: str | None = None
    app_host: str = "127.0.0.1"
    app_port: int = 8080


def load_telegram_bot_token(
    env: dict[str, str] | None = None,
    *,
    dotenv_path: str | os.PathLike[str] = ".env",
) -> str:
    source = {
        **_load_dotenv_values(dotenv_path),
        **(dict(os.environ) if env is None else env),
    }
    token = source.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "Missing required configuration: TELEGRAM_BOT_TOKEN. "
            "Set it in the environment or .env before running."
        )
    return token


def _load_dotenv_values(dotenv_path: str | os.PathLike[str] = ".env") -> dict[str, str]:
    path = Path(dotenv_path)
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def load_config(
    env: dict[str, str] | None = None,
    *,
    dotenv_path: str | os.PathLike[str] = ".env",
) -> AppConfig:
    source = {
        **_load_dotenv_values(dotenv_path),
        **(dict(os.environ) if env is None else env),
    }
    missing = [
        name
        for name in ("TELEGRAM_BOT_TOKEN", "COUPANG_USERNAME", "COUPANG_PASSWORD")
        if not source.get(name)
    ]
    if missing:
        joined = ", ".join(missing)
        raise ConfigError(
            "Missing required configuration: "
            f"{joined}. Set them in the environment or .env before running."
        )

    return AppConfig(
        telegram_bot_token=source["TELEGRAM_BOT_TOKEN"],
        coupang_username=source["COUPANG_USERNAME"],
        coupang_password=source["COUPANG_PASSWORD"],
        coupang_login_url=source.get(
            "COUPANG_LOGIN_URL",
            "https://login.coupang.com/login/login.pang",
        ),
        coupang_cart_url=source.get(
            "COUPANG_CART_URL",
            "https://cart.coupang.com/cartView.pang",
        ),
        default_currency=source.get("DEFAULT_CURRENCY", "KRW"),
        cart_db_path=source.get("CART_DB_PATH", ".data/cart_results.sqlite3"),
        coupang_browser_headless=source.get("COUPANG_BROWSER_HEADLESS", "true").lower() != "false",
        coupang_storage_state_path=source.get(
            "COUPANG_STORAGE_STATE_PATH",
            ".data/coupang-storage-state.json",
        ),
        coupang_browser_launch_mode=source.get("COUPANG_BROWSER_LAUNCH_MODE", "browser_use"),
        coupang_chrome_user_data_dir=source.get("COUPANG_CHROME_USER_DATA_DIR") or None,
        coupang_chrome_profile_directory=source.get("COUPANG_CHROME_PROFILE_DIRECTORY", "Default"),
        coupang_chrome_remote_debugging_port=int(source.get("COUPANG_CHROME_REMOTE_DEBUGGING_PORT", "9223")),
        azure_openai_endpoint=source.get("AZURE_OPENAI_ENDPOINT") or None,
        azure_openai_api_key=source.get("AZURE_OPENAI_API_KEY") or None,
        azure_openai_deployment=source.get("AZURE_OPENAI_DEPLOYMENT") or None,
        azure_openai_api_version=source.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        postgres_dsn=source.get("POSTGRES_DSN") or source.get("DATABASE_URL") or None,
        coupang_search_endpoint=source.get("COUPANG_SEARCH_ENDPOINT") or None,
        app_host=source.get("APP_HOST", "127.0.0.1"),
        app_port=int(source.get("APP_PORT", "8080")),
    )
