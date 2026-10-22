import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import BOT_TOKEN, PORT, ADMIN_IDS
from database import (
    init_db, get_user, create_user, update_lang, add_balance,
    get_active_tasks, get_task, create_task, delete_task,
    has_pending_submission, has_recent_submission, create_submission,
    get_submission, process_submission, count_user_stats
)
from locales import t

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class AdminStates(StatesGroup):
    add_title = State()
    add_desc = State()
    add_reward = State()

class WithdrawState(StatesGroup):
    waiting_amount = State()

# ===== МЕНЮ =====
def get_main_menu(lang="ru"):
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_tasks", lang), callback_data="tasks"))
    b.row(
        InlineKeyboardButton(text=t("btn_bonus", lang), callback_data="soon"),
        InlineKeyboardButton(text=t("btn_promo", lang), callback_data="soon")
    )
    b.row(
        InlineKeyboardButton(text=t("btn_refs", lang), callback_data="soon"),
        InlineKeyboardButton(text=t("btn_profile", lang), callback_data="profile")
    )
    b.row(
        InlineKeyboardButton(text=t("btn_withdraw", lang), callback_data="withdraw"),
        InlineKeyboardButton(text=t("btn_rules", lang), callback_data="rules")
    )
    b.row(
        InlineKeyboardButton(text=t("btn_lang", lang), callback_data="lang"),
        InlineKeyboardButton(text=t("btn_about", lang), callback_data="about")
    )
    return b.as_markup()

def main_menu_text(user):
    lang = user['lang']
    return (
        f"⭐️ StarMine\n\n"
        f"{t('balance', lang)}: {float(user['balance']):.2f} ⭐️\n"
        f"{t('need_tasks', lang)}\n\n"
        f"{t('choose_action', lang)}"
    )

# ===== START =====
@dp.message(CommandStart())
async def start_cmd(message: Message):
    args = message.text.split()
    referrer = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await create_user(message.from_user.id, message.from_user.username, referrer)
    user = await get_user(message.from_user.id)
    await message.answer(main_menu_text(user), reply_markup=get_main_menu(user['lang']))

# ===== НАВИГАЦИЯ =====
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    await cb.message.edit_text(main_menu_text(user), reply_markup=get_main_menu(user['lang']))
    await cb.answer()

@dp.callback_query(F.data == "soon")
async def soon(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    await cb.answer(t("soon", user['lang']), show_alert=True)

@dp.callback_query(F.data == "profile")
async def profile(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    completed, pending = await count_user_stats(user['user_id'])
    text = (
        f"{t('profile_title', lang)}\n\n"
        f"{t('profile_id', lang)}: {user['user_id']}\n"
        f"{t('profile_balance', lang)}: {float(user['balance']):.2f} ⭐️\n"
        f"{t('profile_completed', lang)}: {completed}\n"
        f"{t('profile_pending', lang)}: {pending}"
    )
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await cb.message.edit_text(text, reply_markup=b.as_markup())
    await cb.answer()

@dp.callback_query(F.data == "rules")
async def rules(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_back", user['lang']), callback_data="back_to_main"))
    await cb.message.edit_text(t("rules_text", user['lang']), reply_markup=b.as_markup())
    await cb.answer()

@dp.callback_query(F.data == "about")
async def about(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_back", user['lang']), callback_data="back_to_main"))
    await cb.message.edit_text(t("about_text", user['lang']), reply_markup=b.as_markup())
    await cb.answer()

@dp.callback_query(F.data == "lang")
async def change_lang(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    new_lang = "en" if user['lang'] == "ru" else "ru"
    await update_lang(cb.from_user.id, new_lang)
    user = await get_user(cb.from_user.id)
    await cb.answer(t("lang_changed", new_lang), show_alert=True)
    await cb.message.edit_text(main_menu_text(user), reply_markup=get_main_menu(new_lang))

# ===== TASKS МЕНЮ =====
@dp.callback_query(F.data == "tasks")
async def tasks_menu(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("tasks_auto", lang), callback_data="soon"))
    b.row(InlineKeyboardButton(text=t("tasks_manual", lang), callback_data="tasks_manual"))
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await cb.message.edit_text(t("tasks_menu", lang), reply_markup=b.as_markup())
    await cb.answer()

# ===== РУЧНЫЕ ЗАДАНИЯ (для юзера) =====
@dp.callback_query(F.data == "tasks_manual")
async def manual_list(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    tasks = await get_active_tasks()
    if not tasks:
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="tasks"))
        await cb.message.edit_text(t("manual_tasks_empty", lang), reply_markup=b.as_markup())
        await cb.answer()
        return
    
    b = InlineKeyboardBuilder()
    for task in tasks:
        b.row(InlineKeyboardButton(
            text=f"{task['title']} | +{float(task['reward']):.2f} ⭐️",
            callback_data=f"mtask_{task['id']}"
        ))
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="tasks"))
    await cb.message.edit_text(t("tasks_menu", lang), reply_markup=b.as_markup())
    await cb.answer()

@dp.callback_query(F.data.startswith("mtask_"))
async def manual_task_view(cb: types.CallbackQuery):
    task_id = int(cb.data.split("_")[1])
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    task = await get_task(task_id)
    if not task or not task['is_active']:
        await cb.answer("❌", show_alert=True)
        return
    
    pending = await has_pending_submission(user['user_id'], task_id)
    if pending:
        await cb.answer(t("manual_task_wait", lang), show_alert=True)
        return
    
    recent = await has_recent_submission(user['user_id'], task_id)
    if recent:
        await cb.answer("⏳ 1 скриншот за 24 часа", show_alert=True)
        return
    
    text = (
        f"{t('manual_task_title', lang)}\n\n"
        f"📝 {task['title']}\n"
        f"{task['description'] or ''}\n\n"
        f"💰 {t('manual_task_reward', lang)}: {float(task['reward']):.2f} ⭐️"
    )
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("manual_task_btn_done", lang), callback_data=f"mdone_{task_id}"))
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="tasks_manual"))
    await cb.message.edit_text(text, reply_markup=b.as_markup())
    await cb.answer()

