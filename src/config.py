import os
from pathlib import Path
from dotenv import load_dotenv

# Загрузка .env
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _get_int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


# === Telegram ===
TELEGRAM_API_ID = _get_int("TELEGRAM_API_ID")
TELEGRAM_API_HASH = _get("TELEGRAM_API_HASH")
TELEGRAM_PHONE = _get("TELEGRAM_PHONE")
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_OWNER_ID = _get_int("TELEGRAM_OWNER_ID")

# === Telegram: второй аккаунт ===
TELEGRAM_API_ID_2 = _get_int("TELEGRAM_API_ID_2", 0)
TELEGRAM_API_HASH_2 = _get("TELEGRAM_API_HASH_2", "")
TELEGRAM_PHONE_2 = _get("TELEGRAM_PHONE_2", "")

# === Метки аккаунтов (для отображения в сводках) ===
ACCOUNT_LABEL_1 = _get("ACCOUNT_LABEL_1", "основной")  # акк с ботом
ACCOUNT_LABEL_2 = _get("ACCOUNT_LABEL_2", "9514")       # второй акк

# === PostgreSQL ===
DB_HOST = _get("DB_HOST", "localhost")
DB_PORT = _get_int("DB_PORT", 5432)
DB_NAME = _get("DB_NAME", "jarvis")
DB_USER = _get("DB_USER", "jarvis")
DB_PASSWORD = _get("DB_PASSWORD")

DB_DSN = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# === AI ===
AI_MODE_DEFAULT = _get("AI_MODE", "api")
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")

# === Whisper (Фаза 2) ===
OPENAI_API_KEY = _get("OPENAI_API_KEY")

# === Email (Фаза 2) ===
GMAIL_ADDRESS = _get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = _get("GMAIL_APP_PASSWORD")
MAILRU_ADDRESS = _get("MAILRU_ADDRESS")
MAILRU_APP_PASSWORD = _get("MAILRU_APP_PASSWORD")

# === Yandex.Disk (Фаза 3) ===
YADISK_OAUTH_TOKEN = _get("YADISK_OAUTH_TOKEN")

# === Proxy (для Telethon) ===
PROXY_TYPE = _get("PROXY_TYPE", "")       # socks5, socks4, http, или пусто (без прокси)
PROXY_HOST = _get("PROXY_HOST", "")
PROXY_PORT = _get_int("PROXY_PORT", 0)
PROXY_USERNAME = _get("PROXY_USERNAME", "")
PROXY_PASSWORD = _get("PROXY_PASSWORD", "")

# === Общие ===
TIMEZONE = _get("TIMEZONE", "Europe/Moscow")
LOG_LEVEL = _get("LOG_LEVEL", "INFO")

# === Пути ===
DATA_DIR = BASE_DIR / "data"
CALLS_INCOMING_DIR = DATA_DIR / "calls" / "incoming"
CALLS_PROCESSED_DIR = DATA_DIR / "calls" / "processed"
COLD_ARCHIVE_DIR = DATA_DIR / "cold"
LOGS_DIR = BASE_DIR / "logs"

# === Часовой пояс владельца ===
USER_TIMEZONE_OFFSET = 7            # UTC+7
USER_TIMEZONE_NAME = "Красноярск"

# === Диалог ===
CONVERSATION_WINDOW_SIZE = _get_int("CONVERSATION_WINDOW_SIZE", 20)

# === Константы ===
HEARTBEAT_INTERVAL_SEC = 300        # 5 минут
CONFIDENCE_HIGH = 80                # >80% — молча создаёт
CONFIDENCE_LOW = 50                 # <50% — молча в info
CONFIDENCE_BATCH_HOUR = 10          # 10:00 UTC = 17:00 Красноярск
CONFIDENCE_DAILY_LIMIT = 10         # макс вопросов/день
BRIEFING_HOUR = 2                   # 02:00 UTC = 09:00 Красноярск
DIGEST_HOUR = 14                    # 14:00 UTC = 21:00 Красноярск
WEEKLY_ANALYSIS_DAY = "sun"         # Воскресенье
WEEKLY_ANALYSIS_HOUR = 3            # 03:00 UTC = 10:00 Красноярск


def validate_config():
    """Проверяет обязательные переменные окружения при старте. Fail fast."""
    errors = []

    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN не задан")
    if not TELEGRAM_OWNER_ID:
        errors.append("TELEGRAM_OWNER_ID не задан")
    if not TELEGRAM_API_ID:
        errors.append("TELEGRAM_API_ID не задан")
    if not TELEGRAM_API_HASH:
        errors.append("TELEGRAM_API_HASH не задан")
    if not DB_PASSWORD:
        errors.append("DB_PASSWORD не задан")

    # API mode требует ключ
    if AI_MODE_DEFAULT == "api" and not ANTHROPIC_API_KEY:
        errors.append("AI_MODE=api, но ANTHROPIC_API_KEY не задан")

    if errors:
        raise RuntimeError(
            "Ошибки конфигурации (.env):\n" + "\n".join(f"  - {e}" for e in errors)
        )
