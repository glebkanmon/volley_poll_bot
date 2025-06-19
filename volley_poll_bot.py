import os
import logging
import datetime
import random
import asyncio
import re

import aiosqlite

from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, CallbackQuery
)
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# -------------- Конфиг и глобальные переменные ---------------
load_dotenv()
API_TOKEN = os.getenv('BOT_TOKEN')
DB_FILE = os.getenv("DB_FILE", "volley_poll_bot.sqlite3")

uncertain_titles = [
    "Наверное",
    "Как карта ляжет",
    "Как сердце скажет",
    "Как судьба решит",
    "По воле случая",
    "Как ветер подует",
    "Если звёзды сложатся",
    "Как туман рассеется — узнаем",
    "Если ничего не пойдёт не так"
]
NEGATIVE_TITLE = "Нет"

DAYS = [
    ("Пн", "1"),
    ("Вт", "2"),
    ("Ср", "3"),
    ("Чт", "4"),
    ("Пт", "5"),
    ("Сб", "6"),
    ("Вс", "0"),
]

# -------------- Вспомогательные функции ----------------------

def today_date_str():
    return datetime.datetime.now().strftime("%d/%m")

def parse_time_from_text(text):
    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return hour * 60 + minute
    return None

def mention(user):
    if user.get("username"):
        return f"@{user['username']}"
    else:
        safe_name = user['first_name']
        return f"[{safe_name}](tg://user?id={user['user_id']})"

