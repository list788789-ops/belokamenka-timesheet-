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
from setup_users import setup_users

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


async def _is_foreman(event) -> bool:
    """
    Разрешён ли пользователь. Если его нет в списке — шлём заявку админам
    и возвращаем False.
    """
    cid = _chat_id(event)
    allowed = await asyncio.to_thread(sheets.is_allowed, cid)
    if allowed:
        return True
    # Незнакомый — отправляем заявку админам
    await _send_access_request(event, cid)
    return False


async def _send_access_request(event, cid: int):
    """Сообщает пользователю о заявке и шлёт админам кнопку добавления."""
    try:
        await event.message.answer(
            "Вы не в списке пользователей. Запрос на добавление отправлен админу.")
    except Exception:
        pass
    admins = await asyncio.to_thread(sheets.get_admins)
    for admin_id in admins:
        kb = InlineKeyboardBuilder()
        kb.row(CallbackButton(text="➕ Добавить прораба", payload=f"adduser:{cid}"))
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=f"Запрос доступа. Новый пользователь chat_id: {cid}",
                attachments=[kb.as_markup()])
        except Exception:
            log.warning("Не удалось уведомить админа %s", admin_id)


# ================= МЕНЮ =================

def _main_menu():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="☀️ Утро (присутствующие)", payload="menu:morning"))
    kb.row(CallbackButton(text="🌙 Вечер (ночная смена)", payload="menu:evening"))
    kb.row(CallbackButton(text="📅 Табель за сегодня", payload="menu:today"))
    kb.row(CallbackButton(text="🚪 Оформить увольнение", payload="menu:fire"))
    kb.row(CallbackButton(text="📋 Список уволенных", payload="menu:fired"))
    kb.row(CallbackButton(text="➕ Добавить сотрудника", payload="menu:addemp"))
    kb.row(CallbackButton(text="🧹 Очистить весь день (тест)", payload="menu:clearall"))
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


@dp.message_callback(F.callback.payload.startswith("adduser:"))
async def cb_add_user(event: MessageCallback):
    """Админ добавляет нового прораба по заявке."""
    admin_id = _chat_id(event)
    role = await asyncio.to_thread(sheets.get_role, admin_id)
    if role != sheets.ROLE_ADMIN:
        await event.message.answer("Только админ может добавлять пользователей.")
        return
    new_id = int(event.callback.payload.split(":")[1])
    ok = await asyncio.to_thread(sheets.add_user, new_id, "Прораб", sheets.ROLE_FOREMAN)
    if ok:
        await _finish(event, f"✅ Пользователь {new_id} добавлен как прораб.")
        try:
            await bot.send_message(chat_id=new_id,
                                   text="Вам открыт доступ. Отправьте /menu.")
        except Exception:
            pass
    else:
        await _finish(event, f"Пользователь {new_id} уже в списке.")


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


# ================= СЕССИИ ПО ПОЛЬЗОВАТЕЛЯМ =================
# Каждый прораб имеет своё состояние, чтобы не перебивать других.
_sessions = {}


def _new_session():
    return {
        "morning": {"page": 0, "reason_mode": False},
        "evening": {"page": 0},
        "clear_m": {"page": 0},
        "clear_e": {"page": 0},
        "fire": {"page": 0, "day": None, "name": None, "awaiting_day": False},
        "rotation": {"name": None, "active": False},
        "addemp": {"awaiting": False},
    }


def _chat_id(event):
    """Достаёт chat_id из события (callback или message)."""
    try:
        msg = getattr(event, "message", event)
        return msg.recipient.chat_id
    except Exception:
        return 0


def _sess(event):
    """Возвращает состояние сессии текущего пользователя."""
    cid = _chat_id(event)
    if cid not in _sessions:
        _sessions[cid] = _new_session()
    return _sessions[cid]


