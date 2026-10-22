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

# Список админов через запятую: "766347597,8824689369"
ADMIN_IDS = [int(x.strip()) for x in _get("ADMIN_IDS").split(",")]
PORT = int(os.getenv("PORT", "8080"))