def build_days_inline_keyboard(selected_days=None):
    if selected_days is None:
        selected_days = set()
    keyboard = []
    row = []
    for day, val in DAYS:
        text = f"✅ {day}" if day in selected_days else day
        row.append(InlineKeyboardButton(text=text, callback_data=f"day_{val}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton(text="Готово", callback_data="done"),
        InlineKeyboardButton(text="Сбросить", callback_data="reset"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def days_to_cron(selected_days):
    days_map = {d: v for d, v in DAYS}
    order = [d for d, v in DAYS]
    if set(selected_days) == set(order):
        return "*"
    elif set(selected_days) == set(order[:5]):
        return "1,2,3,4,5"
    else:
        return ",".join([days_map[d] for d in order if d in selected_days])

def time_to_cron(time_str):
    match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", time_str)
    if not match:
        return None
    hour, minute = match.groups()
    return int(minute), int(hour)

def get_answer_sort_key(text):
    t = parse_time_from_text(text)
    return (0, t) if t is not None else (1, 0)

async def get_default_answer_templates():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, text FROM answer_templates WHERE uncertain_flg=0 AND negative_flg=0 ORDER BY sort_time ASC NULLS LAST, id ASC"
        ) as cursor:
            return [(row[0], row[1]) for row in await cursor.fetchall()]

async def get_random_uncertain_answer():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, text FROM answer_templates WHERE uncertain_flg=1"
        ) as cursor:
            rows = await cursor.fetchall()
            if rows:
                return random.choice(rows)
    return None

async def get_negative_answer():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, text FROM answer_templates WHERE negative_flg=1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row
    return None

async def init_answer_templates():
    answers = [
        ("Да 19:00", 19*60, 0, 0),
        ("Да 20:00", 20*60, 0, 0)
    ]
    uncertain_answers = [(text, None, 1, 0) for text in uncertain_titles]
    negative_answer = [(NEGATIVE_TITLE, None, 0, 1)]
    all_answers = answers + uncertain_answers + negative_answer
    async with aiosqlite.connect(DB_FILE) as db:
        for text, sort_time, uncertain_flg, negative_flg in all_answers:
            await db.execute(
                "INSERT OR IGNORE INTO answer_templates (text, sort_time, uncertain_flg, negative_flg) VALUES (?, ?, ?, ?)",
                (text, sort_time, uncertain_flg, negative_flg)
            )
        await db.commit()

async def get_or_create_answer_template(text):
    sort_time = parse_time_from_text(text)
    async with aiosqlite.connect(DB_FILE) as db:
        # Не даём пользователю добавить "Нет" и неопределённые варианты
        async with db.execute(
            "SELECT id FROM answer_templates WHERE LOWER(text)=LOWER(?)", (text,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
        cur = await db.execute(
            "INSERT INTO answer_templates (text, sort_time, uncertain_flg, negative_flg) VALUES (?, ?, 0, 0)", (text, sort_time)
        )
        await db.commit()
        return cur.lastrowid

async def get_answer_template_texts(ids):
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            f"SELECT id, text FROM answer_templates WHERE id IN ({placeholders})", ids
        ) as cursor:
            id_to_text = {row[0]: row[1] for row in await cursor.fetchall()}
    return [id_to_text[i] for i in ids if i in id_to_text]

# -------------- FSM для расписания ---------------------------

class ScheduleStates(StatesGroup):
    entering_poll_title = State()
    add_date_to_title = State()
    choosing_poll_options = State()
    adding_custom_option = State()
    confirm_add_another_option = State()
    ask_uncertain_option = State()
    ask_negative_option = State()
    choosing_days = State()
    entering_time = State()
    confirm_update = State()
    confirm_delete = State()

# -------------- Работа с базой данных ------------------------

async def init_db():
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.executescript("""
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    title TEXT,
    is_active BOOLEAN DEFAULT 1,
    processed_dttm DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL,
    title TEXT,
    processed_dttm DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups(id)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    processed_dttm DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_poll_id TEXT UNIQUE,
    group_id INTEGER NOT NULL,
    thread_id INTEGER,
    question TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE,
    processed_dttm DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups(id),
    FOREIGN KEY (thread_id) REFERENCES threads(id)
);
CREATE TABLE IF NOT EXISTS poll_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER NOT NULL,
    option_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    processed_dttm DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (poll_id) REFERENCES polls(id),
    UNIQUE(poll_id, option_index)
);
CREATE TABLE IF NOT EXISTS votes (
    poll_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    option_id INTEGER NOT NULL,
    voted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    processed_dttm DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (poll_id, user_id),
    FOREIGN KEY (poll_id) REFERENCES polls(id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (option_id) REFERENCES poll_options(id)
);
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    processed_dttm DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS poll_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    thread_id INTEGER,
    cron_expr TEXT NOT NULL,
    poll_title TEXT,
    add_date_to_title BOOLEAN DEFAULT 1,
    is_active BOOLEAN DEFAULT 1,
    processed_dttm DATETIME DEFAULT CURRENT_TIMESTAMP,
    uncertain_option INTEGER,
    negative_option INTEGER,
    FOREIGN KEY (group_id) REFERENCES groups(id),
    FOREIGN KEY (thread_id) REFERENCES threads(id),
    FOREIGN KEY (uncertain_option) REFERENCES answer_templates(id),
    FOREIGN KEY (negative_option) REFERENCES answer_templates(id)
);
CREATE TABLE IF NOT EXISTS answer_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL UNIQUE,
    sort_time INTEGER,
    uncertain_flg INTEGER DEFAULT 0,
    negative_flg INTEGER DEFAULT 0,
    processed_dttm DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS poll_schedule_x_answer_template (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_schedule_id INTEGER NOT NULL,
    answer_template_id INTEGER NOT NULL,
    option_index INTEGER NOT NULL,
    processed_dttm DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (poll_schedule_id) REFERENCES poll_schedule(id),
    FOREIGN KEY (answer_template_id) REFERENCES answer_templates(id),
    UNIQUE(poll_schedule_id, option_index)
);
            """)
            await db.commit()
        await init_answer_templates()
        logging.info("DB initialized successfully")
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Error initializing DB: {e}")

async def add_group_and_thread(group_id, group_title, thread_id=None, thread_title=None):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT OR IGNORE INTO groups (id, title) VALUES (?, ?)", (group_id, group_title)
            )
            if thread_id:
                await db.execute(
                    "INSERT OR IGNORE INTO threads (id, group_id, title) VALUES (?, ?, ?)",
                    (thread_id, group_id, thread_title or "")
                )
            await db.commit()
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Error adding group/thread: {e}")

async def save_poll(telegram_poll_id, group_id, thread_id, question, options):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO polls (telegram_poll_id, group_id, thread_id, question) VALUES (?, ?, ?, ?)",
                (telegram_poll_id, group_id, thread_id, question)
            )
            async with db.execute("SELECT id FROM polls WHERE telegram_poll_id=?", (telegram_poll_id,)) as cursor:
                poll_row = await cursor.fetchone()
                poll_id = poll_row[0]
            for idx, opt in enumerate(options):
                await db.execute(
                    "INSERT INTO poll_options (poll_id, option_index, text) VALUES (?, ?, ?)",
                    (poll_id, idx, opt)
                )
            await db.commit()
            return poll_id
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Error saving poll: {e}")
        return None

