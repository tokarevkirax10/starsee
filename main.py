import asyncio, logging, hmac, hashlib, json
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest

from config import (BOT_TOKEN, PORT, ADMIN_IDS, PIARFLOW_REWARD, REFERRAL_BONUS,
    REFERRAL_TASKS_REQUIRED, BOT_USERNAME, MIN_WITHDRAW, DAILY_BONUS, DAILY_TASKS_REQUIRED,
    TONADS_PROJECT_ID, TONADS_SECRET, TONADS_REWARD)
from database import (
    init_db, get_user, get_user_by_username, create_user, update_lang,
    add_balance, add_balance_admin, deduct_balance,
    get_active_tasks, get_task, create_task, delete_task,
    has_any_submission, create_submission, get_submission, process_submission,
    count_user_stats, get_global_stats, add_user_task, get_user_task,
    process_task_completion, get_ref_stats, get_all_user_ids,
    create_promo, activate_promo, claim_daily_bonus,
    log_admin_give, get_top_users,
    is_tonads_event_processed, mark_tonads_event_processed
)
from locales import t
import piarflow

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
user_pending_links = {}
last_known_tasks = set()

class AdminStates(StatesGroup):
    add_title = State(); add_link = State(); add_desc = State(); add_reward = State()
    quick_add = State(); broadcast = State(); promo_word = State(); promo_reward = State()
    give_user = State(); give_amount = State(); give_reason = State()

class WithdrawState(StatesGroup):
    waiting_amount = State()

class PromoState(StatesGroup):
    waiting_code = State()

def is_admin(uid): return uid in ADMIN_IDS

def parse_reward(text):
    text = text.strip().lower().replace(",", ".")
    for w, n in {"десять":"10","пять":"5","два":"2","один":"1","двадцать":"20","пятьдесят":"50","сто":"100"}.items():
        if w in text: text = text.replace(w, n)
    try: return float(text)
    except ValueError: return None

def back_kb(lang="ru", cb="back_to_main"):
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data=cb))
    return b.as_markup()

# ===== МЕНЮ =====
def get_main_menu(lang="ru"):
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_tasks", lang), callback_data="tasks"))
    b.row(InlineKeyboardButton(text=t("btn_daily", lang), callback_data="daily"),
          InlineKeyboardButton(text=t("btn_promo", lang), callback_data="promo"))
    b.row(InlineKeyboardButton(text=t("btn_refs", lang), callback_data="refs"),
          InlineKeyboardButton(text=t("btn_profile", lang), callback_data="profile"))
    b.row(InlineKeyboardButton(text=t("btn_withdraw", lang), callback_data="withdraw"),
          InlineKeyboardButton(text=t("btn_rules", lang), callback_data="rules"))
    b.row(InlineKeyboardButton(text=t("btn_lang", lang), callback_data="lang"),
          InlineKeyboardButton(text=t("btn_about", lang), callback_data="about"))
    return b.as_markup()

def main_menu_text(user):
    lang = user['lang'] or "ru"
    return (f"⭐️ StarMine\n\n{t('balance', lang)}: {float(user['balance']):.2f} ⭐️\n"
            f"{t('need_tasks', lang)}\n\n{t('choose_action', lang)}")

# ===== START / CANCEL / BROADCAST =====
@dp.message(CommandStart())
async def start_cmd(message: Message):
    args = message.text.split()
    ref = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await create_user(message.from_user.id, message.from_user.username, ref)
    user = await get_user(message.from_user.id)
    await message.answer(main_menu_text(user), reply_markup=get_main_menu(user['lang']))

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    await message.answer("❌ Действие отменено.", reply_markup=back_kb(user['lang'] if user else "ru"))

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(AdminStates.broadcast)
    await message.answer(t("broadcast_prompt", "ru"), reply_markup=back_kb())

