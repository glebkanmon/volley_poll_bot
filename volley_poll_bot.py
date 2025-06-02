import os
import logging
import datetime
import random
import asyncio
from aiogram import Bot
from dotenv import load_dotenv

load_dotenv()

API_TOKEN = os.getenv('BOT_TOKEN')
GROUP_ID = os.getenv('GROUP_ID')


print(API_TOKEN)

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

async def main():
    bot = Bot(token=API_TOKEN)
    poll_title = today_poll_title()
    poll_options = build_poll_options()
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
        disable_notification=False  # уведомление всем
    )
    await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())