async def get_poll_id_by_telegram_poll_id(telegram_poll_id):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT id FROM polls WHERE telegram_poll_id=?", (telegram_poll_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Error getting poll id by telegram id: {e}")
        return None

async def save_user(user: types.User):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT OR REPLACE INTO users (id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
                (user.id, user.username, user.first_name, user.last_name)
            )
            await db.commit()
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Error saving user {user.id}: {e}")

async def save_vote(poll_id, user: types.User, option_ids):
    try:
        await save_user(user)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM votes WHERE poll_id=? AND user_id=?", (poll_id, user.id))
            option_id = option_ids[0] if option_ids else None
            if option_id is not None:
                async with db.execute("SELECT id FROM poll_options WHERE poll_id=? AND option_index=?",
                                      (poll_id, option_id)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        await db.execute(
                            "INSERT INTO votes (poll_id, user_id, option_id) VALUES (?, ?, ?)",
                            (poll_id, user.id, row[0])
                        )
            await db.commit()
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Error saving vote: {e}")

async def get_existing_schedule(group_id, thread_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, cron_expr, poll_title, add_date_to_title, uncertain_option, negative_option FROM poll_schedule WHERE group_id=? AND thread_id IS ? AND is_active=1",
            (group_id, thread_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "cron_expr": row[1],
                    "poll_title": row[2],
                    "add_date_to_title": row[3],
                    "uncertain_option": row[4],
                    "negative_option": row[5]
                }
            return None

async def save_or_update_schedule(group_id, thread_id, cron_expr, poll_title, add_date_to_title, answer_template_ids, uncertain_option_id, negative_option_id):
    async with aiosqlite.connect(DB_FILE) as db:
        # Delete old schedule and options
        async with db.execute(
            "SELECT id FROM poll_schedule WHERE group_id=? AND thread_id IS ?", (group_id, thread_id)
        ) as cursor:
            old = await cursor.fetchone()
            if old:
                await db.execute("DELETE FROM poll_schedule_x_answer_template WHERE poll_schedule_id=?", (old[0],))
                await db.execute("DELETE FROM poll_schedule WHERE id=?", (old[0],))
        # Insert new schedule
        cur = await db.execute(
            "INSERT INTO poll_schedule (group_id, thread_id, cron_expr, poll_title, add_date_to_title, uncertain_option, negative_option) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (group_id, thread_id, cron_expr, poll_title, int(add_date_to_title), uncertain_option_id, negative_option_id)
        )
        poll_schedule_id = cur.lastrowid
        # Insert options
        for idx, aid in enumerate(answer_template_ids):
            await db.execute(
                "INSERT INTO poll_schedule_x_answer_template (poll_schedule_id, answer_template_id, option_index) VALUES (?, ?, ?)",
                (poll_schedule_id, aid, idx)
            )
        await db.commit()

async def delete_schedule(group_id, thread_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id FROM poll_schedule WHERE group_id=? AND thread_id IS ?", (group_id, thread_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                await db.execute("DELETE FROM poll_schedule_x_answer_template WHERE poll_schedule_id=?", (row[0],))
                await db.execute("DELETE FROM poll_schedule WHERE id=?", (row[0],))
                await db.commit()

async def get_schedule_option_ids(poll_schedule_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT answer_template_id FROM poll_schedule_x_answer_template WHERE poll_schedule_id=? ORDER BY option_index",
            (poll_schedule_id,)
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

async def get_schedule_special_option(poll_schedule_id, flag_type="uncertain"):
    col = "uncertain_option" if flag_type == "uncertain" else "negative_option"
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            f"SELECT {col} FROM poll_schedule WHERE id=?", (poll_schedule_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None

# -------------- FSM-диалог для настройки опций опроса --------

router = Router()

@router.message(Command("poll_settings"))
async def poll_settings_command(message: types.Message, state: FSMContext, bot: Bot):
    group_id = message.chat.id
    user_id = message.from_user.id

    try:
        member = await bot.get_chat_member(chat_id=group_id, user_id=user_id)
        status = member.status
        if status not in ("administrator", "creator", "owner"):
            await message.reply("Только администраторы могут настраивать расписание опросов.")
            return
    except Exception as e:
        await message.reply("Не удалось проверить права пользователя.")
        logging.error(f"Error checking admin rights: {e}")
        return

    group_title = message.chat.title or ""
    thread_id = getattr(message, "message_thread_id", None)
    thread_title = ""
    await add_group_and_thread(group_id, group_title, thread_id, thread_title)
    existing = await get_existing_schedule(group_id, thread_id)
    if existing:
        cron_str = existing["cron_expr"]
        poll_title = existing["poll_title"]
        add_date = bool(existing["add_date_to_title"])
        msg = (
            f"В этом чате уже настроено расписание опросов:\n"
            f"Название: <b>{poll_title}</b>\n"
            f"Добавлять дату: {'да' if add_date else 'нет'}\n"
            f"Cron: <code>{cron_str}</code>\n\n"
            "Что хотите сделать?"
        )
        await state.set_state(ScheduleStates.confirm_update)
        await state.update_data(existing_cron=cron_str, existing_poll_title=poll_title, existing_add_date=add_date, existing_id=existing["id"])
        await message.answer(
            msg,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Изменить", callback_data="update_schedule"),
                        InlineKeyboardButton(text="Оставить как есть", callback_data="keep_schedule"),
                    ],
                    [
                        InlineKeyboardButton(text="Удалить расписание", callback_data="delete_schedule"),
                    ]
                ]
            )
        )
    else:
        await state.set_state(ScheduleStates.entering_poll_title)
        await message.answer(
            "Введите название опроса (например: Волейбол, Футбол, Бег, Шашлык и т.д.):"
        )

@router.message(ScheduleStates.entering_poll_title)
async def entering_poll_title(message: types.Message, state: FSMContext):
    poll_title = message.text.strip()
    if not poll_title or len(poll_title) < 2:
        await message.answer("Название опроса слишком короткое. Введите другое название.")
        return
    await state.update_data(poll_title=poll_title)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data="add_date_yes"),
                InlineKeyboardButton(text="Нет", callback_data="add_date_no"),
            ]
        ]
    )
    await state.set_state(ScheduleStates.add_date_to_title)
    await message.answer(
        f"Добавлять дату публикации (ДД/ММ) к названию опроса?\n\n"
        f"Вы ввели: <b>{poll_title}</b>\n\n"
        f"Например: <b>{poll_title} {today_date_str()}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )

@router.callback_query((F.data == "add_date_yes") | (F.data == "add_date_no"), ScheduleStates.add_date_to_title)
async def add_date_to_title_choice(callback: CallbackQuery, state: FSMContext):
    add_date = callback.data == "add_date_yes"
    await state.update_data(add_date_to_title=add_date)
    default_options = await get_default_answer_templates()
    await state.update_data(available_options=default_options, selected_options=[], uncertain_option_id=None, negative_option_id=None)
    await state.set_state(ScheduleStates.choosing_poll_options)
    await send_options_choice(callback.message, state)
    await callback.answer()

async def send_options_choice(message, state):
    data = await state.get_data()
    available_options = data.get("available_options", [])
    selected_options = data.get("selected_options", [])
    selected_ids = [i for i, _ in selected_options]
    buttons = []
    for opt_id, opt_text in available_options:
        if opt_id not in selected_ids:
            buttons.append([InlineKeyboardButton(text=opt_text, callback_data=f"opt_{opt_id}")])
    buttons.append([InlineKeyboardButton(text="Добавить свой ответ", callback_data="add_custom_option")])
    if selected_options:
        buttons.append([InlineKeyboardButton(text="Достаточно", callback_data="options_done")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if selected_options:
        txt = "Выбранные варианты:\n" + "\n".join(f"• {o[1]}" for o in selected_options)
    else:
        txt = "Выберите варианты ответа для опроса:"
    await message.answer(txt, reply_markup=kb)

@router.callback_query(F.data.startswith("opt_"), ScheduleStates.choosing_poll_options)
async def on_option_chosen(callback: CallbackQuery, state: FSMContext):
    opt_id = int(callback.data[4:])
    data = await state.get_data()
    selected_options = data.get("selected_options", [])
    available_options = data.get("available_options", [])
    text = next((t for i, t in available_options if i == opt_id), None)
    if text and (opt_id, text) not in selected_options:
        selected_options.append((opt_id, text))
        await state.update_data(selected_options=selected_options)
    await callback.message.edit_reply_markup(reply_markup=None)
    await send_options_choice(callback.message, state)
    await callback.answer()

@router.callback_query(F.data == "add_custom_option", ScheduleStates.choosing_poll_options)
async def on_add_custom_option(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ScheduleStates.adding_custom_option)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Введите свой вариант ответа (например, Да 18:30):")
    await callback.answer()

@router.message(ScheduleStates.adding_custom_option)
async def process_custom_option(message: types.Message, state: FSMContext):
    option = message.text.strip()
    if not option or len(option) < 2:
        await message.answer("Вариант слишком короткий. Введите другой вариант.")
        return
    # Не даём добавить "Нет" и неопределённые варианты
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT 1 FROM answer_templates WHERE (uncertain_flg=1 OR negative_flg=1) AND LOWER(text)=LOWER(?)", (option,)
        ) as cursor:
            if await cursor.fetchone():
                await message.answer("Такой вариант добавлять нельзя. Введите другой вариант.")
                return
    data = await state.get_data()
    selected_options = data.get("selected_options", [])
    available_options = data.get("available_options", [])
    all_texts_lower = [t.lower() for _, t in available_options + selected_options]
    if option.lower() in all_texts_lower:
        await message.answer("Такой вариант уже есть! Введите другой вариант.")
        return
    opt_id = await get_or_create_answer_template(option)
    available_options.append((opt_id, option))
    selected_options.append((opt_id, option))
    await state.update_data(selected_options=selected_options, available_options=available_options)
    await state.set_state(ScheduleStates.confirm_add_another_option)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить еще", callback_data="add_more_custom")],
            [InlineKeyboardButton(text="Достаточно", callback_data="options_done")],
        ]
    )
    await message.answer(f"Вариант <b>{option}</b> добавлен. Хотите добавить еще?", parse_mode=ParseMode.HTML, reply_markup=kb)

