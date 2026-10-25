import aiohttp
import logging
from config import PIARFLOW_API_KEY

API_URL = "https://piarflow.com/v1"

async def get_sponsors(user_id: int, chat_id: int, max_sponsors: int = 5):
    headers = {
        "Authorization": f"Bearer {PIARFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"user_id": user_id, "chat_id": chat_id, "max_sponsors": max_sponsors}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{API_URL}/sponsors", json=payload, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 404:
                    # Фикс: Если PiarFlow говорит "Нет подходящих спонсоров", возвращаем пустой список
                    return {"status": "ok", "sponsors": []}
                else:
                    logging.error(f"PiarFlow Get Sponsors Error: {resp.status} {await resp.text()}")
                    return None
    except Exception as e:
        logging.error(f"PiarFlow Connection Error: {e}")
        return None

async def check_sponsors(user_id: int, links: list[str]):
    headers = {
        "Authorization": f"Bearer {PIARFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"user_id": user_id, "links": links}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{API_URL}/sponsors/check", json=payload, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 404:
                    # Если задание не найдено (уже выполнено или сгорело)
                    return {"status": "ok", "sponsors": []}
                else:
                    logging.error(f"PiarFlow Check Error: {resp.status} {await resp.text()}")
                    return None
    except Exception as e:
        logging.error(f"PiarFlow Connection Error: {e}")
        return None