import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest

from config import BOT_TOKEN, PORT, ADMIN_IDS, PIARFLOW_REWARD, REFERRAL_BONUS, REFERRAL_TASKS_REQUIRED, BOT_USERNAME, MIN_WITHDRAW, DAILY_BONUS, DAILY_TASKS_REQUIRED
from database import (
    init_db, get_user, create_user, update_lang, add_balance, deduct_balance,
    get_active_tasks, get_task, create_task, delete_task,
    has_pending_submission, has_recent_submission, create_submission,
    get_submission, process_submission, count_user_stats, get_global_stats,
    add_user_task, get_user_task, process_task_completion, get_ref_stats, get_all_user_ids,
    create_promo, activate_promo, claim_daily_bonus
)
from locales import t
import piarflow

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_pending_links = {}
last_known_tasks = set()

class AdminStates(StatesGroup):
    add_title = State()
    add_link = State()
    add_desc = State()
    add_reward = State()
    quick_add = State()
    broadcast = State()
    promo_word = State()
    promo_reward = State()

class WithdrawState(StatesGroup):
    waiting_amount = State()

class PromoState(StatesGroup):
    waiting_code = State()

# ===== МЕНЮ =====
def get_main_menu(lang="ru"):
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_tasks", lang), callback_data="tasks"))
    b.row(
        InlineKeyboardButton(text=t("btn_daily", lang), callback_data="daily"),
        InlineKeyboardButton(text=t("btn_promo", lang), callback_data="promo")
    )
    b.row(
        InlineKeyboardButton(text=t("btn_refs", lang), callback_data="refs"),
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
    lang = user['lang'] or "ru"
    return (
        f"⭐️ StarMine\n\n"
        f"{t('balance', lang)}: {float(user['balance']):.2f} ⭐️\n"
        f"{t('need_tasks', lang)}\n\n"
        f"{t('choose_action', lang)}"
    )

def parse_reward(text: str) -> float | None:
    text = text.strip().lower().replace(",", ".")
    replacements = {"десять": "10", "пять": "5", "два": "2", "один": "1", "двадцать": "20", "пятьдесят": "50", "сто": "100"}
    for word, num in replacements.items():
        if word in text: text = text.replace(word, num)
    try: return float(text)
    except ValueError: return None

@dp.message(CommandStart())
async def start_cmd(message: Message):
    args = message.text.split()
    referrer = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await create_user(message.from_user.id, message.from_user.username, referrer)
    user = await get_user(message.from_user.id)
    await message.answer(main_menu_text(user), reply_markup=get_main_menu(user['lang']))

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    lang = user['lang'] if user else "ru"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await message.answer("❌ Действие отменено.", reply_markup=b.as_markup())

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminStates.broadcast)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main"))
    await message.answer(t("broadcast_prompt", "ru"), reply_markup=b.as_markup())