@router.callback_query(F.data == "add_more_custom", ScheduleStates.confirm_add_another_option)
async def add_more_custom_option(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ScheduleStates.adding_custom_option)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Введите следующий вариант ответа:")
    await callback.answer()

@router.callback_query(F.data == "options_done", ScheduleStates.choosing_poll_options)
@router.callback_query(F.data == "options_done", ScheduleStates.confirm_add_another_option)
async def options_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_options = data.get("selected_options", [])
    if not selected_options:
        await callback.answer("Выберите хотя бы один вариант!", show_alert=True)
        return
    await state.update_data(selected_options=selected_options)
    await state.set_state(ScheduleStates.ask_uncertain_option)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="add_uncertain")],
            [InlineKeyboardButton(text="Нет", callback_data="skip_uncertain")]
        ]
    )
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Добавить неопределённый вариант (например, 'Наверное', 'Как карта ляжет')?", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "add_uncertain", ScheduleStates.ask_uncertain_option)
async def add_uncertain_option(callback: CallbackQuery, state: FSMContext):
    uncertain = await get_random_uncertain_answer()
    uncertain_id = uncertain[0] if uncertain else None
    await state.update_data(uncertain_option_id=uncertain_id)
    await state.set_state(ScheduleStates.ask_negative_option)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="add_negative")],
            [InlineKeyboardButton(text="Нет", callback_data="skip_negative")]
        ]
    )
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Добавить отрицательный вариант 'Нет'?", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "skip_uncertain", ScheduleStates.ask_uncertain_option)
async def skip_uncertain_option(callback: CallbackQuery, state: FSMContext):
    await state.update_data(uncertain_option_id=None)
    await state.set_state(ScheduleStates.ask_negative_option)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="add_negative")],
            [InlineKeyboardButton(text="Нет", callback_data="skip_negative")]
        ]
    )
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Добавить отрицательный вариант 'Нет'?", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "add_negative", ScheduleStates.ask_negative_option)
async def add_negative_option(callback: CallbackQuery, state: FSMContext):
    negative = await get_negative_answer()
    negative_id = negative[0] if negative else None
    await state.update_data(negative_option_id=negative_id)
    await state.set_state(ScheduleStates.choosing_days)
    kb = build_days_inline_keyboard()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Выберите дни для расписания опроса:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "skip_negative", ScheduleStates.ask_negative_option)