@dp.callback_query(F.data.startswith("mdone_"))
async def manual_task_request_screenshot(cb: types.CallbackQuery, state: FSMContext):
    task_id = int(cb.data.split("_")[1])
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    await state.update_data(task_id=task_id)
    await state.set_state("waiting_screenshot")
    await cb.message.edit_text(t("manual_task_send_screenshot", lang))
    await cb.answer()

@dp.message(F.photo, F.text == None)
async def receive_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data.get("task_id")
    if not task_id:
        return
    
    user = await get_user(message.from_user.id)
    lang = user['lang']
    
    if await has_pending_submission(user['user_id'], task_id):
        await message.answer(t("manual_task_wait", lang))
        await state.clear()
        return
    if await has_recent_submission(user['user_id'], task_id):
        await message.answer("⏳ 1 скриншот за 24 часа")
        await state.clear()
        return
    
    file_id = message.photo[-1].file_id
    sub_id = await create_submission(user['user_id'], task_id, file_id)
    task = await get_task(task_id)
    
    await message.answer(t("manual_task_sent", lang))
    await state.clear()
    
    for admin_id in ADMIN_IDS:
        try:
            kb = InlineKeyboardBuilder()
            kb.row(
                InlineKeyboardButton(text=t("admin_approve", lang), callback_data=f"sub_ok_{sub_id}"),
                InlineKeyboardButton(text=t("admin_reject", lang), callback_data=f"sub_no_{sub_id}")
            )
            caption = t("admin_new_submission", lang).format(
                message.from_user.username or "—", user['user_id'], task['title'], f"{float(task['reward']):.2f}"
            )
            await bot.send_photo(admin_id, photo=file_id, caption=caption, reply_markup=kb.as_markup())
        except Exception as e:
            logging.error(f"Не смог отправить админу {admin_id}: {e}")

# ===== ОБРАБОТКА АДМИНОМ =====
@dp.callback_query(F.data.startswith("sub_ok_"))
async def sub_approve(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    sub_id = int(cb.data.split("_")[2])
    sub = await get_submission(sub_id)
    if not sub or sub['status'] != 'pending':
        await cb.answer(t("admin_already_processed"), show_alert=True)
        return
    
    ok = await process_submission(sub_id, "approved", cb.from_user.id)
    if not ok:
        await cb.answer(t("admin_already_processed"), show_alert=True)
        return
    
    task = await get_task(sub['task_id'])
    await add_balance(sub['user_id'], float(task['reward']))
    
    user = await get_user(sub['user_id'])
    try:
        await bot.send_message(sub['user_id'], t("manual_task_approved", user['lang']).format(f"{float(task['reward']):.2f}"))
    except Exception:
        pass
    
    await cb.message.edit_caption(caption=f"{cb.message.caption}\n\n✅ Зачислено (админ: {cb.from_user.id})")
    await cb.answer("✅ Зачислено")

@dp.callback_query(F.data.startswith("sub_no_"))
async def sub_reject(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    sub_id = int(cb.data.split("_")[2])
    sub = await get_submission(sub_id)
    if not sub or sub['status'] != 'pending':
        await cb.answer(t("admin_already_processed"), show_alert=True)
        return
    
    ok = await process_submission(sub_id, "rejected", cb.from_user.id)
    if not ok:
        await cb.answer(t("admin_already_processed"), show_alert=True)
        return
    
    user = await get_user(sub['user_id'])
    try:
        await bot.send_message(sub['user_id'], t("manual_task_rejected", user['lang']))
    except Exception:
        pass
    
    await cb.message.edit_caption(caption=f"{cb.message.caption}\n\n❌ Отклонено (админ: {cb.from_user.id})")
    await cb.answer("❌ Отклонено")

# ===== АДМИН-ПАНЕЛЬ =====
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    user = await get_user(message.from_user.id)
    lang = user['lang'] if user else "ru"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("admin_add_task", lang), callback_data="admin_add"))
    b.row(InlineKeyboardButton(text=t("admin_del_task", lang), callback_data="admin_del_list"))
    await message.answer(t("admin_panel", lang), reply_markup=b.as_markup())

