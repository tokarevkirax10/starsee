import os
from dotenv import load_dotenv

load_dotenv()

def _get(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"❌ Не задана переменная окружения: {name}")
    return val

BOT_TOKEN = _get("BOT_TOKEN")
PIARFLOW_API_KEY = _get("PIARFLOW_API_KEY")
DATABASE_URL = _get("DATABASE_URL")

ADMIN_IDS = [int(x.strip()) for x in _get("ADMIN_IDS").split(",")]
PORT = int(os.getenv("PORT", "8080"))

# Награда за ОДНО автоматическое задание PiarFlow (внутренние звезды)
PIARFLOW_REWARD = 0.10
# Бонус рефереру
REFERRAL_BONUS = 5.0
# Сколько заданий должен выполнить реферал, чтобы реферер получил бонус
REFERRAL_TASKS_REQUIRED = 5
# Имя бота для реферальных ссылок
BOT_USERNAME = "StarsMineAi"