async def skip_negative_option(callback: CallbackQuery, state: FSMContext):
    await state.update_data(negative_option_id=None)
    await state.set_state(ScheduleStates.choosing_days)
    kb = build_days_inline_keyboard()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Выберите дни для расписания опроса:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "delete_schedule", ScheduleStates.confirm_update)
async def delete_schedule_confirm(callback: CallbackQuery, state: FSMContext):
    group_id = callback.message.chat.id
    thread_id = getattr(callback.message, "message_thread_id", None)
    await delete_schedule(group_id, thread_id)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Расписание опросов для этого чата/темы удалено.")
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "update_schedule", ScheduleStates.confirm_update)
async def confirm_update_schedule(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ScheduleStates.entering_poll_title)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Введите новое название опроса (например: Волейбол, Футбол, Бег, Шашлык и т.д.):"
    )
    await callback.answer()

@router.callback_query(F.data == "keep_schedule", ScheduleStates.confirm_update)
async def keep_schedule(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Расписание оставлено без изменений.")
    await state.clear()
    await callback.answer()

@router.callback_query(F.data.startswith("day_"), ScheduleStates.choosing_days)
async def on_day_toggle(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_days = set(data.get("selected_days", []))
    day_val = callback.data.split("_")[1]
    day = next(d for d, v in DAYS if v == day_val)
    if day in selected_days:
        selected_days.remove(day)
    else:
        selected_days.add(day)
    await state.update_data(selected_days=list(selected_days))
    kb = build_days_inline_keyboard(selected_days)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "reset", ScheduleStates.choosing_days)
async def on_reset(callback: CallbackQuery, state: FSMContext):
    await state.update_data(selected_days=[])
    kb = build_days_inline_keyboard()
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Выбор сброшен.")

@router.callback_query(F.data == "done", ScheduleStates.choosing_days)
async def on_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_days = set(data.get("selected_days", []))
    if not selected_days:
        await callback.answer("Выберите хотя бы один день!", show_alert=True)
        return
    await state.set_state(ScheduleStates.entering_time)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Введите время опроса в формате ЧЧ:ММ (например, 18:30):"
    )
    await state.update_data(selected_days=list(selected_days))
    await callback.answer()