@dp.message(AdminStates.broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    await state.clear()
    uids = await get_all_user_ids()
    status = await message.answer(t("broadcast_started", "ru"))
    ok = err = 0
    for uid in uids:
        try:
            await message.copy_to(chat_id=uid); ok += 1; await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try: await message.copy_to(chat_id=uid); ok += 1
            except Exception: err += 1
        except Exception: err += 1
    await status.edit_text(t("broadcast_finished", "ru").format(ok, err))

# ===== ДЕЙЛИ =====
@dp.callback_query(F.data == "daily")
async def daily_bonus_handler(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id); lang = user['lang']
    r = await claim_daily_bonus(user['user_id'])
    if r == "success":
        await cb.answer(t("daily_success", lang).format(DAILY_BONUS), show_alert=True)
        user = await get_user(cb.from_user.id)
        await cb.message.edit_text(main_menu_text(user), reply_markup=get_main_menu(lang))
    elif r == "already_claimed":
        await cb.answer(t("daily_already_claimed", lang), show_alert=True)
    elif r.startswith("not_enough_tasks:"):
        c = r.split(":")[1]
        await cb.answer(t("daily_not_enough", lang).format(DAILY_TASKS_REQUIRED, c, DAILY_TASKS_REQUIRED), show_alert=True)

# ===== ПРОМОКОД =====
@dp.callback_query(F.data == "promo")
async def promo_menu(cb: types.CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id); lang = user['lang']
    await state.set_state(PromoState.waiting_code)
    await cb.message.edit_text(t("promo_enter", lang), reply_markup=back_kb(lang))
    await cb.answer()

@dp.message(PromoState.waiting_code)
async def process_promo(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id); lang = user['lang']
    status, reward = await activate_promo(message.text.strip().upper(), user['user_id'])
    if status == "success": await message.answer(t("promo_success", lang).format(f"{reward:.2f}"))
    elif status == "used": await message.answer(t("promo_used", lang))
    elif status == "expired": await message.answer(t("promo_expired", lang))
    else: await message.answer(t("promo_not_found", lang))
    await state.clear()
    user = await get_user(message.from_user.id)
    await message.answer(main_menu_text(user), reply_markup=get_main_menu(lang))

# ===== НАВИГАЦИЯ =====
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user(cb.from_user.id)
    await cb.message.edit_text(main_menu_text(user), reply_markup=get_main_menu(user['lang']))
    await cb.answer()

@dp.callback_query(F.data == "profile")
async def profile(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id); lang = user['lang']
    done, pend = await count_user_stats(user['user_id'])
    text = (f"{t('profile_title', lang)}\n\n"
            f"{t('profile_id', lang)}: {user['user_id']}\n"
            f"{t('profile_balance', lang)}: {float(user['balance']):.2f} ⭐️\n"
            f"{t('profile_completed', lang)}: {done}\n"
            f"{t('profile_pending', lang)}: {pend}")
    await cb.message.edit_text(text, reply_markup=back_kb(lang))
    await cb.answer()

@dp.callback_query(F.data == "rules")
async def rules(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    await cb.message.edit_text(t("rules_text", user['lang']), reply_markup=back_kb(user['lang']))
    await cb.answer()

@dp.callback_query(F.data == "about")
async def about(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    await cb.message.edit_text(t("about_text", user['lang']), reply_markup=back_kb(user['lang']))
    await cb.answer()

@dp.callback_query(F.data == "lang")
async def change_lang(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id)
    nl = "en" if user['lang'] == "ru" else "ru"
    await update_lang(cb.from_user.id, nl)
    user = await get_user(cb.from_user.id)
    await cb.answer(t("lang_changed", nl), show_alert=True)
    await cb.message.edit_text(main_menu_text(user), reply_markup=get_main_menu(nl))

# ===== РЕФЕРАЛЫ =====
@dp.callback_query(F.data == "refs")
async def refs_menu(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id); lang = user['lang']
    link = f"https://t.me/{BOT_USERNAME}?start={user['user_id']}"
    invited, earned = await get_ref_stats(user['user_id'])
    text = (f"{t('ref_title', lang)}\n\n"
            f"{t('ref_desc', lang).format(REFERRAL_BONUS, REFERRAL_TASKS_REQUIRED)}\n\n"
            f"{t('ref_link', lang).format(link)}\n\n"
            f"{t('ref_stats', lang).format(invited, f'{earned:.2f}')}")
    await cb.message.edit_text(text, reply_markup=back_kb(lang), parse_mode="Markdown")
    await cb.answer()

# ===== TASKS МЕНЮ =====
@dp.callback_query(F.data == "tasks")
async def tasks_menu(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id); lang = user['lang']
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("tasks_auto", lang), callback_data="tasks_auto"))
    b.row(InlineKeyboardButton(text=t("tasks_manual", lang), callback_data="tasks_manual"))
    
    # --- НОВАЯ КНОПКА TONADS ---
    tonads_link = f"https://t.me/tonads_bot/app?startapp=p{TONADS_PROJECT_ID}_{user['user_id']}"
    b.row(InlineKeyboardButton(text="📺 Смотреть рекламу (TonAds)", url=tonads_link))
    # ---------------------------

    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await cb.message.edit_text(t("tasks_menu", lang), reply_markup=b.as_markup())
    await cb.answer()

# ===== АВТО-ЗАДАНИЯ =====
def build_auto_keyboard(links, lang):
    b = InlineKeyboardBuilder()
    for i, link in enumerate(links):
        b.row(InlineKeyboardButton(text=t("auto_tasks_btn_go", lang), url=link),
              InlineKeyboardButton(text=t("auto_tasks_btn_check", lang), callback_data=f"auto_check_{i}"))
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="tasks"))
    return b.as_markup()

