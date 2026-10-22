import asyncpg
from config import DATABASE_URL, REFERRAL_BONUS

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
        
        # МИГРАЦИЯ: Добавляем новые колонки для рефералки (безопасно для существующих таблиц)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tasks_completed INT DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_paid BOOLEAN DEFAULT FALSE")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_tasks (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                link TEXT,
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_tasks (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                task_ref TEXT NOT NULL,
                reward NUMERIC(10, 2) NOT NULL,
                task_type TEXT DEFAULT 'piarflow',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
    print("✅ БД инициализирована (с поддержкой рефералки)")

# --- USERS ---
async def get_user(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

async def create_user(user_id: int, username: str, referrer_id: int = None):
    async with pool.acquire() as conn:
        # Защита от самого себя
        if referrer_id == user_id:
            referrer_id = None
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

async def deduct_balance(user_id: int, amount: float):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET balance = GREATEST(balance - $1, 0) WHERE user_id = $2",
            amount, user_id
        )

# --- REFERRALS ---
async def process_task_completion(user_id: int):
    """Увеличивает счетчик задач и проверяет, не пора ли платить рефереру."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET tasks_completed = tasks_completed + 1 WHERE user_id = $1", user_id)
        
        user = await conn.fetchrow("SELECT tasks_completed, referrer_id, referrer_paid FROM users WHERE user_id = $1", user_id)
        
        # Если выполнено 5+ задач, есть реферер и ему еще не платили
        if user['tasks_completed'] >= 5 and user['referrer_id'] and not user['referrer_paid']:
            await conn.execute("UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id = $2", REFERRAL_BONUS, user['referrer_id'])
            await conn.execute("UPDATE users SET referrer_paid = TRUE WHERE user_id = $1", user_id)
            return user['referrer_id']
        return None

async def get_ref_stats(user_id: int):
    async with pool.acquire() as conn:
        invited = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id = $1", user_id)
        earned = await conn.fetchval("SELECT COUNT(*) * $1 FROM users WHERE referrer_id = $2 AND referrer_paid = TRUE", REFERRAL_BONUS, user_id)
        return invited, float(earned or 0)

# --- MANUAL TASKS ---
async def get_active_tasks():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM manual_tasks WHERE is_active = TRUE ORDER BY id")

async def get_task(task_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM manual_tasks WHERE id = $1", task_id)

async def create_task(title: str, link: str, desc: str, reward: float):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO manual_tasks (title, link, description, reward) VALUES ($1, $2, $3, $4)",
            title, link, desc, reward
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

# --- USER TASKS (PiarFlow) ---
async def add_user_task(user_id: int, task_ref: str, reward: float):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_tasks (user_id, task_ref, reward) VALUES ($1, $2, $3)",
            user_id, task_ref, reward
        )

async def get_user_task(user_id: int, task_ref: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM user_tasks WHERE user_id = $1 AND task_ref = $2", user_id, task_ref)

# --- GLOBAL STATS ---
async def get_global_stats():
    async with pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_balance = await conn.fetchval("SELECT COALESCE(SUM(balance), 0) FROM users")
        total_earned = await conn.fetchval("SELECT COALESCE(SUM(total_earned), 0) FROM users")
        pending_subs = await conn.fetchval("SELECT COUNT(*) FROM manual_submissions WHERE status = 'pending'")
        return users_count, float(total_balance), float(total_earned), pending_subs