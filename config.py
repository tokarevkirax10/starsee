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

# Награды и лимиты
PIARFLOW_REWARD = 0.15
REFERRAL_BONUS = 5.0
REFERRAL_TASKS_REQUIRED = 5
BOT_USERNAME = "StarsMineAi"
MIN_WITHDRAW = 15