@dp.callback_query(F.data == "admin_add")
async def admin_add_start(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS: return
    user = await get_user(cb.from_user.id)
    await state.set_state(AdminStates.add_title)
    await cb.message.edit_text(t("admin_enter_title", user['lang']))
    await cb.answer()

@dp.message(AdminStates.add_title)
async def admin_add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    user = await get_user(message.from_user.id)
    await state.set_state(AdminStates.add_desc)
    await message.answer(t("admin_enter_desc", user['lang']))

@dp.message(AdminStates.add_desc)
async def admin_add_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text)
    user = await get_user(message.from_user.id)
    await state.set_state(AdminStates.add_reward)
    await message.answer(t("admin_enter_reward", user['lang']))

@dp.message(AdminStates.add_reward)
async def admin_add_reward(message: Message, state: FSMContext):
    try:
        reward = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введи число, например 5.00")
        return
    data = await state.get_data()
    await create_task(data['title'], data['desc'], reward)
    user = await get_user(message.from_user.id)
    await message.answer(t("admin_task_added", user['lang']))
    await state.clear()

@dp.callback_query(F.data == "admin_del_list")
async def admin_del_list(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    tasks = await get_active_tasks()
    if not tasks:
        await cb.answer(t("admin_no_tasks", lang), show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for task in tasks:
        b.row(InlineKeyboardButton(
            text=f"❌ {task['title']} ({float(task['reward']):.2f} ⭐️)",
            callback_data=f"admin_del_{task['id']}"
        ))
    await cb.message.edit_text(t("admin_del_task", lang), reply_markup=b.as_markup())
    await cb.answer()

@dp.callback_query(F.data.startswith("admin_del_"))
async def admin_do_delete(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    task_id = int(cb.data.split("_")[2])
    await delete_task(task_id)
    user = await get_user(cb.from_user.id)
    await cb.answer(t("admin_task_deleted", user['lang']), show_alert=True)
    await cb.message.delete()

# ===== ВЫВОД =====
@dp.callback_query(F.data == "withdraw")
async def withdraw_menu(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("withdraw_25", lang), callback_data="wdraw_25"))
    b.row(InlineKeyboardButton(text=t("withdraw_custom", lang), callback_data="wdraw_custom"))
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await cb.message.edit_text(
        f"{t('withdraw_menu', lang)}\n\n{t('profile_balance', lang)}: {float(user['balance']):.2f} ⭐️",
        reply_markup=b.as_markup()
    )
    await cb.answer()

@dp.callback_query(F.data == "wdraw_25")
async def withdraw_25(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    if float(user['balance']) < 25:
        await cb.answer(t("not_enough_balance", lang), show_alert=True)
        return
    await cb.answer(t("withdraw_request_sent", lang), show_alert=True)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"💸 Заявка на вывод: @{cb.from_user.username} (ID: {cb.from_user.id})\nСумма: 25 ⭐️\nБаланс: {float(user['balance']):.2f} ⭐️")
        except Exception:
            pass

@dp.callback_query(F.data == "wdraw_custom")
async def withdraw_custom(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(WithdrawState.waiting_amount)
    await cb.message.edit_text("Введи сумму (от 50):")
    await cb.answer()

@dp.message(WithdrawState.waiting_amount)
async def withdraw_amount(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = user['lang']
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer(t("invalid_amount", lang))
        return
    if amount < 50:
        await message.answer(t("invalid_amount", lang))
        return
    if float(user['balance']) < amount:
        await message.answer(t("not_enough_balance", lang))
        return
    await message.answer(t("withdraw_request_sent", lang))
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"💸 Заявка на вывод: @{message.from_user.username} (ID: {message.from_user.id})\nСумма: {amount:.2f} ⭐️\nБаланс: {float(user['balance']):.2f} ⭐️")
        except Exception:
            pass
    await state.clear()

# ===== ВЕБ-СЕРВЕР =====
async def handle_ping(request):
    return web.Response(text="Bot is alive")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Web server on {PORT}")

async def main():
    await init_db()
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())