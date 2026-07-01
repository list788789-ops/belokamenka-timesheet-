"""
Главный модуль: MAX-бот + планировщик (APScheduler).

Расписание:
  08:00 — fill_present(): всем "Я" на сегодня
  20:00 — вечерняя проверка:
            если за день ничего не менялось → спросить бригадира в MAX;
            нет ответа в течение WAIT_MINUTES → clear_day() (всем "Н").

Запуск:  python main.py
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import sheets

# --- MAX Bot API (библиотека maxapi, см. requirements.txt) ---
from maxapi import Bot, Dispatcher
from maxapi.types import MessageCreated, BotStarted, Command

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s — %(levelname)s — %(message)s")
log = logging.getLogger("timesheet")

# --- Конфиг из переменных окружения (задаются в Railway) ---
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")           # токен от @MasterBot
FOREMAN_CHAT_ID = int(os.getenv("FOREMAN_CHAT_ID", "0"))  # chat_id бригадира
TIMEZONE = os.getenv("TZ", "Europe/Moscow")
WAIT_MINUTES = int(os.getenv("WAIT_MINUTES", "60"))  # сколько ждать ответа вечером

bot = Bot(MAX_BOT_TOKEN)
dp = Dispatcher()

# Флаг: получили ли подтверждение от бригадира за текущий вечер
_confirmation_pending = False
_day_confirmed = False


# ============ Задачи по расписанию ============

async def morning_fill():
    """08:00 — поставить всем явку."""
    try:
        n = await asyncio.to_thread(sheets.fill_present)
        log.info("Утро: проставлено присутствие для %s сотрудников", n)
    except Exception as e:
        log.exception("Ошибка при утреннем заполнении: %s", e)


async def evening_check():
    """20:00 — проверить, велся ли табель сегодня."""
    global _confirmation_pending, _day_confirmed
    try:
        untouched = await asyncio.to_thread(sheets.is_untouched)
    except Exception as e:
        log.exception("Ошибка при вечерней проверке: %s", e)
        return

    if not untouched:
        log.info("Вечер: табель сегодня менялся — всё в порядке.")
        return

    # Ничего не менялось — спрашиваем бригадира
    log.info("Вечер: за день нет изменений, отправляю запрос бригадиру.")
    _confirmation_pending = True
    _day_confirmed = False

    today = datetime.now().strftime("%d.%m.%Y")
    if FOREMAN_CHAT_ID:
        await bot.send_message(
            chat_id=FOREMAN_CHAT_ID,
            text=(
                f"Табель за {today} сегодня не заполнялся.\n"
                f"Все вышли на работу? Ответьте /да или /нет в течение "
                f"{WAIT_MINUTES} минут.\n"
                f"Если ответа не будет — присутствие будет отменено."
            ),
        )

    # Ждём ответа
    await asyncio.sleep(WAIT_MINUTES * 60)

    if _confirmation_pending and not _day_confirmed:
        # Ответа не было — отменяем присутствие
        try:
            n = await asyncio.to_thread(sheets.clear_day)
            log.info("Нет подтверждения: присутствие отменено (%s сотр.)", n)
            if FOREMAN_CHAT_ID:
                await bot.send_message(
                    chat_id=FOREMAN_CHAT_ID,
                    text=f"Подтверждение не получено. Присутствие за {today} отменено (всем 'Н').",
                )
        except Exception as e:
            log.exception("Ошибка при отмене присутствия: %s", e)
    _confirmation_pending = False


# ============ Обработчики MAX-бота ============

@dp.message_created(Command("да"))
async def confirm_yes(event: MessageCreated):
    global _confirmation_pending, _day_confirmed
    if _confirmation_pending:
        _day_confirmed = True
        _confirmation_pending = False
        await event.message.answer("Принято. Присутствие за день подтверждено.")
    else:
        await event.message.answer("Сейчас нет активного запроса на подтверждение.")


@dp.message_created(Command("нет"))
async def confirm_no(event: MessageCreated):
    global _confirmation_pending, _day_confirmed
    if _confirmation_pending:
        _confirmation_pending = False
        _day_confirmed = False
        n = await asyncio.to_thread(sheets.clear_day)
        await event.message.answer(f"Понял. Присутствие отменено, всем проставлено 'Н' ({n}).")
    else:
        await event.message.answer("Сейчас нет активного запроса на подтверждение.")


@dp.bot_started()
async def on_bot_started(event: BotStarted):
    await bot.send_message(
        chat_id=event.chat_id,
        text="Бот табеля запущен. Команды: /chatid — узнать id чата.",
    )


@dp.message_created(Command("chatid"))
async def show_chat_id(event: MessageCreated):
    """Утилита: узнать chat_id (для настройки FOREMAN_CHAT_ID)."""
    chat_id = event.message.recipient.chat_id
    await event.message.answer(f"chat_id этого чата: {chat_id}")


# ============ Запуск ============

async def main():
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(morning_fill, CronTrigger(hour=8, minute=0))
    scheduler.add_job(evening_check, CronTrigger(hour=20, minute=0))
    scheduler.start()
    log.info("Планировщик запущен (TZ=%s). Бот стартует...", TIMEZONE)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