async def get_sorted_active_links(uid):
    res = await piarflow.get_sponsors(uid, uid)
    sps = res.get("sponsors") if res and res.get("status") == "ok" else []
    out = []
    for s in sps:
        if await get_user_task(uid, s["link"]): continue
        if "PiarFlowBot" in s["link"]: continue
        out.append(s["link"])
    return out

@dp.callback_query(F.data == "tasks_auto")
async def auto_tasks_list(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id); lang = user['lang']
    links = await get_sorted_active_links(user['user_id'])
    if not links:
        await cb.message.edit_text(t("auto_tasks_empty", lang), reply_markup=back_kb(lang, "tasks"))
        await cb.answer(); return
    user_pending_links[user['user_id']] = links
    try: await cb.message.edit_text(t("auto_tasks_ready", lang), reply_markup=build_auto_keyboard(links, lang))
    except TelegramBadRequest: pass
    await cb.answer()

@dp.callback_query(F.data.startswith("auto_check_"))
async def auto_tasks_check(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id); lang = user['lang']
    links = user_pending_links.get(user['user_id'], [])
    idx = int(cb.data.split("_")[2])
    if not links or idx >= len(links):
        await cb.answer("❌ Обновляю список...", show_alert=False)
        await auto_tasks_list(cb); return
    link = links[idx]
    res = await piarflow.check_sponsors(user['user_id'], [link])
    if not res or res.get("status") != "ok":
        await cb.answer("❌ Ошибка проверки.", show_alert=True); return
    sps = res.get("sponsors", [])
    if not sps:
        await cb.answer("❌ Задание не найдено.", show_alert=True); return
    st = sps[0].get("status")

    if st == "subscribed":
        if not await get_user_task(user['user_id'], link):
            await add_user_task(user['user_id'], link, PIARFLOW_REWARD)
            await add_balance(user['user_id'], PIARFLOW_REWARD)
            rid = await process_task_completion(user['user_id'])
            if rid:
                ru = await get_user(rid)
                try: await bot.send_message(rid, t("ref_bonus_received", ru['lang']).format(REFERRAL_TASKS_REQUIRED, REFERRAL_BONUS))
                except Exception: pass
            await cb.answer(f"✅ Начислено {PIARFLOW_REWARD:.2f} ⭐️", show_alert=True)
        else:
            await cb.answer("✅ Уже получено.", show_alert=True)
        nl = await get_sorted_active_links(user['user_id'])
        user_pending_links[user['user_id']] = nl
        if nl:
            try: await cb.message.edit_reply_markup(reply_markup=build_auto_keyboard(nl, lang))
            except TelegramBadRequest: pass
        else:
            await cb.message.edit_text("✅ Все доступные задания выполнены!")
            user_pending_links.pop(user['user_id'], None)
    elif st == "not_counted":
        await cb.answer("⚠️ Не засчитано.", show_alert=True)
    else:
        await cb.answer("❌ Не выполнено.", show_alert=True)
        nl = await get_sorted_active_links(user['user_id'])
        user_pending_links[user['user_id']] = nl
        if nl:
            try: await cb.message.edit_reply_markup(reply_markup=build_auto_keyboard(nl, lang))
            except TelegramBadRequest: pass

