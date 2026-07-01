"""
Главный модуль: MAX-бот табеля (модель ДЕНЬ/НОЧЬ).

Логика:
  Утро (кнопка ☀️): бот сам ставит отдых тем, кто с ночи; прораб тапает
    присутствующих (ДЕНЬ=Д); оставшимся указывает причину (Н/Б/МЖ).
  Вечер (кнопка 🌙): прораб отмечает заступающих в ночь (НОЧЬ=НЧ).
  Межвахта (МЖ): запрос даты возврата; ежедневное напоминание в 09:00
    за 3 дня до возврата.

Запуск:  python main.py
"""

import asyncio
import logging
import os
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import sheets
from setup_dropdowns import setup_dropdowns
from reorganize import reorganize
from employees_sheet import create_employees_sheet
from rebuild_daynight import rebuild_daynight
from refresh_validation import refresh_validation

from maxapi import Bot, Dispatcher, F
from maxapi.types import MessageCreated, BotStarted, Command, MessageCallback, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s — %(levelname)s — %(message)s")
log = logging.getLogger("timesheet")

MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
FOREMAN_CHAT_ID = int(os.getenv("FOREMAN_CHAT_ID", "0"))
TIMEZONE = os.getenv("TZ", "Europe/Moscow")

bot = Bot(MAX_BOT_TOKEN)
dp = Dispatcher()

PAGE_SIZE = 10
_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _day_label(day: int) -> str:
    return f"{day} {_MONTHS_GEN[datetime.now().month - 1]}"


def _is_foreman(event) -> bool:
    if not FOREMAN_CHAT_ID:
        return True
    try:
        return event.message.recipient.chat_id == FOREMAN_CHAT_ID
    except Exception:
        return True


# ================= МЕНЮ =================

def _main_menu():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="☀️ Утро (присутствующие)", payload="menu:morning"))
    kb.row(CallbackButton(text="🌙 Вечер (ночная смена)", payload="menu:evening"))
    kb.row(CallbackButton(text="📅 Табель за сегодня", payload="menu:today"))
    kb.row(CallbackButton(text="🚪 Оформить увольнение", payload="menu:fire"))
    kb.row(CallbackButton(text="📋 Список уволенных", payload="menu:fired"))
    return kb.as_markup()


@dp.bot_started()
async def on_bot_started(event: BotStarted):
    await bot.send_message(chat_id=event.chat_id,
                           text="Бот табеля. Выберите действие:",
                           attachments=[_main_menu()])


@dp.message_created(Command("menu"))
async def show_menu(event: MessageCreated):
    await event.message.answer("Выберите действие:", attachments=[_main_menu()])


@dp.message_created(Command("chatid"))
async def show_chat_id(event: MessageCreated):
    await event.message.answer(f"chat_id этого чата: {event.message.recipient.chat_id}")


# ================= ТАБЕЛЬ ЗА СЕГОДНЯ =================

@dp.message_callback(F.callback.payload == "menu:today")
async def cb_today(event: MessageCallback):
    s = await asyncio.to_thread(sheets.day_summary)
    lines = [
        f"Табель за {_day_label(datetime.now().day)}:",
        f"☀️ День: {s['day']}   🌙 Ночь: {s['night']}   😴 Отдых: {s['rest']}",
        f"🤒 Больн.: {s['sick']}   ✈️ Межвахта: {s['rotation']}   ❌ Неявка: {s['absent']}",
    ]
    if s["absent_list"]:
        lines.append("\nОтсутствуют/особое:")
        for name, code in s["absent_list"]:
            lines.append(f"  • {name} — {code}")
    await event.message.answer("\n".join(lines))


# ================= УТРО =================
# Сессия утренней отметки
_morning = {"page": 0, "reason_mode": False}


