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
ADMIN_ID = int(_get("ADMIN_ID"))
PORT = int(os.getenv("PORT", "8080"))