# ===== ПРЕМИУМ-ЗАДАНИЯ =====
@dp.callback_query(F.data == "tasks_manual")
async def manual_list(cb: types.CallbackQuery):
    user = await get_user(cb.from_user.id); lang = user['lang']
    tasks = await get_active_tasks()
    if not tasks:
        await cb.message.edit_text(t("manual_tasks_empty", lang), reply_markup=back_kb(lang, "tasks"))
        await cb.answer(); return
    b = InlineKeyboardBuilder()
    for task in tasks:
        b.row(InlineKeyboardButton(text=f"{task['title']} | +{float(task['reward']):.2f} ⭐️",
                                   callback_data=f"mtask_{task['id']}"))
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="tasks"))
    await cb.message.edit_text(t("tasks_menu", lang), reply_markup=b.as_markup())
    await cb.answer()

@dp.callback_query(F.data.startswith("mtask_"))
async def manual_task_view(cb: types.CallbackQuery):
    tid = int(cb.data.split("_")[1])
    user = await get_user(cb.from_user.id); lang = user['lang']
    task = await get_task(tid)
    if not task or not task['is_active']:
        await cb.answer("❌", show_alert=True); return
    if await has_any_submission(user['user_id'], tid):
        await cb.answer(t("manual_task_already_done", lang), show_alert=True); return
    text = (f"{t('manual_task_title', lang)}\n\n📝 {task['title']}\n"
            f"{task['description'] or ''}\n\n"
            f"💰 {t('manual_task_reward', lang)}: {float(task['reward']):.2f} ⭐️")
    b = InlineKeyboardBuilder()
    if task['link']:
        b.row(InlineKeyboardButton(text=t("manual_task_btn_go", lang), url=task['link']))
    b.row(InlineKeyboardButton(text=t("manual_task_btn_done", lang), callback_data=f"mdone_{tid}"))
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="tasks_manual"))
    await cb.message.edit_text(text, reply_markup=b.as_markup())
    await cb.answer()

@dp.callback_query(F.data.startswith("mdone_"))
async def manual_task_request_screenshot(cb: types.CallbackQuery, state: FSMContext):
    tid = int(cb.data.split("_")[1])
    user = await get_user(cb.from_user.id); lang = user['lang']
    if await has_any_submission(user['user_id'], tid):
        await cb.answer(t("manual_task_already_done", lang), show_alert=True); return
    await state.update_data(task_id=tid)
    await state.set_state("waiting_screenshot")
    await cb.message.edit_text(t("manual_task_send_screenshot", lang))
    await cb.answer()

@dp.message(F.photo, F.text == None)
async def receive_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("task_id")
    if not tid: return
    user = await get_user(message.from_user.id); lang = user['lang']
    if await has_any_submission(user['user_id'], tid):
        await message.answer(t("manual_task_already_done", lang))
        await state.clear(); return
    file_id = message.photo[-1].file_id
    sub_id = await create_submission(user['user_id'], tid, file_id)
    task = await get_task(tid)
    await message.answer(t("manual_task_sent", lang))
    await state.clear()
    for aid in ADMIN_IDS:
        try:
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text=t("admin_approve", lang), callback_data=f"sub_ok_{sub_id}"),
                   InlineKeyboardButton(text=t("admin_reject", lang), callback_data=f"sub_no_{sub_id}"))
            caption = t("admin_new_submission", lang).format(
                message.from_user.username or "—", user['user_id'], task['title'], f"{float(task['reward']):.2f}")
            await bot.send_photo(aid, photo=file_id, caption=caption, reply_markup=kb.as_markup())
        except Exception as e:
            logging.error(f"admin {aid}: {e}")

@dp.callback_query(F.data.startswith("sub_ok_"))
async def sub_approve(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id): return
    sub_id = int(cb.data.split("_")[2])
    sub = await get_submission(sub_id)
    if not sub or sub['status'] != 'pending':
        await cb.answer(t("admin_already_processed"), show_alert=True); return
    if not await process_submission(sub_id, "approved", cb.from_user.id):
        await cb.answer(t("admin_already_processed"), show_alert=True); return
    task = await get_task(sub['task_id'])
    await add_balance(sub['user_id'], float(task['reward']))
    rid = await process_task_completion(sub['user_id'])
    if rid:
        ru = await get_user(rid)
        try: await bot.send_message(rid, t("ref_bonus_received", ru['lang']).format(REFERRAL_TASKS_REQUIRED, REFERRAL_BONUS))
        except Exception: pass
    u = await get_user(sub['user_id'])
    try: await bot.send_message(sub['user_id'], t("manual_task_approved", u['lang']).format(f"{float(task['reward']):.2f}"))
    except Exception: pass
    await cb.message.edit_caption(caption=f"{cb.message.caption}\n\n✅ Зачислено (админ: {cb.from_user.id})")
    await cb.answer("✅ Зачислено")