async def _edit_or_send(event_or_target, text, markup=None):
    """
    Редактирует сообщение (метод edit), при неудаче шлёт новое.
    markup=None — клавиатуру не трогаем; пустой markup — убираем кнопки.
    """
    attachments = [markup] if markup is not None else None
    msg = getattr(event_or_target, "message", event_or_target)

    candidates = []
    for obj in (event_or_target, msg):
        fn = getattr(obj, "edit", None)
        if callable(fn):
            candidates.append((obj, fn))

    for obj, fn in candidates:
        try:
            if attachments is not None:
                await fn(text, attachments=attachments)
            else:
                await fn(text)
            return
        except Exception as e:
            log.warning("edit failed: %s", e)

    if attachments is not None:
        await msg.answer(text, attachments=attachments)
    else:
        await msg.answer(text)


async def _finish(event_or_target, text):
    """
    Финальное сообщение без кнопок: удаляет текущее сообщение с кнопками
    и шлёт чистый текст. Если delete не поддержан — просто шлёт новое.
    """
    msg = getattr(event_or_target, "message", event_or_target)
    for obj in (event_or_target, msg):
        fn = getattr(obj, "delete", None)
        if callable(fn):
            try:
                await fn()
                break
            except Exception as e:
                log.warning("delete failed: %s", e)
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
    if not await _is_foreman(event):
        return
    s = _sess(event)
    s["morning"]["page"] = 0
    s["morning"]["reason_mode"] = False

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
    _sess(event)["morning"]["page"] = 0
    await _send_morning_list(event.message, 0)


@dp.message_callback(F.callback.payload == "mrestart")
async def cb_morning_restart(event: MessageCallback):
    _sess(event)["morning"]["page"] = 0
    await _morning_start(event.message)


@dp.message_callback(F.callback.payload == "mclear")
async def cb_morning_clear_list(event: MessageCallback):
    _sess(event)["clear_m"]["page"] = 0
    await _send_morning_clear_list(event, 0)


async def _send_morning_clear_list(event, page: int):
    marked = await asyncio.to_thread(sheets.get_marked_day)
    if not marked:
        await _edit_or_send(event, "Пока некого очищать.")
        return
    total = len(marked)
    start = page * PAGE_SIZE
    chunk = marked[start:start + PAGE_SIZE]
    kb = InlineKeyboardBuilder()
    for name in chunk:
        kb.row(CallbackButton(text=name, payload=f"mclr:{name}"))
    nav = []
    if start + PAGE_SIZE < total:
        nav.append(CallbackButton(text="Ещё ▼", payload=f"mclrpage:{page + 1}"))
    if page > 0:
        nav.append(CallbackButton(text="▲ Назад", payload=f"mclrpage:{page - 1}"))
    if nav:
        kb.row(*nav)
    kb.row(CallbackButton(text="◀ К отметке", payload="mpage:0"))
    await _edit_or_send(event, f"Кого очистить (дневной слот)? Всего: {total}",
                        kb.as_markup())


@dp.message_callback(F.callback.payload.startswith("mclrpage:"))
async def cb_morning_clear_page(event: MessageCallback):
    page = int(event.callback.payload.split(":")[1])
    _sess(event)["clear_m"]["page"] = page
    await _send_morning_clear_list(event, page)


