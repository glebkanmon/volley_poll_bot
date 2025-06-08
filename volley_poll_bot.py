import os
import logging
import datetime
import random
import asyncio
import json

from aiogram import Bot, Dispatcher, F, types
from aiogram.types import PollAnswer
from aiogram.enums.parse_mode import ParseMode
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

load_dotenv()

API_TOKEN = os.getenv('BOT_TOKEN')
GROUP_ID = int(os.getenv('GROUP_ID'))
STORAGE_FILE = "poll_storage.json"
POLL_ID_FILE = "current_poll_id.txt"
POLL_CHAT_MAP_FILE = "poll_chat_map.json"

uncertain_titles = [
    "Наверное",
    "Как карта ляжет",
    "Как сердце скажет",
    "Как бог на душу положит",
    "Как судьба решит",
    "По воле случая",
    "Как ветер подует",
    "Если звёзды сложатся"
]

def today_poll_title():
    today = datetime.datetime.now().strftime("%d/%m")
    return f"Играем {today}?"

def build_poll_options():
    uncertain = random.choice(uncertain_titles)
    return ["Да 19:00", "Да 20:00", uncertain, "Нет"]

def seconds_until(hour: int, minute: int) -> int:
    now = datetime.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target < now:
        target += datetime.timedelta(days=1)
    return int((target - now).total_seconds())

def load_poll_votes():
    if not os.path.exists(STORAGE_FILE):
        return {}
    with open(STORAGE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_poll_votes(data):
    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def save_current_poll_id(poll_id):
    with open(POLL_ID_FILE, "w") as f:
        f.write(poll_id)

def load_current_poll_id():
    if not os.path.exists(POLL_ID_FILE):
        return None
    with open(POLL_ID_FILE, "r") as f:
        return f.read().strip()

def save_poll_chat_mapping(poll_id, chat_id):
    mapping = {}
    if os.path.exists(POLL_CHAT_MAP_FILE):
        with open(POLL_CHAT_MAP_FILE, "r", encoding="utf-8") as f:
            mapping = json.load(f)
    mapping[poll_id] = chat_id
    with open(POLL_CHAT_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

def load_poll_chat_mapping():
    if not os.path.exists(POLL_CHAT_MAP_FILE):
        return {}
    with open(POLL_CHAT_MAP_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def markdown_v2_escape(text: str) -> str:
    # Экранирует все спецсимволы MarkdownV2
    to_escape = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in to_escape else c for c in text)

def mention(user):
    if user["username"]:
        # username в Telegram не должен содержать спецсимволов, но экранируем на всякий случай
        return f"@{markdown_v2_escape(user['username'])}"
    else:
        safe_name = markdown_v2_escape(user['first_name'])
        return f"[{safe_name}](tg://user?id={user['user_id']})"

async def send_results(bot: Bot, poll_id: str, poll_options: list):
    data = load_poll_votes()
    votes = data.get(poll_id, [])
    # Группируем по выбранным вариантам
    by_option = {i: [] for i in range(len(poll_options))}
    for user in votes:
        if not user["option_ids"]:
            continue
        idx = user["option_ids"][0]
        by_option[idx].append(user)

    yes_19 = by_option.get(0, [])
    yes_20 = by_option.get(1, [])
    uncertain = by_option.get(2, [])

    text = ""
    if yes_19 or yes_20:
        text += "Сегодня идут играть:\n"
        if yes_19:
            text += "19:00: " + ", ".join(mention(u) for u in yes_19) + "\n"
        if yes_20:
            text += "20:00: " + ", ".join(mention(u) for u in yes_20) + "\n"
    if uncertain:
        text += "\nЕщё есть время надумать:\n"
        text += ", ".join(mention(u) for u in uncertain)
    if not text:
        text = "Пока никто не проголосовал за игру!"

    await bot.send_message(GROUP_ID, text, parse_mode=ParseMode.MARKDOWN_V2)

async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2))
    dp = Dispatcher()

    poll_options = build_poll_options()
    poll_title = today_poll_title()

    # Удаляем вебхук, если он активен, чтобы не было конфликта с polling
    await bot.delete_webhook(drop_pending_updates=True)

    # Событие — обработка голосов
    @dp.poll_answer()
    async def poll_answer_handler(poll_answer: types.PollAnswer):
        poll_chat_map = load_poll_chat_mapping()
        poll_id = poll_answer.poll_id
        chat_id = poll_chat_map.get(poll_id)
        # Проверяем, что ответ именно из нужного чата
        if chat_id is None or int(chat_id) != GROUP_ID:
            return

        data = load_poll_votes()
        if poll_id not in data:
            data[poll_id] = []
        # Сохраняем уникально (не дублируем user_id)
        data[poll_id] = [u for u in data[poll_id] if u["user_id"] != poll_answer.user.id]
        data[poll_id].append({
            "user_id": poll_answer.user.id,
            "username": poll_answer.user.username,
            "first_name": poll_answer.user.first_name,
            "option_ids": poll_answer.option_ids
        })
        save_poll_votes(data)

    # Создаём опрос
    poll_message = await bot.send_poll(
        chat_id=GROUP_ID,
        question=poll_title,
        options=poll_options,
        is_anonymous=False,
        allows_multiple_answers=False
    )
    await bot.pin_chat_message(
        chat_id=GROUP_ID,
        message_id=poll_message.message_id,
        disable_notification=False
    )

    poll_id = poll_message.poll.id
    save_current_poll_id(poll_id)
    save_poll_chat_mapping(poll_id, poll_message.chat.id)

    # Запускаем polling параллельно со sleep
    polling_task = asyncio.create_task(dp.start_polling(bot))

    # Ждём до 18:30
    sleep_seconds = seconds_until(13, 42)
    logging.info(f"Sleeping for {sleep_seconds} seconds until 18:30")
    await asyncio.sleep(sleep_seconds)

    # После сна формируем и отправляем сообщение с результатами
    await send_results(bot, poll_id, poll_options)

    # Останавливаем polling и закрываем сессию
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())