@dp.callback_query(F.data.startswith("sub_no_"))
async def sub_reject(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id): return
    sub_id = int(cb.data.split("_")[2])
    sub = await get_submission(sub_id)
    if not sub or sub['status'] != 'pending':
        await cb.answer(t("admin_already_processed"), show_alert=True); return
    if not await process_submission(sub_id, "rejected", cb.from_user.id):
        await cb.answer(t("admin_already_processed"), show_alert=True); return
    u = await get_user(sub['user_id'])
    try: await bot.send_message(sub['user_id'], t("manual_task_rejected", u['lang']))
    except Exception: pass
    await cb.message.edit_caption(caption=f"{cb.message.caption}\n\n❌ Отклонено (админ: {cb.from_user.id})")
    await cb.answer("❌ Отклонено")

# ===== АДМИН-ПАНЕЛЬ =====
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id): return
    u = await get_user(message.from_user.id); lang = u['lang'] if u else "ru"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast_info"))
    b.row(InlineKeyboardButton(text="🏷 Создать промокод", callback_data="admin_create_promo"))
    b.row(InlineKeyboardButton(text="⚡️ Добавить (1 сообщением)", callback_data="admin_quick_add"))
    b.row(InlineKeyboardButton(text=t("admin_add_task", lang), callback_data="admin_add"))
    b.row(InlineKeyboardButton(text=t("admin_del_task", lang), callback_data="admin_del_list"))
    b.row(InlineKeyboardButton(text=t("admin_give_stars", lang), callback_data="admin_give_start"))
    await message.answer(t("admin_panel", lang), reply_markup=b.as_markup())

@dp.callback_query(F.data == "admin_broadcast_info")
async def admin_broadcast_info(cb: types.CallbackQuery):
    await cb.answer("Напиши команду /broadcast в чат", show_alert=True)

@dp.callback_query(F.data == "admin_create_promo")
async def admin_promo_start(cb: types.CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminStates.promo_word)
    await cb.message.edit_text(t("admin_promo_word", "ru"), reply_markup=back_kb())
    await cb.answer()

@dp.message(AdminStates.promo_word)
async def admin_promo_word(message: Message, state: FSMContext):
    await state.update_data(promo_word=message.text.strip().upper())
    await state.set_state(AdminStates.promo_reward)
    await message.answer(t("admin_promo_reward", "ru"), reply_markup=back_kb())

@dp.message(AdminStates.promo_reward)
async def admin_promo_reward(message: Message, state: FSMContext):
    reward = parse_reward(message.text)
    if reward is None: return await message.answer("❌ Введи число, например 1.00")
    data = await state.get_data()
    if await create_promo(data['promo_word'], reward):
        await message.answer(t("admin_promo_created", "ru").format(data['promo_word'], f"{reward:.2f}"), parse_mode="Markdown")
    else:
        await message.answer(t("admin_promo_exists", "ru"))
    await state.clear()

@dp.callback_query(F.data == "admin_quick_add")
async def admin_quick_add(cb: types.CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminStates.quick_add)
    txt = ("Отправь всё в одном сообщении, каждое с новой строки:\n\n"
           "1. Название\n2. Ссылка\n3. Описание\n4. Награда\n\n"
           "Пример:\nMinerXRealm\nhttps://t.me/MinerXRealmBot/app\nПосмотрите 10 реклам\n5")
    await cb.message.edit_text(txt, reply_markup=back_kb())
    await cb.answer()

@dp.message(AdminStates.quick_add)
async def admin_quick_add_process(message: Message, state: FSMContext):
    lines = message.text.strip().split("\n")
    if len(lines) < 4: return await message.answer("❌ Меньше 4 строк.")
    reward = parse_reward(lines[3])
    if reward is None: return await message.answer("❌ Награда — число.")
    await create_task(lines[0].strip(), lines[1].strip(), lines[2].strip(), reward)
    u = await get_user(message.from_user.id)
    await message.answer(t("admin_task_added", u['lang']))
    await state.clear()

