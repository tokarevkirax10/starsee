import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import BOT_TOKEN, PORT, ADMIN_ID
from database import init_db, get_user, create_user, update_lang
from locales import t

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class WithdrawState(StatesGroup):
    waiting_amount = State()

def get_main_menu(lang="ru"):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=t("btn_tasks", lang), callback_data="tasks"))
    builder.row(
        InlineKeyboardButton(text=t("btn_bonus", lang), callback_data="soon"),
        InlineKeyboardButton(text=t("btn_promo", lang), callback_data="soon")
    )
    builder.row(
        InlineKeyboardButton(text=t("btn_refs", lang), callback_data="soon"),
        InlineKeyboardButton(text=t("btn_profile", lang), callback_data="profile")
    )
    builder.row(
        InlineKeyboardButton(text=t("btn_withdraw", lang), callback_data="withdraw"),
        InlineKeyboardButton(text=t("btn_rules", lang), callback_data="rules")
    )
    builder.row(
        InlineKeyboardButton(text=t("btn_lang", lang), callback_data="lang"),
        InlineKeyboardButton(text=t("btn_about", lang), callback_data="about")
    )
    return builder.as_markup()

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await create_user(message.from_user.id, message.from_user.username)
    user = await get_user(message.from_user.id)
    lang = user['lang']
    
    text = f"{t('welcome', lang)}\n\n⭐️ StarMine\n\n{t('balance', lang)}: {user['balance']:.2f} ⭐️\n{t('need_tasks', lang)}\n\n{t('choose_action', lang)}"
    await message.answer(text, reply_markup=get_main_menu(lang))

@dp.callback_query(F.data == "profile")
async def profile(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    lang = user['lang']
    text = (
        f"{t('profile_title', lang)}\n\n"
        f"{t('profile_id', lang)}: {user['user_id']}\n"
        f"{t('profile_balance', lang)}: {user['balance']:.2f} ⭐️\n"
        f"{t('profile_completed', lang)}: 0\n" # Пока заглушка
        f"{t('profile_pending', lang)}: 0"    # Пока заглушка
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "lang")
async def change_lang(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    new_lang = "en" if user['lang'] == "ru" else "ru"
    await update_lang(callback.from_user.id, new_lang)
    await callback.answer(t("lang_changed", new_lang), show_alert=True)
    await callback.message.edit_text(
        f"⭐️ StarMine\n\n{t('balance', new_lang)}: {user['balance']:.2f} ⭐️\n\n{t('choose_action', new_lang)}",
        reply_markup=get_main_menu(new_lang)
    )

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    lang = user['lang']
    text = f"⭐️ StarMine\n\n{t('balance', lang)}: {user['balance']:.2f} ⭐️\n{t('need_tasks', lang)}\n\n{t('choose_action', lang)}"
    await callback.message.edit_text(text, reply_markup=get_main_menu(lang))
    await callback.answer()

# ===== Веб-сервер =====
async def handle_ping(request):
    return web.Response(text="Bot is alive")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Web server started on port {PORT}")

async def main():
    await init_db()
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())