@router.message(ScheduleStates.entering_time)
async def enter_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()
    time_res = time_to_cron(time_str)
    if not time_res:
        await message.answer("Некорректный формат времени! Введите, например, 18:30")
        return
    minute, hour = time_res
    data = await state.get_data()
    selected_days = set(data.get("selected_days", []))
    cron_days = days_to_cron(selected_days)
    cron_expr = f"{minute} {hour} * * {cron_days}"
    poll_title = data.get("poll_title", "Опрос")
    add_date_to_title = data.get("add_date_to_title", True)
    selected_options = data.get("selected_options", [])
    answer_template_ids = [opt_id for opt_id, _ in selected_options]
    uncertain_option_id = data.get("uncertain_option_id", None)
    negative_option_id = data.get("negative_option_id", None)
    group_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    try:
        await save_or_update_schedule(group_id, thread_id, cron_expr, poll_title, add_date_to_title, answer_template_ids, uncertain_option_id, negative_option_id)
        await message.answer(
            f"Новое расписание сохранено!\n"
            f"Название опроса: <b>{poll_title}</b>\n"
            f"Добавлять дату: {'да' if add_date_to_title else 'нет'}\n"
            f"Cron: <code>{cron_expr}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove()
        )
    except Exception as e:
        await message.answer("Ошибка при сохранении расписания.")
        logging.error(f"{datetime.datetime.now()}: Error saving schedule: {e}")
    await state.clear()

# -------------- Автоматический запуск опросов по расписанию --