@dp.message_callback(F.callback.payload.startswith("mclr:"))
async def cb_morning_clear_do(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    await asyncio.to_thread(sheets.clear_day_slot, name)
    await _send_morning_clear_list(event, _sess(event)["clear_m"]["page"])


@dp.message_callback(F.callback.payload.startswith("mpage:"))
async def cb_morning_page(event: MessageCallback):
    page = int(event.callback.payload.split(":")[1])
    _sess(event)["morning"]["page"] = page
    await _send_morning_list(event.message, page, edit_event=event)


@dp.message_callback(F.callback.payload.startswith("mday:"))
async def cb_mark_day(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    # Проверка конфликтов день/ночь
    warn = await asyncio.to_thread(sheets.check_day_conflict, name)
    if warn:
        kb = InlineKeyboardBuilder()
        kb.row(
            CallbackButton(text="✅ Всё равно день", payload=f"fday:{name}"),
            CallbackButton(text="✖ Отмена", payload=f"mpage:{_sess(event)['morning']['page']}"),
        )
        await _edit_or_send(event, f"⚠️ {warn}\nВсё равно поставить день?", kb.as_markup())
        return
    await asyncio.to_thread(sheets.mark_day, name)
    await _send_morning_list(event.message, _sess(event)["morning"]["page"], edit_event=event)


@dp.message_callback(F.callback.payload.startswith("fday:"))
async def cb_force_day(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    await asyncio.to_thread(sheets.mark_day, name)
    await _send_morning_list(event.message, _sess(event)["morning"]["page"], edit_event=event)


@dp.message_callback(F.callback.payload == "mdone")
async def cb_morning_done(event: MessageCallback):
    """Присутствующие отмечены — переходим к причинам для оставшихся."""
    remaining = await asyncio.to_thread(sheets.get_unmarked_day)
    if not remaining:
        await _finish(event, "Все отмечены. Утро завершено.")
        return
    _sess(event)["morning"]["reason_mode"] = True
    await _send_reason_list(event.message, edit_event=event)


async def _send_reason_list(target, edit_event=None, page: int = 0):
    """Оставшиеся без отметки → выбор причины (с пагинацией и «Завершить»)."""
    remaining = await asyncio.to_thread(sheets.get_unmarked_day)
    if not remaining:
        txt = "Причины проставлены всем. Утро завершено."
        if edit_event is not None:
            await _finish(edit_event, txt)
        else:
            await target.answer(txt)
        return
    total = len(remaining)
    start = page * PAGE_SIZE
    chunk = remaining[start:start + PAGE_SIZE]
    kb = InlineKeyboardBuilder()
    for name in chunk:
        kb.row(CallbackButton(text=name, payload=f"rsn:{name}"))
    nav = []
    if start + PAGE_SIZE < total:
        nav.append(CallbackButton(text="Ещё ▼", payload=f"rsnpage:{page + 1}"))
    if page > 0:
        nav.append(CallbackButton(text="▲ Назад", payload=f"rsnpage:{page - 1}"))
    if nav:
        kb.row(*nav)
    kb.row(CallbackButton(text="✅ Завершить (остальным неявка)", payload="rsnfinish"))
    txt = (f"Укажите причину отсутствия (осталось {total}). "
           f"Нажмите на сотрудника, либо «Завершить»:")
    if edit_event is not None:
        await _edit_or_send(edit_event, txt, kb.as_markup())
    else:
        await target.answer(txt, attachments=[kb.as_markup()])


@dp.message_callback(F.callback.payload.startswith("rsnpage:"))
async def cb_reason_page(event: MessageCallback):
    page = int(event.callback.payload.split(":")[1])
    await _send_reason_list(event.message, edit_event=event, page=page)


@dp.message_callback(F.callback.payload == "rsnfinish")
async def cb_reason_finish(event: MessageCallback):
    remaining = await asyncio.to_thread(sheets.get_unmarked_day)
    if not remaining:
        await _edit_or_send(event, "Все размечены. Утро завершено.")
        return
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Да, неявка", payload="rsnfinish_yes"),
        CallbackButton(text="✖ Отмена", payload="rsnfinish_no"),
    )
    await _edit_or_send(
        event,
        f"Всем непроставленным ({len(remaining)} чел.) будет проставлена "
        f"неявка (Н). Продолжить?",
        kb.as_markup())


@dp.message_callback(F.callback.payload == "rsnfinish_yes")
async def cb_reason_finish_yes(event: MessageCallback):
    n = await asyncio.to_thread(sheets.fill_unmarked_absent)
    await _finish(event, f"Утро завершено. Неявка проставлена: {n} чел.")


@dp.message_callback(F.callback.payload == "rsnfinish_no")
async def cb_reason_finish_no(event: MessageCallback):
    await _send_reason_list(event.message, edit_event=event)


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
        rot = _sess(event)["rotation"]
        rot["name"] = name
        rot["active"] = True
        await asyncio.to_thread(sheets.set_reason, name, code)
        await _edit_or_send(
            event,
            f"{name}: межвахта. До какого числа? Введите дату возврата (ДД.ММ):")
        return
    await asyncio.to_thread(sheets.set_reason, name, code)
    await _send_reason_list(event.message, edit_event=event)


# ================= ВЕЧЕР =================


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
    _sess(event)["clear_e"]["page"] = 0
    await _send_evening_clear_list(event, 0)


async def _send_evening_clear_list(event, page: int):
    marked = await asyncio.to_thread(sheets.get_marked_night)
    if not marked:
        await _edit_or_send(event, "Пока некого очищать.")
        return
    total = len(marked)
    start = page * PAGE_SIZE
    chunk = marked[start:start + PAGE_SIZE]
    kb = InlineKeyboardBuilder()
    for name in chunk:
        kb.row(CallbackButton(text=name, payload=f"eclr:{name}"))
    nav = []
    if start + PAGE_SIZE < total:
        nav.append(CallbackButton(text="Ещё ▼", payload=f"eclrpage:{page + 1}"))
    if page > 0:
        nav.append(CallbackButton(text="▲ Назад", payload=f"eclrpage:{page - 1}"))
    if nav:
        kb.row(*nav)
    kb.row(CallbackButton(text="◀ К отметке", payload="epage:0"))
    await _edit_or_send(event, f"Кого очистить (ночной слот)? Всего: {total}",
                        kb.as_markup())


@dp.message_callback(F.callback.payload.startswith("eclrpage:"))
async def cb_evening_clear_page(event: MessageCallback):
    page = int(event.callback.payload.split(":")[1])
    _sess(event)["clear_e"]["page"] = page
    await _send_evening_clear_list(event, page)


@dp.message_callback(F.callback.payload.startswith("eclr:"))
async def cb_evening_clear_do(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    await asyncio.to_thread(sheets.clear_night_slot, name)
    await _send_evening_clear_list(event, _sess(event)["clear_e"]["page"])


@dp.message_callback(F.callback.payload == "menu:evening")
async def cb_menu_evening(event: MessageCallback):
    if not await _is_foreman(event):
        return
    _sess(event)["evening"]["page"] = 0
    await _send_evening_list(event.message, 0)


@dp.message_callback(F.callback.payload.startswith("epage:"))
async def cb_evening_page(event: MessageCallback):
    page = int(event.callback.payload.split(":")[1])
    _sess(event)["evening"]["page"] = page
    await _send_evening_list(event.message, page, edit_event=event)


@dp.message_callback(F.callback.payload.startswith("mnight:"))
async def cb_mark_night(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    warn = await asyncio.to_thread(sheets.check_night_conflict, name)
    if warn:
        kb = InlineKeyboardBuilder()
        kb.row(
            CallbackButton(text="✅ Всё равно ночь", payload=f"fnight:{name}"),
            CallbackButton(text="✖ Отмена", payload=f"epage:{_sess(event)['evening']['page']}"),
        )
        await _edit_or_send(event, f"⚠️ {warn}\nВсё равно поставить ночь?", kb.as_markup())
        return
    await asyncio.to_thread(sheets.mark_night, name)
    await _send_evening_list(event.message, _sess(event)["evening"]["page"], edit_event=event)


@dp.message_callback(F.callback.payload.startswith("fnight:"))
async def cb_force_night(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    await asyncio.to_thread(sheets.mark_night, name)
    await _send_evening_list(event.message, _sess(event)["evening"]["page"], edit_event=event)


@dp.message_callback(F.callback.payload == "edone")
async def cb_evening_done(event: MessageCallback):
    await _finish(event, "🌙 Вечерняя отметка завершена.")


# ================= УВОЛЬНЕНИЕ =================


@dp.message_callback(F.callback.payload == "menu:fire")
async def cb_menu_fire(event: MessageCallback):
    if not await _is_foreman(event):
        return
    _sess(event)["fire"].update({"page": 0, "day": None, "name": None, "awaiting_day": False})
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


@dp.message_callback(F.callback.payload == "menu:addemp")
async def cb_menu_addemp(event: MessageCallback):
    if not await _is_foreman(event):
        return
    _sess(event)["addemp"]["awaiting"] = True
    await event.message.answer(
        "Введите ФИО нового сотрудника (Фамилия Имя Отчество):")


@dp.message_created(F.message.body.text.regexp(r"^[А-ЯЁа-яё]+\s+[А-ЯЁа-яё]"))
async def on_new_employee_name(event: MessageCreated):
    """Приём ФИО нового сотрудника (текст с 2+ слов кириллицей)."""
    s = _sess(event)
    if not s["addemp"].get("awaiting"):
        return
    s["addemp"]["awaiting"] = False
    name = " ".join(event.message.body.text.split())
    exists = await asyncio.to_thread(sheets.employee_exists, name)
    if exists:
        await event.message.answer(f"⚠️ {name} уже есть в списке. Добавление отменено.")
        return
    await event.message.answer(f"Добавляю {name}… (это займёт несколько секунд)")
    ok = await asyncio.to_thread(sheets.add_employee, name)
    if ok:
        await event.message.answer(f"✅ {name} добавлен в табель.")
    else:
        await event.message.answer("Не удалось добавить (возможно, уже существует).")


@dp.message_callback(F.callback.payload == "menu:clearall")
async def cb_menu_clearall(event: MessageCallback):
    if not await _is_foreman(event):
        return
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Да, удалить всё", payload="clearall_yes"),
        CallbackButton(text="✖ Отмена", payload="clearall_no"),
    )
    await event.message.answer(
        "⚠️ ТЕСТ: удалить отметки (день И ночь) у ВСЕХ за сегодня?",
        attachments=[kb.as_markup()])


@dp.message_callback(F.callback.payload == "clearall_yes")
async def cb_clearall_yes(event: MessageCallback):
    n = await asyncio.to_thread(sheets.clear_all_day)
    await _finish(event, f"🧹 Очищено за сегодня: {n} сотрудников (день+ночь).")


@dp.message_callback(F.callback.payload == "clearall_no")
async def cb_clearall_no(event: MessageCallback):
    await _finish(event, "Отменено.")


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
    _sess(event)["fire"]["page"] = page
    await _send_fire_list(event.message, page)


@dp.message_callback(F.callback.payload.startswith("fire:"))
async def cb_fire_pick(event: MessageCallback):
    idx = int(event.callback.payload.split(":")[1])
    employees = await asyncio.to_thread(sheets.get_employees)
    if idx >= len(employees):
        await event.message.answer("Сотрудник не найден.")
        return
    fs = _sess(event)["fire"]
    fs["name"] = employees[idx]
    fs["awaiting_day"] = True
    await event.message.answer(
        f"Увольняем: {employees[idx]}\nВведите число месяца — дату увольнения:")


@dp.message_callback(F.callback.payload == "fireconfirm")
async def cb_fire_confirm(event: MessageCallback):
    fs = _sess(event)["fire"]
    name = fs["name"]
    day = fs["day"]
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
    _sess(event)["fire"].update({"name": None, "day": None, "awaiting_day": False})
    await event.message.answer("Увольнение отменено.")


# ================= ВВОД ЧИСЕЛ / ДАТ =================


@dp.message_created(F.message.body.text.regexp(r"^\d{1,2}\.\d{1,2}$"))
async def on_date_ddmm(event: MessageCreated):
    """Дата возврата с межвахты (ДД.ММ)."""
    rot = _sess(event)["rotation"]
    if not rot.get("active"):
        return
    name = rot["name"]
    rot["active"] = False
    rot["name"] = None
    await asyncio.to_thread(sheets.set_rotation_return, name, event.message.body.text)
    await event.message.answer(
        f"✔ {name}: межвахта до {event.message.body.text}. "
        f"Напомню за 3 дня до возврата.")
    await _send_reason_list(event.message)


@dp.message_created(F.message.body.text.regexp(r"^\d{1,2}$"))
async def on_day_number(event: MessageCreated):
    """Число — дата увольнения."""
    day = int(event.message.body.text)
    fs = _sess(event)["fire"]
    if fs.get("awaiting_day"):
        fs["awaiting_day"] = False
        fs["day"] = day
        name = fs["name"]
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

    if os.getenv("RUN_USERS") == "1":
        try:
            aid = await asyncio.to_thread(setup_users)
            log.info("Лист «Пользователи» создан. Админ: %s", aid)
        except Exception as e:
            log.exception("Ошибка создания пользователей: %s", e)

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(rotation_reminders_job, CronTrigger(hour=9, minute=0))
    scheduler.start()
    log.info("Планировщик запущен (TZ=%s). Бот стартует...", TIMEZONE)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
