import asyncpg
from config import DATABASE_URL

pool: asyncpg.Pool = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                balance NUMERIC(10, 2) DEFAULT 0,
                total_earned NUMERIC(10, 2) DEFAULT 0,
                referrer_id BIGINT,
                lang TEXT DEFAULT 'ru',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_tasks (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                reward NUMERIC(10, 2) NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_submissions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                task_id INT NOT NULL,
                screenshot_file_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                processed_by BIGINT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount NUMERIC(10, 2) NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
    print("✅ БД инициализирована")

# --- USERS ---
async def get_user(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

async def create_user(user_id: int, username: str, referrer_id: int = None):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, username, referrer_id) VALUES ($1, $2, $3) ON CONFLICT (user_id) DO NOTHING",
            user_id, username, referrer_id
        )

async def update_lang(user_id: int, lang: str):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET lang = $1 WHERE user_id = $2", lang, user_id)

async def add_balance(user_id: int, amount: float):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id = $2",
            amount, user_id
        )

# --- MANUAL TASKS ---
async def get_active_tasks():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM manual_tasks WHERE is_active = TRUE ORDER BY id")

async def get_task(task_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM manual_tasks WHERE id = $1", task_id)

async def create_task(title: str, desc: str, reward: float):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO manual_tasks (title, description, reward) VALUES ($1, $2, $3)",
            title, desc, reward
        )

async def delete_task(task_id: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE manual_tasks SET is_active = FALSE WHERE id = $1", task_id)

# --- SUBMISSIONS ---
async def has_pending_submission(user_id: int, task_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM manual_submissions WHERE user_id = $1 AND task_id = $2 AND status = 'pending'",
            user_id, task_id
        )
        return row is not None

async def has_recent_submission(user_id: int, task_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM manual_submissions WHERE user_id = $1 AND task_id = $2 AND created_at > NOW() - INTERVAL '24 hours'",
            user_id, task_id
        )
        return row is not None

async def create_submission(user_id: int, task_id: int, file_id: str) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO manual_submissions (user_id, task_id, screenshot_file_id) VALUES ($1, $2, $3) RETURNING id",
            user_id, task_id, file_id
        )
        return row['id']

async def get_submission(sub_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM manual_submissions WHERE id = $1", sub_id)

async def process_submission(sub_id: int, status: str, admin_id: int) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE manual_submissions SET status = $1, processed_by = $2 WHERE id = $3 AND status = 'pending'",
            status, admin_id, sub_id
        )
        return result == "UPDATE 1"

async def count_user_stats(user_id: int):
    async with pool.acquire() as conn:
        completed = await conn.fetchval(
            "SELECT COUNT(*) FROM manual_submissions WHERE user_id = $1 AND status = 'approved'", user_id
        )
        pending = await conn.fetchval(
            "SELECT COUNT(*) FROM manual_submissions WHERE user_id = $1 AND status = 'pending'", user_id
        )
        return completed, pending