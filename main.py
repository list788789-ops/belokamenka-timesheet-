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
from setup_dropdowns import setup_dropdowns
from reorganize import reorganize
from employees_sheet import create_employees_sheet

# --- MAX Bot API (библиотека maxapi, см. requirements.txt) ---
from maxapi import Bot, Dispatcher, F
from maxapi.types import MessageCreated, BotStarted, Command, MessageCallback, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

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


def _main_menu():
    """Стартовое меню бригадира."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📝 Отметить отсутствующих", payload="menu:mark"))
    kb.row(CallbackButton(text="📅 Табель за сегодня", payload="menu:today"))
    kb.row(CallbackButton(text="🚪 Оформить увольнение", payload="menu:fire"))
    kb.row(CallbackButton(text="📋 Список уволенных", payload="menu:fired"))
    return kb.as_markup()


@dp.bot_started()
async def on_bot_started(event: BotStarted):
    await bot.send_message(
        chat_id=event.chat_id,
        text="Бот табеля. Выберите действие:",
        attachments=[_main_menu()],
    )


@dp.message_created(Command("menu"))
async def show_menu(event: MessageCreated):
    await event.message.answer("Выберите действие:", attachments=[_main_menu()])


@dp.message_created(Command("chatid"))
async def show_chat_id(event: MessageCreated):
    """Утилита: узнать chat_id (для настройки FOREMAN_CHAT_ID)."""
    chat_id = event.message.recipient.chat_id
    await event.message.answer(f"chat_id этого чата: {chat_id}")


@dp.message_callback(F.callback.payload == "menu:today")
async def cb_today(event: MessageCallback):
    s = await asyncio.to_thread(sheets.day_summary)
    c = s["counts"]
    lines = [
        f"Табель за {_day_label(datetime.now().day)}:",
        f"Явка: {c.get('Я', 0)}   Неявка: {c.get('Н', 0)}   "
        f"Больничный: {c.get('Б', 0)}   Отпуск: {c.get('О', 0)}   "
        f"Выходной: {c.get('В', 0)}",
    ]
    if s["absent"]:
        lines.append("\nОтсутствуют:")
        for name, code in s["absent"]:
            lines.append(f"  • {name} — {code}")
    else:
        lines.append("\nВсе на месте.")
    await event.message.answer("\n".join(lines))


@dp.message_callback(F.callback.payload == "menu:mark")
async def cb_menu_mark(event: MessageCallback):
    _mark_session["day"] = datetime.now().day
    _mark_session["page"] = 0
    await _send_employee_list(event.message, _mark_session["day"], 0)


@dp.message_callback(F.callback.payload == "menu:fire")
async def cb_menu_fire(event: MessageCallback):
    _fire_session["page"] = 0
    _fire_session["day"] = None
    _fire_session["name"] = None
    await _send_fire_list(event.message, 0)


@dp.message_callback(F.callback.payload == "menu:fired")
async def cb_menu_fired(event: MessageCallback):
    fired = await asyncio.to_thread(sheets.get_fired)
    if not fired:
        await event.message.answer("Уволенных нет.")
        return
    lines = ["Уволенные сотрудники:"]
    for f in fired:
        d = f["fired_date"] or "—"
        lines.append(f"  ⚫ {f['name']} — уволен {d}")
    await event.message.answer("\n".join(lines))


# ---- Флоу увольнения ----
_fire_session = {"page": 0, "day": None, "name": None, "awaiting_day": False}


async def _send_fire_list(target, page: int):
    employees = await asyncio.to_thread(sheets.get_employees)
    total = len(employees)
    start = page * PAGE_SIZE
    chunk = employees[start:start + PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for i, name in enumerate(chunk, start=start):
        kb.row(CallbackButton(text=f"{i + 1}. {name}", payload=f"fire:{i}"))
    nav = []
    if start + PAGE_SIZE < total:
        nav.append(CallbackButton(text="Ещё ▼", payload=f"firepage:{page + 1}"))
    if page > 0:
        nav.append(CallbackButton(text="▲ Назад", payload=f"firepage:{page - 1}"))
    if nav:
        kb.row(*nav)
    kb.row(CallbackButton(text="✖ Отмена", payload="firecancel"))
    await target.answer("Кого увольняем?", attachments=[kb.as_markup()])


@dp.message_callback(F.callback.payload.startswith("firepage:"))
async def cb_fire_page(event: MessageCallback):
    page = int(event.callback.payload.split(":")[1])
    _fire_session["page"] = page
    await _send_fire_list(event.message, page)


@dp.message_callback(F.callback.payload.startswith("fire:"))
async def cb_fire_pick(event: MessageCallback):
    idx = int(event.callback.payload.split(":")[1])
    employees = await asyncio.to_thread(sheets.get_employees)
    if idx >= len(employees):
        await event.message.answer("Сотрудник не найден.")
        return
    _fire_session["name"] = employees[idx]
    _fire_session["awaiting_day"] = True
    await event.message.answer(
        f"Увольняем: {employees[idx]}\nВведите число месяца — дату увольнения:"
    )


@dp.message_callback(F.callback.payload == "fireconfirm")
async def cb_fire_confirm(event: MessageCallback):
    name = _fire_session["name"]
    day = _fire_session["day"]
    if not name or not day:
        await event.message.answer("Данные увольнения потеряны, начните заново.")
        return
    # 1. Помечаем уволенным
    ok = await asyncio.to_thread(sheets.fire_employee, name, day)
    if not ok:
        await event.message.answer("Не удалось обновить статус. Проверьте лист «Сотрудники».")
        return
    await event.message.answer(f"⚫ {name} уволен с {day:02d}.{datetime.now().month:02d}.")

    # 2. Формируем Excel-график (для будущей отправки на почту)
    safe = "".join(ch for ch in name if ch.isalnum() or ch in " _-").strip().replace(" ", "_")
    out_path = f"/tmp/Otchet_{safe}.xlsx"
    path = await asyncio.to_thread(sheets.build_work_report, name, out_path)
    if path:
        await event.message.answer(
            "График работы сформирован и готов к отправке бухгалтеру "
            "(почта будет подключена позже)."
        )
    else:
        await event.message.answer("График пуст — у сотрудника нет отметок.")


@dp.message_callback(F.callback.payload == "firecancel")
async def cb_fire_cancel(event: MessageCallback):
    _fire_session["name"] = None
    _fire_session["day"] = None
    _fire_session["awaiting_day"] = False
    await event.message.answer("Увольнение отменено.")


# ============ Отметка отсутствующих кнопками ============
# Простое состояние сессии бригадира: выбранный день и страница списка.
PAGE_SIZE = 10
_mark_session = {"day": None, "page": 0}
_STATUS_LABELS = {
    sheets.CODE_ABSENT: "Н (неявка)",
    sheets.CODE_SICK: "Б (больничный)",
    sheets.CODE_VACATION: "О (отпуск)",
}
_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _day_label(day: int) -> str:
    now = datetime.now()
    return f"{day} {_MONTHS_GEN[now.month - 1]}"


async def _send_employee_list(target, day: int, page: int):
    """Отправляет страницу списка сотрудников с кнопками."""
    employees = await asyncio.to_thread(sheets.get_employees)
    total = len(employees)
    start = page * PAGE_SIZE
    chunk = employees[start:start + PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for i, name in enumerate(chunk, start=start):
        kb.row(CallbackButton(text=f"{i + 1}. {name}", payload=f"emp:{i}"))

    # Навигация
    nav = []
    if start + PAGE_SIZE < total:
        nav.append(CallbackButton(text="Ещё ▼", payload=f"page:{page + 1}"))
    if page > 0:
        nav.append(CallbackButton(text="▲ Назад", payload=f"page:{page - 1}"))
    if nav:
        kb.row(*nav)
    kb.row(CallbackButton(text="📅 Другой день", payload="pickday"))
    kb.row(CallbackButton(text="✅ Завершить", payload="finish"))

    text = f"Отметка за {_day_label(day)}. Кто отсутствует?"
    await target.answer(text, attachments=[kb.as_markup()])


@dp.message_created(Command("отметить"))
async def start_marking(event: MessageCreated):
    if not _is_foreman(event):
        return
    _mark_session["day"] = datetime.now().day
    _mark_session["page"] = 0
    await _send_employee_list(event.message, _mark_session["day"], 0)


def _is_foreman(event) -> bool:
    """Проверка, что команду шлёт бригадир (по chat_id)."""
    if not FOREMAN_CHAT_ID:
        return True  # если не задан — не ограничиваем
    try:
        return event.message.recipient.chat_id == FOREMAN_CHAT_ID
    except Exception:
        return True


@dp.message_callback(F.callback.payload.startswith("page:"))
async def cb_page(event: MessageCallback):
    page = int(event.callback.payload.split(":")[1])
    _mark_session["page"] = page
    await _send_employee_list(event.message, _mark_session["day"], page)


@dp.message_callback(F.callback.payload.startswith("emp:"))
async def cb_employee(event: MessageCallback):
    idx = int(event.callback.payload.split(":")[1])
    employees = await asyncio.to_thread(sheets.get_employees)
    name = employees[idx] if idx < len(employees) else f"№{idx + 1}"

    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="Н", payload=f"set:{idx}:{sheets.CODE_ABSENT}"),
        CallbackButton(text="Б", payload=f"set:{idx}:{sheets.CODE_SICK}"),
        CallbackButton(text="О", payload=f"set:{idx}:{sheets.CODE_VACATION}"),
    )
    kb.row(CallbackButton(text="◀ К списку", payload=f"page:{_mark_session['page']}"))
    await event.message.answer(f"{name} — какой статус?", attachments=[kb.as_markup()])


@dp.message_callback(F.callback.payload.startswith("set:"))
async def cb_set_status(event: MessageCallback):
    _, idx_s, code = event.callback.payload.split(":")
    idx = int(idx_s)
    day = _mark_session["day"] or datetime.now().day
    date = datetime.now().replace(day=day)

    # Проверка перезаписи: если уже стоит Н/Б/О/В — предупреждаем
    current = await asyncio.to_thread(sheets.get_current_status, idx, date)
    if current in (sheets.CODE_ABSENT, sheets.CODE_SICK,
                   sheets.CODE_VACATION, sheets.CODE_WEEKEND):
        employees = await asyncio.to_thread(sheets.get_employees)
        name = employees[idx] if idx < len(employees) else f"№{idx + 1}"
        kb = InlineKeyboardBuilder()
        kb.row(
            CallbackButton(text="✅ Заменить", payload=f"force:{idx}:{code}"),
            CallbackButton(text="✖ Отмена", payload=f"page:{_mark_session['page']}"),
        )
        await event.message.answer(
            f"У {name} уже стоит «{current}». Заменить на «{code}»?",
            attachments=[kb.as_markup()],
        )
        return

    await _do_set(event, idx, code, date)


@dp.message_callback(F.callback.payload.startswith("force:"))
async def cb_force_status(event: MessageCallback):
    _, idx_s, code = event.callback.payload.split(":")
    idx = int(idx_s)
    day = _mark_session["day"] or datetime.now().day
    date = datetime.now().replace(day=day)
    await _do_set(event, idx, code, date)


async def _do_set(event, idx, code, date):
    name, _ = await asyncio.to_thread(sheets.set_status, idx, code, date)
    await event.message.answer(f"✔ {name} — {_STATUS_LABELS.get(code, code)}")
    await _send_employee_list(event.message, _mark_session["day"], _mark_session["page"])


@dp.message_callback(F.callback.payload == "pickday")
async def cb_pickday(event: MessageCallback):
    _mark_session["awaiting_day"] = True
    await event.message.answer("Введите число месяца (например, 15):")


@dp.message_callback(F.callback.payload == "finish")
async def cb_finish(event: MessageCallback):
    _mark_session["day"] = None
    _mark_session["page"] = 0
    await event.message.answer("Готово. Отметка завершена.")


@dp.message_created(F.message.body.text.regexp(r"^\d{1,2}$"))
async def on_day_number(event: MessageCreated):
    """Приём числа: либо дата увольнения, либо день для отметок."""
    day = int(event.message.body.text)

    # Приоритет — сессия увольнения
    if _fire_session.get("awaiting_day"):
        _fire_session["awaiting_day"] = False
        _fire_session["day"] = day
        name = _fire_session["name"]
        kb = InlineKeyboardBuilder()
        kb.row(
            CallbackButton(text="✅ Уволить", payload="fireconfirm"),
            CallbackButton(text="✖ Отмена", payload="firecancel"),
        )
        await event.message.answer(
            f"Уволить {name} с {day:02d}.{datetime.now().month:02d}?\n"
            f"Он исчезнет из списка отметок.",
            attachments=[kb.as_markup()],
        )
        return

    # Иначе — день для отметок
    if _mark_session.get("awaiting_day"):
        _mark_session["awaiting_day"] = False
        _mark_session["day"] = day
        _mark_session["page"] = 0
        await _send_employee_list(event.message, day, 0)


# ============ Запуск ============

async def main():
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")

    # Разовое создание листа «Сотрудники». Включается RUN_EMPLOYEES=1.
    if os.getenv("RUN_EMPLOYEES") == "1":
        try:
            n = await asyncio.to_thread(create_employees_sheet)
            log.info("Лист «Сотрудники» создан: %s активных.", n)
        except Exception as e:
            log.exception("Ошибка создания листа «Сотрудники»: %s", e)

    # Разовая реорганизация: № + сортировка ФИО + сдвиг дней.
    # Включается RUN_REORG=1. После успеха убери переменную.
    if os.getenv("RUN_REORG") == "1":
        try:
            n = await asyncio.to_thread(reorganize)
            log.info("Реорганизация выполнена: %s сотрудников.", n)
        except Exception as e:
            log.exception("Ошибка реорганизации: %s", e)

    # Разовая настройка выпадающих списков.
    # Включается переменной RUN_SETUP=1. После успеха убери её, чтобы
    # не гонять настройку при каждом рестарте.
    if os.getenv("RUN_SETUP") == "1":
        try:
            n = await asyncio.to_thread(setup_dropdowns)
            log.info("Выпадающие списки настроены на %s листах.", n)
        except Exception as e:
            log.exception("Ошибка настройки выпадающих списков: %s", e)

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(morning_fill, CronTrigger(hour=8, minute=0))
    scheduler.add_job(evening_check, CronTrigger(hour=20, minute=0))
    scheduler.start()
    log.info("Планировщик запущен (TZ=%s). Бот стартует...", TIMEZONE)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