async def _edit_or_send(event_or_target, text, markup=None):
    """
    Пытается отредактировать сообщение (callback), иначе шлёт новое.
    event_or_target — либо MessageCallback (есть .message + правка),
    либо объект message (метод .answer).
    """
    attachments = [markup] if markup else None
    # Попытка редактирования (для callback-событий)
    msg = getattr(event_or_target, "message", event_or_target)
    for method in ("edit", "edit_text", "edit_message"):
        fn = getattr(msg, method, None)
        if callable(fn):
            try:
                if attachments:
                    await fn(text, attachments=attachments)
                else:
                    await fn(text)
                return
            except TypeError:
                try:
                    await fn(text)
                    return
                except Exception:
                    pass
            except Exception:
                pass
    # Фолбэк — новое сообщение
    if attachments:
        await msg.answer(text, attachments=attachments)
    else:
        await msg.answer(text)


async def _send_morning_list(target, page: int, edit_event=None):
    """Список неотмеченных днём. Если edit_event задан — правит на месте."""
    unmarked = await asyncio.to_thread(sheets.get_unmarked_day)
    total = len(unmarked)
    start = page * PAGE_SIZE
    chunk = unmarked[start:start + PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for name in chunk:
        kb.row(CallbackButton(text=name, payload=f"mday:{name}"))
    nav = []
    if start + PAGE_SIZE < total:
        nav.append(CallbackButton(text="Ещё ▼", payload=f"mpage:{page + 1}"))
    if page > 0:
        nav.append(CallbackButton(text="▲ Назад", payload=f"mpage:{page - 1}"))
    if nav:
        kb.row(*nav)
    kb.row(CallbackButton(text="🧹 Очистить сотрудника", payload="mclear"))
    kb.row(CallbackButton(text="✅ Отметил всех присутствующих", payload="mdone"))
    txt = f"☀️ Утро. Отметьте, кто на месте (осталось {total}):"
    if edit_event is not None:
        await _edit_or_send(edit_event, txt, kb.as_markup())
    else:
        await target.answer(txt, attachments=[kb.as_markup()])


@dp.message_callback(F.callback.payload == "menu:morning")
async def cb_menu_morning(event: MessageCallback):
    if not _is_foreman(event):
        return
    _morning["page"] = 0
    _morning["reason_mode"] = False

    # Проверка прерванной отметки
    prog = await asyncio.to_thread(sheets.morning_progress)
    if prog["interrupted"]:
        kb = InlineKeyboardBuilder()
        kb.row(
            CallbackButton(text="▶️ Продолжить", payload="mcontinue"),
            CallbackButton(text="🔄 Начать заново", payload="mrestart"),
        )
        await event.message.answer(
            f"Отметка за сегодня не завершена: отмечено {prog['marked']}, "
            f"осталось {prog['unmarked']}. Продолжить?",
            attachments=[kb.as_markup()])
        return

    await _morning_start(event.message)


async def _morning_start(target):
    """Начало утренней отметки: автоотдых с ночи + список."""
    rest_names = await asyncio.to_thread(sheets.get_night_rest)
    for nm in rest_names:
        await asyncio.to_thread(sheets.set_rest, nm)
    if rest_names:
        await target.answer("С ночи отдыхают (проставлен отдых):\n" +
                            "\n".join(f"  😴 {n}" for n in rest_names))
    await _send_morning_list(target, 0)


@dp.message_callback(F.callback.payload == "mcontinue")
async def cb_morning_continue(event: MessageCallback):
    _morning["page"] = 0
    await _send_morning_list(event.message, 0)


@dp.message_callback(F.callback.payload == "mrestart")
async def cb_morning_restart(event: MessageCallback):
    _morning["page"] = 0
    await _morning_start(event.message)


@dp.message_callback(F.callback.payload == "mclear")
async def cb_morning_clear_list(event: MessageCallback):
    """Список отмеченных днём для очистки."""
    marked = await asyncio.to_thread(sheets.get_marked_day)
    if not marked:
        await event.message.answer("Пока некого очищать.")
        return
    kb = InlineKeyboardBuilder()
    for name in marked[:PAGE_SIZE]:
        kb.row(CallbackButton(text=name, payload=f"mclr:{name}"))
    kb.row(CallbackButton(text="◀ К отметке", payload="mpage:0"))
    await event.message.answer("Кого очистить (дневной слот)?",
                               attachments=[kb.as_markup()])


@dp.message_callback(F.callback.payload.startswith("mclr:"))
async def cb_morning_clear_do(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    await asyncio.to_thread(sheets.clear_day_slot, name)
    await _send_morning_list(event.message, _morning["page"], edit_event=event)


@dp.message_callback(F.callback.payload.startswith("mpage:"))
async def cb_morning_page(event: MessageCallback):
    page = int(event.callback.payload.split(":")[1])
    _morning["page"] = page
    await _send_morning_list(event.message, page, edit_event=event)


@dp.message_callback(F.callback.payload.startswith("mday:"))
async def cb_mark_day(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    await asyncio.to_thread(sheets.mark_day, name)
    await _send_morning_list(event.message, _morning["page"], edit_event=event)


@dp.message_callback(F.callback.payload == "mdone")
async def cb_morning_done(event: MessageCallback):
    """Присутствующие отмечены — переходим к причинам для оставшихся."""
    remaining = await asyncio.to_thread(sheets.get_unmarked_day)
    if not remaining:
        await event.message.answer("Все отмечены. Утро завершено.")
        return
    _morning["reason_mode"] = True
    await _send_reason_list(event.message)


async def _send_reason_list(target, edit_event=None):
    """Оставшиеся без отметки → выбор причины."""
    remaining = await asyncio.to_thread(sheets.get_unmarked_day)
    if not remaining:
        txt = "Причины проставлены всем. Утро завершено."
        if edit_event is not None:
            await _edit_or_send(edit_event, txt)
        else:
            await target.answer(txt)
        return
    kb = InlineKeyboardBuilder()
    for name in remaining[:PAGE_SIZE]:
        kb.row(CallbackButton(text=name, payload=f"rsn:{name}"))
    txt = (f"Укажите причину отсутствия (осталось {len(remaining)}). "
           f"Нажмите на сотрудника:")
    if edit_event is not None:
        await _edit_or_send(edit_event, txt, kb.as_markup())
    else:
        await target.answer(txt, attachments=[kb.as_markup()])


@dp.message_callback(F.callback.payload.startswith("rsn:"))
async def cb_pick_reason(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="❌ Неявка", payload=f"setrsn:{name}:{sheets.DN_ABSENT}"),
        CallbackButton(text="🤒 Больничный", payload=f"setrsn:{name}:{sheets.DN_SICK}"),
    )
    kb.row(
        CallbackButton(text="✈️ Межвахта", payload=f"setrsn:{name}:{sheets.DN_ROTATION}"),
        CallbackButton(text="📋 Мигр.учёт", payload=f"setrsn:{name}:{sheets.DN_MIGR}"),
    )
    await _edit_or_send(event, f"{name} — причина?", kb.as_markup())


@dp.message_callback(F.callback.payload.startswith("setrsn:"))
async def cb_set_reason(event: MessageCallback):
    _, name, code = event.callback.payload.split(":", 2)
    if code == sheets.DN_ROTATION:
        # межвахта → спрашиваем дату возврата
        _rotation_wait["name"] = name
        _rotation_wait["active"] = True
        await asyncio.to_thread(sheets.set_reason, name, code)
        await _edit_or_send(
            event,
            f"{name}: межвахта. До какого числа? Введите дату возврата (ДД.ММ):")
        return
    await asyncio.to_thread(sheets.set_reason, name, code)
    await _send_reason_list(event.message, edit_event=event)


# ================= ВЕЧЕР =================
_evening = {"page": 0}


async def _send_evening_list(target, page: int, edit_event=None):
    """Список тех, кто НЕ работал днём — их можно в ночь."""
    candidates = await asyncio.to_thread(sheets.get_not_worked_day)
    total = len(candidates)
    start = page * PAGE_SIZE
    chunk = candidates[start:start + PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for name in chunk:
        kb.row(CallbackButton(text=name, payload=f"mnight:{name}"))
    nav = []
    if start + PAGE_SIZE < total:
        nav.append(CallbackButton(text="Ещё ▼", payload=f"epage:{page + 1}"))
    if page > 0:
        nav.append(CallbackButton(text="▲ Назад", payload=f"epage:{page - 1}"))
    if nav:
        kb.row(*nav)
    kb.row(CallbackButton(text="🧹 Очистить сотрудника", payload="eclear"))
    kb.row(CallbackButton(text="✅ Готово", payload="edone"))
    txt = f"🌙 Вечер. Кто заступает в ночь? (доступно {total})"
    if edit_event is not None:
        await _edit_or_send(edit_event, txt, kb.as_markup())
    else:
        await target.answer(txt, attachments=[kb.as_markup()])


@dp.message_callback(F.callback.payload == "eclear")
async def cb_evening_clear_list(event: MessageCallback):
    """Список отмеченных в ночь для очистки."""
    marked = await asyncio.to_thread(sheets.get_marked_night)
    if not marked:
        await event.message.answer("Пока некого очищать.")
        return
    kb = InlineKeyboardBuilder()
    for name in marked[:PAGE_SIZE]:
        kb.row(CallbackButton(text=name, payload=f"eclr:{name}"))
    kb.row(CallbackButton(text="◀ К отметке", payload="epage:0"))
    await _edit_or_send(event, "Кого очистить (ночной слот)?", kb.as_markup())


@dp.message_callback(F.callback.payload.startswith("eclr:"))
async def cb_evening_clear_do(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    await asyncio.to_thread(sheets.clear_night_slot, name)
    await _send_evening_list(event.message, _evening["page"], edit_event=event)


@dp.message_callback(F.callback.payload == "menu:evening")
async def cb_menu_evening(event: MessageCallback):
    if not _is_foreman(event):
        return
    _evening["page"] = 0
    await _send_evening_list(event.message, 0)


@dp.message_callback(F.callback.payload.startswith("epage:"))
async def cb_evening_page(event: MessageCallback):
    page = int(event.callback.payload.split(":")[1])
    _evening["page"] = page
    await _send_evening_list(event.message, page, edit_event=event)


@dp.message_callback(F.callback.payload.startswith("mnight:"))
async def cb_mark_night(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    await asyncio.to_thread(sheets.mark_night, name)
    await _send_evening_list(event.message, _evening["page"], edit_event=event)


@dp.message_callback(F.callback.payload == "edone")
async def cb_evening_done(event: MessageCallback):
    await event.message.answer("🌙 Вечерняя отметка завершена.")


# ================= УВОЛЬНЕНИЕ =================
_fire_session = {"page": 0, "day": None, "name": None, "awaiting_day": False}


@dp.message_callback(F.callback.payload == "menu:fire")
async def cb_menu_fire(event: MessageCallback):
    if not _is_foreman(event):
        return
    _fire_session.update({"page": 0, "day": None, "name": None, "awaiting_day": False})
    await _send_fire_list(event.message, 0)


@dp.message_callback(F.callback.payload == "menu:fired")
async def cb_menu_fired(event: MessageCallback):
    fired = await asyncio.to_thread(sheets.get_fired)
    if not fired:
        await event.message.answer("Уволенных нет.")
        return
    lines = ["Уволенные сотрудники:"]
    for f in fired:
        lines.append(f"  ⚫ {f['name']} — уволен {f['fired_date'] or '—'}")
    await event.message.answer("\n".join(lines))


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
        f"Увольняем: {employees[idx]}\nВведите число месяца — дату увольнения:")


@dp.message_callback(F.callback.payload == "fireconfirm")
async def cb_fire_confirm(event: MessageCallback):
    name = _fire_session["name"]
    day = _fire_session["day"]
    if not name or not day:
        await event.message.answer("Данные увольнения потеряны, начните заново.")
        return
    ok = await asyncio.to_thread(sheets.fire_employee, name, day)
    if not ok:
        await event.message.answer("Не удалось обновить статус.")
        return
    await event.message.answer(f"⚫ {name} уволен с {day:02d}.{datetime.now().month:02d}.")

    safe = "".join(ch for ch in name if ch.isalnum() or ch in " _-").strip().replace(" ", "_")
    out_path = f"/tmp/Otchet_{safe}.xlsx"
    path = await asyncio.to_thread(sheets.build_work_report, name, out_path)
    if path:
        await event.message.answer(
            "График работы сформирован и готов к отправке бухгалтеру "
            "(почта будет подключена позже).")
    else:
        await event.message.answer("График пуст — у сотрудника нет отметок.")


@dp.message_callback(F.callback.payload == "firecancel")
async def cb_fire_cancel(event: MessageCallback):
    _fire_session.update({"name": None, "day": None, "awaiting_day": False})
    await event.message.answer("Увольнение отменено.")


# ================= ВВОД ЧИСЕЛ / ДАТ =================
_rotation_wait = {"name": None, "active": False}


@dp.message_created(F.message.body.text.regexp(r"^\d{1,2}\.\d{1,2}$"))
async def on_date_ddmm(event: MessageCreated):
    """Дата возврата с межвахты (ДД.ММ)."""
    if not _rotation_wait.get("active"):
        return
    name = _rotation_wait["name"]
    _rotation_wait["active"] = False
    _rotation_wait["name"] = None
    await asyncio.to_thread(sheets.set_rotation_return, name, event.message.body.text)
    await event.message.answer(
        f"✔ {name}: межвахта до {event.message.body.text}. "
        f"Напомню за 3 дня до возврата.")
    await _send_reason_list(event.message)


@dp.message_created(F.message.body.text.regexp(r"^\d{1,2}$"))
async def on_day_number(event: MessageCreated):
    """Число — дата увольнения."""
    day = int(event.message.body.text)
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
            attachments=[kb.as_markup()])


# ================= НАПОМИНАНИЯ О МЕЖВАХТЕ (09:00) =================

async def rotation_reminders_job():
    try:
        reminders = await asyncio.to_thread(sheets.get_rotation_reminders, 3)
    except Exception as e:
        log.exception("Ошибка проверки межвахты: %s", e)
        return
    if not reminders and FOREMAN_CHAT_ID:
        return
    for r in reminders:
        try:
            await bot.send_message(
                chat_id=FOREMAN_CHAT_ID,
                text=(f"✈️ {r['name']} возвращается {r['return_date']}. "
                      f"Закажите билеты и обратите внимание на отметки."))
        except Exception:
            log.exception("Не удалось отправить напоминание о межвахте")


# ================= ЗАПУСК =================

async def main():
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан")

    if os.getenv("RUN_REBUILD_DN") == "1":
        try:
            n = await asyncio.to_thread(rebuild_daynight)
            log.info("Структура ДЕНЬ/НОЧЬ пересоздана: %s сотрудников.", n)
        except Exception as e:
            log.exception("Ошибка пересоздания структуры: %s", e)

    if os.getenv("RUN_EMPLOYEES") == "1":
        try:
            n = await asyncio.to_thread(create_employees_sheet)
            log.info("Лист «Сотрудники» создан: %s активных.", n)
        except Exception as e:
            log.exception("Ошибка создания листа «Сотрудники»: %s", e)

    if os.getenv("RUN_REORG") == "1":
        try:
            n = await asyncio.to_thread(reorganize)
            log.info("Реорганизация выполнена: %s сотрудников.", n)
        except Exception as e:
            log.exception("Ошибка реорганизации: %s", e)

    if os.getenv("RUN_SETUP") == "1":
        try:
            n = await asyncio.to_thread(setup_dropdowns)
            log.info("Выпадающие списки настроены на %s листах.", n)
        except Exception as e:
            log.exception("Ошибка настройки списков: %s", e)

    if os.getenv("RUN_REFRESH_DV") == "1":
        try:
            n = await asyncio.to_thread(refresh_validation)
            log.info("Выпадающие списки обновлены (МУ) на 12 листах.")
        except Exception as e:
            log.exception("Ошибка обновления списков: %s", e)

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(rotation_reminders_job, CronTrigger(hour=9, minute=0))
    scheduler.start()
    log.info("Планировщик запущен (TZ=%s). Бот стартует...", TIMEZONE)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