@dp.callback_query(F.data == "admin_add")
async def admin_add_start(cb: types.CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    u = await get_user(cb.from_user.id)
    await state.set_state(AdminStates.add_title)
    await cb.message.edit_text(t("admin_enter_title", u['lang']), reply_markup=back_kb())
    await cb.answer()

@dp.message(AdminStates.add_title)
async def admin_add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    u = await get_user(message.from_user.id)
    await state.set_state(AdminStates.add_link)
    await message.answer(t("admin_enter_link", u['lang']), reply_markup=back_kb())

@dp.message(AdminStates.add_link)
async def admin_add_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text)
    u = await get_user(message.from_user.id)
    await state.set_state(AdminStates.add_desc)
    await message.answer(t("admin_enter_desc", u['lang']), reply_markup=back_kb())

@dp.message(AdminStates.add_desc)
async def admin_add_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text)
    u = await get_user(message.from_user.id)
    await state.set_state(AdminStates.add_reward)
    await message.answer(t("admin_enter_reward", u['lang']), reply_markup=back_kb())

@dp.message(AdminStates.add_reward)
async def admin_add_reward(message: Message, state: FSMContext):
    reward = parse_reward(message.text)
    if reward is None: return await message.answer("❌ Введи число.")
    data = await state.get_data()
    await create_task(data['title'], data['link'], data['desc'], reward)
    u = await get_user(message.from_user.id)
    await message.answer(t("admin_task_added", u['lang']))
    await state.clear()

@dp.callback_query(F.data == "admin_del_list")
async def admin_del_list(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id): return
    u = await get_user(cb.from_user.id); lang = u['lang']
    tasks = await get_active_tasks()
    if not tasks: return await cb.answer(t("admin_no_tasks", lang), show_alert=True)
    b = InlineKeyboardBuilder()
    for task in tasks:
        b.row(InlineKeyboardButton(text=f"❌ {task['title']} ({float(task['reward']):.2f} ⭐️)",
                                   callback_data=f"admin_del_{task['id']}"))
    await cb.message.edit_text(t("admin_del_task", lang), reply_markup=b.as_markup())
    await cb.answer()

@dp.callback_query(F.data.startswith("admin_del_"))
async def admin_do_delete(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id): return
    await delete_task(int(cb.data.split("_")[2]))
    u = await get_user(cb.from_user.id)
    await cb.answer(t("admin_task_deleted", u['lang']), show_alert=True)
    await cb.message.delete()