async def get_due_schedules(now=None):
    if now is None:
        now = datetime.datetime.now()
    minute = now.minute
    hour = now.hour
    weekday = now.weekday()
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT id, group_id, thread_id, cron_expr, poll_title, add_date_to_title FROM poll_schedule WHERE is_active=1"
            ) as cursor:
                rows = await cursor.fetchall()
        result = []
        for sched_id, group_id, thread_id, cron_expr, poll_title, add_date_to_title in rows:
            m = re.match(r'^(\*|\d{1,2})\s+(\*|\d{1,2})\s+\*\s+\*\s+([\d*,]*)$', cron_expr)
            if not m:
                continue
            cm, ch, cd = m.group(1), m.group(2), m.group(3)
            if cm != "*" and int(cm) != minute:
                continue
            if ch != "*" and int(ch) != hour:
                continue
            if cd == "*" or cd == "":
                result.append((sched_id, group_id, thread_id, poll_title, bool(add_date_to_title)))
            else:
                cd_set = {int(i) for i in cd.split(",")}
                if weekday in cd_set:
                    result.append((sched_id, group_id, thread_id, poll_title, bool(add_date_to_title)))
        return result
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Error fetching due schedules: {e}")
        return []

async def start_new_poll(bot, sched_id, group_id, thread_id, poll_title, add_date_to_title):
    try:
        chat = await bot.get_chat(group_id)
        group_title = chat.title or ""
        thread_title = ""
        await add_group_and_thread(group_id, group_title, thread_id, thread_title)
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Error fetching chat info: {e}")
        await add_group_and_thread(group_id, "", thread_id, "")

    poll_title_final = f"{poll_title} {today_date_str()}" if add_date_to_title else poll_title
    answer_template_ids = await get_schedule_option_ids(sched_id)
    poll_options = await get_answer_template_texts(answer_template_ids)
    uncertain_option_id = await get_schedule_special_option(sched_id, "uncertain")
    negative_option_id = await get_schedule_special_option(sched_id, "negative")
    if uncertain_option_id:
        txts = await get_answer_template_texts([uncertain_option_id])
        if txts:
            poll_options.append(txts[0])
    if negative_option_id:
        txts = await get_answer_template_texts([negative_option_id])
        if txts:
            poll_options.append(txts[0])
    if not poll_options:
        poll_options = [t for _, t in await get_default_answer_templates()]

    try:
        poll_message = await bot.send_poll(
            chat_id=group_id,
            question=poll_title_final,
            options=poll_options,
            is_anonymous=False,
            allows_multiple_answers=False,
            message_thread_id=thread_id
        )
        await bot.pin_chat_message(
            chat_id=group_id,
            message_id=poll_message.message_id,
            disable_notification=False
        )
        telegram_poll_id = poll_message.poll.id
        await save_poll(telegram_poll_id, group_id, thread_id, poll_title_final, poll_options)
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Error creating or pinning poll: {e}")

async def poll_scheduler(bot):
    while True:
        try:
            now = datetime.datetime.now()
            due = await get_due_schedules(now)
            for sched_id, group_id, thread_id, poll_title, add_date_to_title in due:
                await start_new_poll(bot, sched_id, group_id, thread_id, poll_title, add_date_to_title)
        except Exception as e:
            logging.error(f"{datetime.datetime.now()}: Error in scheduler: {e}")
        await asyncio.sleep(60)

# -------------- Обработка голосов ----------------------------

async def get_poll_options_by_poll_id(poll_id):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT option_index, text FROM poll_options WHERE poll_id=? ORDER BY option_index",
                (poll_id,)
            ) as cursor:
                opts = await cursor.fetchall()
        return [opt[1] for opt in sorted(opts, key=lambda x: x[0])]
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Error getting poll options: {e}")
        return []

# -------------- Main ----------------------------------------

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    await init_db()
    try:
        bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=None))
        dp = Dispatcher()
        dp.include_router(router)

        @dp.poll_answer()
        async def poll_answer_handler(poll_answer: types.PollAnswer):
            try:
                telegram_poll_id = poll_answer.poll_id
                poll_id = await get_poll_id_by_telegram_poll_id(telegram_poll_id)
                if not poll_id:
                    logging.warning(f"{datetime.datetime.now()}: poll_id not found for telegram_poll_id={telegram_poll_id}")
                    return
                await save_vote(poll_id, poll_answer.user, poll_answer.option_ids)
            except Exception as e:
                logging.error(f"{datetime.datetime.now()}: Error in poll_answer_handler: {e}")

        asyncio.create_task(poll_scheduler(bot))

        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logging.error(f"{datetime.datetime.now()}: Error deleting webhook: {e}")

        logging.info("Bot started polling.")
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"{datetime.datetime.now()}: Unhandled error in main: {e}")

if __name__ == "__main__":
    asyncio.run(main())