@dp.message(AdminStates.broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    await state.clear()
    user_ids = await get_all_user_ids()
    status_msg = await message.answer(t("broadcast_started", "ru"))
    success = 0
    errors = 0
    
    for uid in user_ids:
        try:
            await message.copy_to(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await message.copy_to(chat_id=uid)
                success += 1
            except: errors += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            errors += 1
        except Exception:
            errors += 1
            
    await status_msg.edit_text(t("broadcast_finished", "ru").format(success, errors))

# ===== ЕЖЕДНЕВНЫЙ БОНУС (ЧЕК-ИН) =====
@dp.callback_query(F.data == "daily")
async def daily_bonus_handler(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    
    result = await claim_daily_bonus(user['user_id'])
    
    if result == "success":
        await cb.answer(t("daily_success", lang).format(DAILY_BONUS), show_alert=True)
        user = await get_user(cb.from_user.id)
        await cb.message.edit_text(main_menu_text(user), reply_markup=get_main_menu(lang))
    elif result == "already_claimed":
        await cb.answer(t("daily_already_claimed", lang), show_alert=True)
    elif result.startswith("not_enough_tasks:"):
        completed = result.split(":")[1]
        await cb.answer(t("daily_not_enough", lang).format(DAILY_TASKS_REQUIRED, completed, DAILY_TASKS_REQUIRED), show_alert=True)

# ===== ПРОМОКОДЫ (ПОЛЬЗОВАТЕЛЬ) =====
@dp.callback_query(F.data == "promo")
async def promo_menu(cb: types.CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    await state.set_state(PromoState.waiting_code)
    
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await cb.message.edit_text(t("promo_enter", lang), reply_markup=b.as_markup())
    await cb.answer()

@dp.message(PromoState.waiting_code)
async def process_promo(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = user['lang']
    code = message.text.strip().upper()
    
    status, reward = await activate_promo(code, user['user_id'])
    
    if status == "success":
        await message.answer(t("promo_success", lang).format(f"{reward:.2f}"))
    elif status == "used":
        await message.answer(t("promo_used", lang))
    elif status == "expired":
        await message.answer(t("promo_expired", lang))
    else:
        await message.answer(t("promo_not_found", lang))
    
    await state.clear()
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await message.answer(main_menu_text(await get_user(message.from_user.id)), reply_markup=get_main_menu(lang))

# ===== НАВИГАЦИЯ =====
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
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

# ===== РЕФЕРАЛЫ =====
@dp.callback_query(F.data == "refs")
async def refs_menu(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    link = f"https://t.me/{BOT_USERNAME}?start={user['user_id']}"
    invited, earned = await get_ref_stats(user['user_id'])
    text = (
        f"{t('ref_title', lang)}\n\n"
        f"{t('ref_desc', lang).format(REFERRAL_BONUS, REFERRAL_TASKS_REQUIRED)}\n\n"
        f"{t('ref_link', lang).format(link)}\n\n"
        f"{t('ref_stats', lang).format(invited, f'{earned:.2f}')}"
    )
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await cb.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")
    await cb.answer()

# ===== TASKS МЕНЮ =====
@dp.callback_query(F.data == "tasks")
async def tasks_menu(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("tasks_auto", lang), callback_data="tasks_auto"))
    b.row(InlineKeyboardButton(text=t("tasks_manual", lang), callback_data="tasks_manual"))
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await cb.message.edit_text(t("tasks_menu", lang), reply_markup=b.as_markup())
    await cb.answer()

# ===== АВТОМАТИЧЕСКИЕ ЗАДАНИЯ (PiarFlow) =====
def build_auto_keyboard(links, lang):
    b = InlineKeyboardBuilder()
    for i, link in enumerate(links):
        b.row(
            InlineKeyboardButton(text=t("auto_tasks_btn_go", lang), url=link),
            InlineKeyboardButton(text=t("auto_tasks_btn_check", lang), callback_data=f"auto_check_{i}")
        )
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="tasks"))
    return b.as_markup()

async def get_sorted_active_links(user_id):
    res = await piarflow.get_sponsors(user_id, user_id)
    sponsors = res.get("sponsors") if res and res.get("status") == "ok" else []
    active_links = []
    
    for s in sponsors:
        existing = await get_user_task(user_id, s["link"])
        if not existing:
            if "PiarFlowBot" in s["link"]:
                continue
            active_links.append(s["link"])
    
    return active_links

@dp.callback_query(F.data == "tasks_auto")
async def auto_tasks_list(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    active_links = await get_sorted_active_links(user['user_id'])
    
    if not active_links:
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="tasks"))
        await cb.message.edit_text(t("auto_tasks_empty", lang), reply_markup=b.as_markup())
        await cb.answer()
        return
        
    user_pending_links[user['user_id']] = active_links
    text = t("auto_tasks_ready", lang)
    keyboard = build_auto_keyboard(active_links, lang)
    try:
        await cb.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass
    await cb.answer()

@dp.callback_query(F.data.startswith("auto_check_"))
async def auto_tasks_check(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    links = user_pending_links.get(user['user_id'], [])
    if not links:
        await cb.answer("❌ Список заданий пуст. Обновляю...", show_alert=False)
        await auto_tasks_list(cb)
        return
    index = int(cb.data.split("_")[2])
    if index >= len(links):
        await cb.answer("❌ Ошибка. Обновляю список...", show_alert=False)
        await auto_tasks_list(cb)
        return
    link = links[index]
    res = await piarflow.check_sponsors(user['user_id'], [link])
    
    if not res or res.get("status") != "ok":
        await cb.answer("❌ Ошибка проверки. Попробуй позже.", show_alert=True)
        return
    sponsors = res.get("sponsors", [])
    if not sponsors:
        await cb.answer("❌ Задание не найдено.", show_alert=True)
        return
    status = sponsors[0].get("status")
    
    if status == "subscribed":
        existing = await get_user_task(user['user_id'], link)
        if existing:
            await cb.answer("✅ Ты уже получил награду за это задание.", show_alert=True)
        else:
            await add_user_task(user['user_id'], link, PIARFLOW_REWARD)
            await add_balance(user['user_id'], PIARFLOW_REWARD)
            referrer_id = await process_task_completion(user['user_id'])
            if referrer_id:
                ref_user = await get_user(referrer_id)
                try:
                    await bot.send_message(referrer_id, t("ref_bonus_received", ref_user['lang']).format(REFERRAL_TASKS_REQUIRED, REFERRAL_BONUS))
                except Exception:
                    pass
            await cb.answer(f"✅ Начислено {PIARFLOW_REWARD:.2f} ⭐️", show_alert=True)
            
        new_links = await get_sorted_active_links(user['user_id'])
        user_pending_links[user['user_id']] = new_links
        if new_links:
            try:
                await cb.message.edit_reply_markup(reply_markup=build_auto_keyboard(new_links, lang))
            except TelegramBadRequest:
                pass
        else:
            await cb.message.edit_text("✅ Все доступные задания выполнены! Возвращайся позже.")
            user_pending_links.pop(user['user_id'], None)
    elif status == "not_counted":
        await cb.answer("⚠️ Задание не засчитано.", show_alert=True)
    else:
        await cb.answer("❌ Задание не выполнено. Подпишись и попробуй снова.", show_alert=True)
        new_links = await get_sorted_active_links(user['user_id'])
        user_pending_links[user['user_id']] = new_links
        if new_links:
            try:
                await cb.message.edit_reply_markup(reply_markup=build_auto_keyboard(new_links, lang))
            except TelegramBadRequest:
                pass

# ===== ПРЕМИУМ ЗАДАНИЯ (для юзера) =====
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
    if task['link']:
        b.row(InlineKeyboardButton(text=t("manual_task_btn_go", lang), url=task['link']))
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
    if not task_id: return
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
    if cb.from_user.id not in ADMIN_IDS: return
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
    referrer_id = await process_task_completion(sub['user_id'])
    if referrer_id:
        ref_user = await get_user(referrer_id)
        try:
            await bot.send_message(referrer_id, t("ref_bonus_received", ref_user['lang']).format(REFERRAL_TASKS_REQUIRED, REFERRAL_BONUS))
        except Exception:
            pass
    user = await get_user(sub['user_id'])
    try:
        await bot.send_message(sub['user_id'], t("manual_task_approved", user['lang']).format(f"{float(task['reward']):.2f}"))
    except Exception: pass
    await cb.message.edit_caption(caption=f"{cb.message.caption}\n\n✅ Зачислено (админ: {cb.from_user.id})")
    await cb.answer("✅ Зачислено")

@dp.callback_query(F.data.startswith("sub_no_"))
async def sub_reject(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
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
    except Exception: pass
    await cb.message.edit_caption(caption=f"{cb.message.caption}\n\n❌ Отклонено (админ: {cb.from_user.id})")
    await cb.answer("❌ Отклонено")

# ===== АДМИН-ПАНЕЛЬ =====
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    user = await get_user(message.from_user.id)
    lang = user['lang'] if user else "ru"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast_info"))
    b.row(InlineKeyboardButton(text="🏷 Создать промокод", callback_data="admin_create_promo"))
    b.row(InlineKeyboardButton(text="⚡️ Добавить (1 сообщением)", callback_data="admin_quick_add"))
    b.row(InlineKeyboardButton(text=t("admin_add_task", lang), callback_data="admin_add"))
    b.row(InlineKeyboardButton(text=t("admin_del_task", lang), callback_data="admin_del_list"))
    await message.answer(t("admin_panel", lang), reply_markup=b.as_markup())

@dp.callback_query(F.data == "admin_broadcast_info")
async def admin_broadcast_info(cb: types.CallbackQuery):
    await cb.answer("Напиши команду /broadcast в чат", show_alert=True)

@dp.callback_query(F.data == "admin_create_promo")
async def admin_promo_start(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminStates.promo_word)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main"))
    await cb.message.edit_text(t("admin_promo_word", "ru"), reply_markup=b.as_markup())
    await cb.answer()

@dp.message(AdminStates.promo_word)
async def admin_promo_word(message: Message, state: FSMContext):
    await state.update_data(promo_word=message.text.strip().upper())
    await state.set_state(AdminStates.promo_reward)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main"))
    await message.answer(t("admin_promo_reward", "ru"), reply_markup=b.as_markup())

@dp.message(AdminStates.promo_reward)
async def admin_promo_reward(message: Message, state: FSMContext):
    reward = parse_reward(message.text)
    if reward is None:
        await message.answer("❌ Введи число, например 1.00")
        return
    data = await state.get_data()
    code = data['promo_word']
    success = await create_promo(code, reward)
    
    if success:
        await message.answer(t("admin_promo_created", "ru").format(code, f"{reward:.2f}"), parse_mode="Markdown")
    else:
        await message.answer(t("admin_promo_exists", "ru"))
    await state.clear()

@dp.callback_query(F.data == "admin_quick_add")
async def admin_quick_add(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminStates.quick_add)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main"))
    text = (
        "Отправь всё в одном сообщении, каждое с новой строки:\n\n"
        "1. Название\n"
        "2. Ссылка\n"
        "3. Описание\n"
        "4. Награда\n\n"
        "Пример:\n"
        "MinerXRealm\n"
        "https://t.me/MinerXRealmBot/app?startapp=ref_766347597\n"
        "Посмотрите 10 реклам\n"
        "5"
    )
    await cb.message.edit_text(text, reply_markup=b.as_markup())
    await cb.answer()

@dp.message(AdminStates.quick_add)
async def admin_quick_add_process(message: Message, state: FSMContext):
    lines = message.text.strip().split("\n")
    if len(lines) < 4:
        await message.answer("❌ Ошибка. Ты прислал меньше 4 строк. Попробуй снова.")
        return
    title = lines[0].strip()
    link = lines[1].strip()
    desc = lines[2].strip()
    reward_text = lines[3].strip()
    reward = parse_reward(reward_text)
    if reward is None:
        await message.answer("❌ Ошибка в награде. Напиши цифру в 4-й строке.")
        return
    await create_task(title, link, desc, reward)
    user = await get_user(message.from_user.id)
    await message.answer(t("admin_task_added", user['lang']))
    await state.clear()

@dp.callback_query(F.data == "admin_add")
async def admin_add_start(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS: return
    user = await get_user(cb.from_user.id)
    await state.set_state(AdminStates.add_title)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main"))
    await cb.message.edit_text(t("admin_enter_title", user['lang']), reply_markup=b.as_markup())
    await cb.answer()

@dp.message(AdminStates.add_title)
async def admin_add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    user = await get_user(message.from_user.id)
    await state.set_state(AdminStates.add_link)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main"))
    await message.answer(t("admin_enter_link", user['lang']), reply_markup=b.as_markup())

@dp.message(AdminStates.add_link)
async def admin_add_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text)
    user = await get_user(message.from_user.id)
    await state.set_state(AdminStates.add_desc)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main"))
    await message.answer(t("admin_enter_desc", user['lang']), reply_markup=b.as_markup())

@dp.message(AdminStates.add_desc)
async def admin_add_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text)
    user = await get_user(message.from_user.id)
    await state.set_state(AdminStates.add_reward)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main"))
    await message.answer(t("admin_enter_reward", user['lang']), reply_markup=b.as_markup())

@dp.message(AdminStates.add_reward)
async def admin_add_reward(message: Message, state: FSMContext):
    reward = parse_reward(message.text)
    if reward is None:
        await message.answer("❌ Не понял число. Введи цифрами, например: 5.00 или 10")
        return
    data = await state.get_data()
    await create_task(data['title'], data['link'], data['desc'], reward)
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

# ===== СТАТИСТИКА =====
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    user = await get_user(message.from_user.id)
    lang = user['lang'] if user else "ru"
    users, t_balance, t_earned, pending = await get_global_stats()
    text = t("admin_stats", lang).format(users, f"{t_balance:.2f}", f"{t_earned:.2f}", pending)
    await message.answer(text)

# ===== ВЫВОД =====
@dp.callback_query(F.data == "withdraw")
async def withdraw_menu(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("withdraw_15", lang), callback_data="wdraw_15"))
    b.row(InlineKeyboardButton(text=t("withdraw_custom", lang), callback_data="wdraw_custom"))
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await cb.message.edit_text(
        t("withdraw_menu", lang).format(f"{float(user['balance']):.2f}"),
        reply_markup=b.as_markup()
    )
    await cb.answer()

@dp.callback_query(F.data == "wdraw_15")
async def withdraw_15(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    lang = user['lang']
    if float(user['balance']) < MIN_WITHDRAW:
        await cb.answer(t("not_enough_balance", lang), show_alert=True)
        return
    await cb.answer(t("withdraw_request_sent", lang), show_alert=True)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"💸 Заявка на вывод: @{cb.from_user.username} (ID: {cb.from_user.id})\nСумма: {MIN_WITHDRAW} ⭐️\nБаланс: {float(user['balance']):.2f} ⭐️")
        except Exception: pass

@dp.callback_query(F.data == "wdraw_custom")
async def withdraw_custom(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(WithdrawState.waiting_amount)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main"))
    await cb.message.edit_text("Введи сумму (от 50):", reply_markup=b.as_markup())
    await cb.answer()

@dp.message(WithdrawState.waiting_amount)
async def withdraw_amount(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = user['lang']
    amount = parse_reward(message.text)
    if amount is None or amount < 50:
        await message.answer(t("invalid_amount", lang))
        return
    if float(user['balance']) < amount:
        await message.answer(t("not_enough_balance", lang))
        return
    await message.answer(t("withdraw_request_sent", lang))
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"💸 Заявка на вывод: @{message.from_user.username} (ID: {message.from_user.id})\nСумма: {amount:.2f} ⭐️\nБаланс: {float(user['balance']):.2f} ⭐️")
        except Exception: pass
    await state.clear()

# ===== ВЕБ-СЕРВЕР И ВЕБХУК PIARFLOW =====
async def handle_ping(request):
    return web.Response(text="Bot is alive")

async def handle_piarflow_webhook(request: web.Request):
    try:
        payload = await request.json()
        if payload.get("test"):
            return web.json_response({"ok": True})
        if payload.get("status") == "unsubscribed":
            tg_user_id = int(payload["tg_user_id"])
            offer_link = str(payload["offer_link"])
            task = await get_user_task(tg_user_id, offer_link)
            if task:
                reward_to_deduct = float(task['reward'])
                await deduct_balance(tg_user_id, reward_to_deduct)
                logging.info(f"Вебхук PiarFlow: Юзер {tg_user_id} отписался от {offer_link}, списано {reward_to_deduct} ⭐️")
                try:
                    await bot.send_message(tg_user_id, f"⚠️ Вы отписались от задания {offer_link}.\nС вашего баланса списано {reward_to_deduct:.2f} ⭐️.")
                except Exception:
                    pass
        return web.json_response({"ok": True})
    except Exception as e:
        logging.error(f"Ошибка вебхука PiarFlow: {e}")
        return web.json_response({"ok": False}, status=500)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_post('/piarflow/webhook', handle_piarflow_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Web server on {PORT}")

# ===== ФОНОВАЯ ПРОВЕРКА НОВЫХ ЗАДАНИЙ =====
async def background_task_checker():
    await asyncio.sleep(60)
    global last_known_tasks
    while True:
        try:
            users = await get_all_user_ids()
            if users:
                test_uid = users[0]
                res = await piarflow.get_sponsors(test_uid, test_uid)
                sponsors = res.get("sponsors") if res and res.get("status") == "ok" else []
                current_tasks = set([s["link"] for s in sponsors])
                
                if last_known_tasks and not current_tasks.issubset(last_known_tasks):
                    logging.info(f"Появились новые задания! Было {len(last_known_tasks)}, стало {len(current_tasks)}. Рассылка...")
                    for uid in users:
                        try:
                            await bot.send_message(uid, t("new_tasks_notification", "ru"), parse_mode="Markdown")
                            await asyncio.sleep(0.05)
                        except: pass
                
                last_known_tasks = current_tasks
                logging.info(f"Фоновая проверка: отслеживается {len(last_known_tasks)} заданий.")
        except Exception as e:
            logging.error(f"Ошибка в фоновой проверке: {e}")
        
        await asyncio.sleep(3600)

async def main():
    await init_db()
    await start_web_server()
    asyncio.create_task(background_task_checker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())