# ===== ⚙️ АДМИН: НАЗНАЧИТЬ БАЛАНС =====
@dp.callback_query(F.data == "admin_give_start")
async def admin_give_start(cb: types.CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    u = await get_user(cb.from_user.id); lang = u['lang'] if u else "ru"
    await state.set_state(AdminStates.give_user)
    await cb.message.answer(t("admin_give_enter_user", lang), reply_markup=back_kb(lang))
    await cb.answer()

@dp.message(AdminStates.give_user)
async def admin_give_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    u = await get_user(message.from_user.id); lang = u['lang'] if u else "ru"
    txt = (message.text or "").strip()
    if txt.isdigit():
        target = await get_user(int(txt))
    elif txt.startswith("@"):
        target = await get_user_by_username(txt[1:])
    else:
        target = await get_user_by_username(txt)
    if not target:
        return await message.answer(t("admin_give_user_not_found", lang))
    await state.update_data(target_id=target['user_id'])
    await state.set_state(AdminStates.give_amount)
    await message.answer(
        f"{t('admin_give_enter_amount', lang)}\n\n"
        f"👤 @{target['username'] or target['user_id']} (ID: {target['user_id']})\n"
        f"💰 Текущий: {float(target['balance']):.2f} ⭐️",
        reply_markup=back_kb(lang))

@dp.message(AdminStates.give_amount)
async def admin_give_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    u = await get_user(message.from_user.id); lang = u['lang'] if u else "ru"
    amount = parse_reward(message.text)
    if amount is None or amount < 0:
        return await message.answer(t("admin_give_bad_amount", lang))
    await state.update_data(amount=amount)
    await state.set_state(AdminStates.give_reason)
    await message.answer(t("admin_give_enter_reason", lang), reply_markup=back_kb(lang))

@dp.message(AdminStates.give_reason)
async def admin_give_reason(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    u = await get_user(message.from_user.id); lang = u['lang'] if u else "ru"
    reason = (message.text or "").strip()
    data = await state.get_data()
    target_id, amount = data.get('target_id'), data.get('amount')
    if target_id is None or amount is None:
        await state.clear()
        return await message.answer("❌ Данные потеряны. Начни заново: /admin")
    old = await get_user(target_id)
    if not old:
        await state.clear()
        return await message.answer("❌ Юзер не найден.")
    old_bal = float(old['balance'])
    delta = amount - old_bal
    await add_balance_admin(target_id, delta)
    await log_admin_give(message.from_user.id, target_id, amount, f"[SET] {reason}")
    updated = await get_user(target_id)
    new_bal = float(updated['balance'])
    try:
        await bot.send_message(target_id,
            f"⚙️ Твой баланс изменён администратором.\n"
            f"Новый баланс: {new_bal:.2f} ⭐️\n"
            f"Причина: {reason}")
    except Exception: pass
    await message.answer(
        f"✅ Баланс назначен\n\n"
        f"👤 @{updated['username'] or target_id} (ID: {target_id})\n"
        f"Было: {old_bal:.2f} ⭐️\n"
        f"Стало: {new_bal:.2f} ⭐️\n"
        f"Причина: {reason}")
    await state.clear()

# ===== АДМИН: /top и /balance =====
@dp.message(Command("top"))
async def cmd_top(message: Message):
    if not is_admin(message.from_user.id): return
    u = await get_user(message.from_user.id); lang = u['lang'] if u else "ru"
    top = await get_top_users(20)
    if not top: return await message.answer(t("admin_top_empty", lang))
    lines = [t("admin_top_title", lang)]
    for i, r in enumerate(top, 1):
        uname = f"@{r['username']}" if r['username'] else str(r['user_id'])
        lines.append(f"{i}. {uname} — {float(r['balance']):.2f} ⭐️")
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    if not is_admin(message.from_user.id): return
    u = await get_user(message.from_user.id); lang = u['lang'] if u else "ru"
    args = message.text.split()
    if len(args) < 2: return await message.answer(t("admin_balance_usage", lang))
    tgt = args[1]
    if tgt.startswith("@"): target = await get_user_by_username(tgt[1:])
    elif tgt.isdigit(): target = await get_user(int(tgt))
    else: target = None
    if not target: return await message.answer(t("admin_balance_not_found", lang))
    await message.answer(t("admin_balance_result", lang).format(
        target['username'] or target['user_id'], target['user_id'], f"{float(target['balance']):.2f}"))

# ===== СТАТИСТИКА =====
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id): return
    u = await get_user(message.from_user.id); lang = u['lang'] if u else "ru"
    users, tb, te, pend = await get_global_stats()
    await message.answer(t("admin_stats", lang).format(users, f"{tb:.2f}", f"{te:.2f}", pend))

# ===== ВЫВОД =====
@dp.callback_query(F.data == "withdraw")
async def withdraw_menu(cb: types.CallbackQuery):
    u = await get_user(cb.from_user.id); lang = u['lang']
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("withdraw_15", lang), callback_data="wdraw_15"))
    b.row(InlineKeyboardButton(text=t("withdraw_custom", lang), callback_data="wdraw_custom"))
    b.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_to_main"))
    await cb.message.edit_text(t("withdraw_menu", lang).format(f"{float(u['balance']):.2f}"),
                               reply_markup=b.as_markup())
    await cb.answer()

@dp.callback_query(F.data == "wdraw_15")
async def withdraw_15(cb: types.CallbackQuery):
    u = await get_user(cb.from_user.id); lang = u['lang']
    if float(u['balance']) < MIN_WITHDRAW:
        return await cb.answer(t("not_enough_balance", lang), show_alert=True)
    await cb.answer(t("withdraw_request_sent", lang), show_alert=True)
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, f"💸 Заявка: @{cb.from_user.username} (ID: {cb.from_user.id})\nСумма: {MIN_WITHDRAW} ⭐️\nБаланс: {float(u['balance']):.2f} ⭐️")
        except Exception: pass

@dp.callback_query(F.data == "wdraw_custom")
async def withdraw_custom(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(WithdrawState.waiting_amount)
    await cb.message.edit_text("Введи сумму (от 50):", reply_markup=back_kb())
    await cb.answer()

@dp.message(WithdrawState.waiting_amount)
async def withdraw_amount(message: Message, state: FSMContext):
    u = await get_user(message.from_user.id); lang = u['lang']
    amount = parse_reward(message.text)
    if amount is None or amount < 50: return await message.answer(t("invalid_amount", lang))
    if float(u['balance']) < amount: return await message.answer(t("not_enough_balance", lang))
    await message.answer(t("withdraw_request_sent", lang))
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, f"💸 Заявка: @{message.from_user.username} (ID: {message.from_user.id})\nСумма: {amount:.2f} ⭐️\nБаланс: {float(u['balance']):.2f} ⭐️")
        except Exception: pass
    await state.clear()

# ===== ВЕБ-СЕРВЕР / ВЕБХУКИ =====
async def handle_ping(request):
    return web.Response(text="Bot is alive")

async def handle_piarflow_webhook(request):
    try:
        p = await request.json()
        if p.get("test"): return web.json_response({"ok": True})
        if p.get("status") == "unsubscribed":
            uid = int(p["tg_user_id"]); link = str(p["offer_link"])
            task = await get_user_task(uid, link)
            if task:
                r = float(task['reward'])
                await deduct_balance(uid, r)
                logging.info(f"Webhook: {uid} отписался от {link}, -{r} ⭐️")
                try: await bot.send_message(uid, f"⚠️ Отписка от {link}. Списано {r:.2f} ⭐️.")
                except Exception: pass
        return web.json_response({"ok": True})
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return web.json_response({"ok": False}, status=500)

async def handle_tonads_webhook(request):
    try:
        raw_body = await request.read()
        signature_header = request.headers.get('X-TonAds-Signature', '')
        
        expected_sig = hmac.new(TONADS_SECRET.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
        expected_sig = f"sha256={expected_sig}"
        
        if not hmac.compare_digest(expected_sig, signature_header):
            logging.warning("TonAds Webhook: Неверная подпись!")
            return web.Response(status=401)
            
        payload = json.loads(raw_body)
        event_id = payload.get("eventId")
        if not event_id:
            return web.Response(status=400)

        if await is_tonads_event_processed(event_id):
            return web.Response(status=200)

        if "task_finish" in payload:
            task = payload["task_finish"]
            user_id_str = task.get("tgId") or task.get("payload")
            
            if user_id_str:
                try:
                    uid = int(user_id_str)
                    await add_balance(uid, TONADS_REWARD)
                    await process_task_completion(uid) # Начисляем выполненные задания для рефералки/чекина
                    await mark_tonads_event_processed(event_id)
                    
                    user = await get_user(uid)
                    if user:
                        try:
                            await bot.send_message(
                                uid, 
                                f"✅ Начислено {TONADS_REWARD:.2f} ⭐️ за просмотр рекламы (TonAds)!\n"
                                f"Баланс: {float(user['balance']) + TONADS_REWARD:.2f} ⭐️"
                            )
                        except Exception:
                            pass
                    
                    return web.json_response({
                        "title": "✅ Награда получена!",
                        "text": f"Вам начислено {TONADS_REWARD:.2f} ⭐️. Возвращайтесь в бота!",
                        "link": f"https://t.me/{BOT_USERNAME}"
                    })
                except ValueError:
                    logging.error(f"TonAds: Неверный ID пользователя {user_id_str}")
            
            return web.Response(status=200)

        elif "task_payment" in payload:
            await mark_tonads_event_processed(event_id)
            logging.info(f"TonAds: Оплата получена {payload['task_payment'].get('amount')} USDT")
            return web.Response(status=200)

        return web.Response(status=200)
    except Exception as e:
        logging.error(f"TonAds webhook error: {e}")
        return web.Response(status=500)

# ===== ФОН: новые задания =====
async def background_task_checker():
    await asyncio.sleep(60)
    global last_known_tasks
    while True:
        try:
            uids = await get_all_user_ids()
            if uids:
                tu = uids[0]
                res = await piarflow.get_sponsors(tu, tu)
                sps = res.get("sponsors") if res and res.get("status") == "ok" else []
                cur = set(s["link"] for s in sps)
                if last_known_tasks and not cur.issubset(last_known_tasks):
                    for uid in uids:
                        try:
                            await bot.send_message(uid, t("new_tasks_notification", "ru"), parse_mode="Markdown")
                            await asyncio.sleep(0.05)
                        except Exception: pass
                last_known_tasks = cur
        except Exception as e:
            logging.error(f"BG check: {e}")
        await asyncio.sleep(3600)

# ===== ЗАПУСК =====
async def main():
    await init_db()
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_post("/piarflow/webhook", handle_piarflow_webhook)
    app.router.add_post("/tonads/webhook", handle_tonads_webhook) # <--- ДОБАВЛЕНА СТРОКА
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Web server on {PORT}")
    asyncio.create_task(background_task_checker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())