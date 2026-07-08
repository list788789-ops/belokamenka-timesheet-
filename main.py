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

from maxapi import Bot, Dispatcher, F
from maxapi.types import MessageCreated, BotStarted, Command, MessageCallback, CallbackButton, InputMedia
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
    и возвращаем False. Если Sheets временно перегружен (квота) — говорим
    об этом прямо, а не делаем вид, что доступа нет.
    """
    cid = _chat_id(event)
    try:
        allowed = await asyncio.to_thread(sheets.is_allowed, cid)
    except sheets.SheetsBusyError:
        try:
            await _send(event.message, 
                "⏳ Google Sheets временно перегружен (превышена квота запросов). "
                "Попробуйте через минуту.")
        except Exception:
            pass
        return False
    if allowed:
        return True
    # Незнакомый — отправляем заявку админам
    await _send_access_request(event, cid)
    return False


async def _send_access_request(event, cid: int):
    """Сообщает пользователю о заявке и шлёт админам кнопку добавления."""
    try:
        await _send(event.message, 
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

def _main_menu(is_admin: bool = False):
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="☀️ Утро (присутствующие)", payload="menu:morning"))
    kb.row(CallbackButton(text="🌙 Вечер (ночная смена)", payload="menu:evening"))
    kb.row(CallbackButton(text="📁 Отчёты", payload="menu:reports"))
    kb.row(CallbackButton(text="🚪 Оформить увольнение", payload="menu:fire"))
    kb.row(CallbackButton(text="👤 Приём", payload="menu:intake"))
    if is_admin:
        kb.row(CallbackButton(text="🧹 Очистить весь день (тест)", payload="menu:clearall"))
        kb.row(CallbackButton(text="🗑 Удалить сообщения бота", payload="menu:clearmsgs"))
    return kb.as_markup()


async def _is_admin(event) -> bool:
    try:
        role = await asyncio.to_thread(sheets.get_role, _chat_id(event))
    except sheets.SheetsBusyError:
        return False  # при перегрузке Sheets просто не показываем админ-кнопки
    return role == sheets.ROLE_ADMIN


def _reports_menu():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📅 Табель за сегодня", payload="menu:today"))
    kb.row(CallbackButton(text="📊 Свод (Excel)", payload="menu:summary"))
    kb.row(CallbackButton(text="⚠️ Проблемные", payload="menu:problems"))
    kb.row(CallbackButton(text="📋 Список уволенных", payload="menu:fired"))
    kb.row(CallbackButton(text="◀ Назад", payload="menu:back"))
    return kb.as_markup()


def _intake_menu():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="➕ Добавить сотрудника", payload="menu:addemp"))
    kb.row(CallbackButton(text="📥 Загрузить Excel", payload="menu:upload"))
    kb.row(CallbackButton(text="◀ Назад", payload="menu:back"))
    return kb.as_markup()


@dp.message_callback(F.callback.payload == "menu:reports")
async def cb_menu_reports(event: MessageCallback):
    await _edit_or_send(event, "📁 Отчёты:", _reports_menu())


@dp.message_callback(F.callback.payload == "menu:intake")
async def cb_menu_intake(event: MessageCallback):
    await _edit_or_send(event, "👤 Приём:", _intake_menu())


@dp.message_callback(F.callback.payload == "menu:back")
async def cb_menu_back(event: MessageCallback):
    await _edit_or_send(event, "Выберите действие:", _main_menu(await _is_admin(event)))


@dp.bot_started()
async def on_bot_started(event: BotStarted):
    is_admin = (await asyncio.to_thread(sheets.get_role, event.chat_id)) == sheets.ROLE_ADMIN
    await bot.send_message(chat_id=event.chat_id,
                           text="Бот табеля. Выберите действие:",
                           attachments=[_main_menu(is_admin)])


@dp.message_created(Command("menu"))
async def show_menu(event: MessageCreated):
    await _send(event.message, "Выберите действие:", attachments=[_main_menu(await _is_admin(event))])


@dp.message_created(Command("chatid"))
async def show_chat_id(event: MessageCreated):
    await _send(event.message, f"chat_id этого чата: {event.message.recipient.chat_id}")


@dp.message_callback(F.callback.payload.startswith("adduser:"))
async def cb_add_user(event: MessageCallback):
    """Админ добавляет нового прораба по заявке."""
    admin_id = _chat_id(event)
    role = await asyncio.to_thread(sheets.get_role, admin_id)
    if role != sheets.ROLE_ADMIN:
        await _send(event.message, "Только админ может добавлять пользователей.")
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
        f"📋 Мигр.учёт: {s['migr']}",
    ]
    if s["absent_list"]:
        order = {sheets.DN_SICK: 0, sheets.DN_ROTATION: 1,
                  sheets.DN_ABSENT: 2, sheets.DN_MIGR: 3}
        labels = {
            sheets.DN_SICK: "🤒 Больничный",
            sheets.DN_ROTATION: "✈️ Межвахта",
            sheets.DN_ABSENT: "❌ Неявка",
            sheets.DN_MIGR: "📋 Мигр.учёт",
        }
        grouped = sorted(s["absent_list"],
                          key=lambda t: (order.get(t[1], 99), t[0].strip().lower()))
        lines.append("\nОтсутствуют/особое:")
        current_code = None
        for name, code in grouped:
            if code != current_code:
                current_code = code
                lines.append(f"  {labels.get(code, code)}:")
            lines.append(f"    • {name}")
    await _send(event.message, "\n".join(lines))


@dp.message_callback(F.callback.payload == "menu:problems")
async def cb_problems(event: MessageCallback):
    problems = await asyncio.to_thread(sheets.check_problems)
    if not problems:
        await _send(event.message, "Проблемных нет.")
        return
    lines = ["⚠️ Проблемные за месяц:"]
    for p in problems:
        lines.append(f"  • {p['name']}: {', '.join(p['reasons'])}")
    await _send(event.message, "\n".join(lines))


@dp.message_callback(F.callback.payload == "menu:summary")
async def cb_summary(event: MessageCallback):
    await _send(event.message, "Формирую свод за месяц…")
    month = _MONTHS_GEN[datetime.now().month - 1]
    out_path = f"/tmp/Svod_{month}.xlsx"
    path = await asyncio.to_thread(sheets.build_month_summary, out_path)
    if not path:
        await _send(event.message, "Нет данных для свода.")
        return
    try:
        await bot.send_message(
            chat_id=event.message.recipient.chat_id,
            attachments=[InputMedia(path=path)])
    except Exception as e:
        log.warning("send summary failed: %s", e)
        await _send(event.message, 
            "Свод сформирован, но отправка файла не удалась.")


_sessions = {}
# Сообщения бота по chat_id (не по человеку) — очистка чата это операция
# на уровне всего чата, не привязана к тому, кто именно нажал кнопку.
_chat_sent_msgs = {}


def _new_session():
    return {
        "morning": {"page": 0, "reason_mode": False},
        "evening": {"page": 0},
        "clear_m": {"page": 0},
        "clear_e": {"page": 0},
        "fire": {"page": 0, "day": None, "name": None, "awaiting_day": False},
        "rotation": {"name": None, "active": False},
        "actual_return": {"name": None, "active": False, "page": 0,
                           "action": "day", "flow": "morning"},
        "addemp": {"awaiting": False},
        "upload": {"awaiting": False},
    }


def _chat_id(event):
    """Достаёт chat_id из события (callback или message)."""
    try:
        msg = getattr(event, "message", event)
        return msg.recipient.chat_id
    except Exception:
        return 0


def _user_id(event):
    """
    Извлекает user_id отправителя/нажавшего кнопку — нужен, чтобы делить
    сессии между людьми внутри ОДНОГО группового чата (там у всех общий
    chat_id, но разные user_id). Порядок попыток покрывает и обычное
    сообщение, и нажатие inline-кнопки (там отправитель — в другом поле).
    Не проверено по официальной документации maxapi — если в группе с
    несколькими людьми сессии всё равно будут путаться, значит ни один
    из этих путей не подошёл и нужно смотреть реальную структуру event.
    """
    for path in (
        lambda e: e.callback.user.user_id,
        lambda e: e.callback.user_id,
        lambda e: e.message.sender.user_id,
        lambda e: e.sender.user_id,
    ):
        try:
            uid = path(event)
            if uid is not None:
                return uid
        except Exception:
            continue
    return None


def _sess_key(event):
    """
    Ключ сессии: (chat_id, user_id), если user_id удалось достать —
    так один групповой чат не смешивает состояние разных людей.
    Если user_id недоступен — просто chat_id (личный диалог с ботом,
    там chat_id и так уникален на человека).
    """
    cid = _chat_id(event)
    uid = _user_id(event)
    return (cid, uid) if uid is not None else cid


def _sess(event):
    """Возвращает состояние сессии текущего пользователя (в группе — per-user)."""
    key = _sess_key(event)
    if key not in _sessions:
        _sessions[key] = _new_session()
    return _sessions[key]


async def _send(target, text, attachments=None):
    """
    Обёртка над .answer(): отправляет сообщение и запоминает его в сессии
    чата — нужно кнопке «🗑 Удалить сообщения бота» (админ), чтобы потом
    удалить именно то, что бот сам отправил. Чужие сообщения (от людей)
    бот удалять не может — ограничение платформы, не этого кода.
    Покрывает только сообщения, отправленные ПОСЛЕ включения этой правки —
    старую историю бот не помнит.
    """
    if attachments is not None:
        msg = await target.answer(text, attachments=attachments)
    else:
        msg = await target.answer(text)
    try:
        cid = _chat_id(target)
        _chat_sent_msgs.setdefault(cid, []).append(msg)
    except Exception:
        log.warning("Не удалось запомнить отправленное сообщение для очистки")
    return msg


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
        await _send(msg, text, attachments=attachments)
    else:
        await _send(msg, text)


async def _show_problems(event_or_target):
    """Показывает проблемных за месяц (неявки/выходные) текстом, если есть."""
    problems = await asyncio.to_thread(sheets.check_problems)
    if not problems:
        return
    msg = getattr(event_or_target, "message", event_or_target)
    lines = ["⚠️ Проблемные за месяц:"]
    for p in problems:
        lines.append(f"  • {p['name']}: {', '.join(p['reasons'])}")
    await _send(msg, "\n".join(lines))


def _hint(text: str) -> str:
    """Добавляет подсказку про добавление явки после завершения утра —
    механизм существует ('Очистить сотрудника' → отметить заново),
    просто неочевиден без прямого указания."""
    return (text + "\n\nℹ️ Если нужно добавить кого-то в явку после завершения — "
            "«🧹 Очистить сотрудника», выбрать человека, затем «◀ К отметке» "
            "и отметить его заново.")


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
    await _send(msg, text)


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
        await _send(target, txt, attachments=[kb.as_markup()])


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
        await _send(event.message, 
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
        await _send(target, "С ночи отдыхают (проставлен отдых):\n" +
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
    rot_warn = await asyncio.to_thread(sheets.check_rotation_return_conflict, name)
    if rot_warn:
        ar = _sess(event)["actual_return"]
        ar["name"] = name
        ar["active"] = True
        ar["action"] = "day"
        ar["flow"] = "morning"
        ar["page"] = _sess(event)["morning"]["page"]
        await _edit_or_send(
            event, f"⚠️ {rot_warn}\nУкажите дату фактического возврата (ДД.ММ):")
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
        await _finish(event, _hint("Все отмечены. Утро завершено."))
        await _show_problems(event)
        return
    _sess(event)["morning"]["reason_mode"] = True
    await _send_reason_list(event.message, edit_event=event)


async def _send_reason_list(target, edit_event=None, page: int = 0):
    """Оставшиеся без отметки → выбор причины (с пагинацией и «Завершить»)."""
    remaining = await asyncio.to_thread(sheets.get_unmarked_day)
    if not remaining:
        txt = _hint("Причины проставлены всем. Утро завершено.")
        if edit_event is not None:
            await _finish(edit_event, txt)
            await _show_problems(edit_event)
        else:
            await _send(target, txt)
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
        await _send(target, txt, attachments=[kb.as_markup()])


@dp.message_callback(F.callback.payload.startswith("rsnpage:"))
async def cb_reason_page(event: MessageCallback):
    page = int(event.callback.payload.split(":")[1])
    await _send_reason_list(event.message, edit_event=event, page=page)


@dp.message_callback(F.callback.payload == "rsnfinish")
async def cb_reason_finish(event: MessageCallback):
    remaining = await asyncio.to_thread(sheets.get_unmarked_day)
    if not remaining:
        await _edit_or_send(event, _hint("Все размечены. Утро завершено."))
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
    await _finish(event, _hint(f"Утро завершено. Неявка проставлена: {n} чел."))
    await _show_problems(event)


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
    kb.row(CallbackButton(text="🏖 Выходной", payload=f"setrsn:{name}:{sheets.DN_WEEKEND}"))
    await _edit_or_send(event, f"{name} — причина?", kb.as_markup())


@dp.message_callback(F.callback.payload.startswith("setrsn:"))
async def cb_set_reason(event: MessageCallback):
    _, name, code = event.callback.payload.split(":", 2)
    if code == sheets.DN_ROTATION:
        # межвахта → спрашиваем дату возврата
        rot = _sess(event)["rotation"]
        rot["name"] = name
        rot["active"] = True
        rot["prompt_event"] = event  # запомнили, чтобы удалить после ввода даты
        await asyncio.to_thread(sheets.set_reason, name, code)
        await _edit_or_send(
            event,
            f"{name}: межвахта. До какого числа? Введите дату возврата (ДД.ММ):")
        return
    if code == sheets.DN_MIGR:
        migr_warn = await asyncio.to_thread(sheets.check_migr_after_rotation, name)
        if migr_warn:
            kb = InlineKeyboardBuilder()
            kb.row(
                CallbackButton(text="✅ Всё равно МУ", payload=f"fmigr:{name}"),
                CallbackButton(text="✖ Отмена", payload=f"rsn:{name}"),
            )
            await _edit_or_send(event, f"⚠️ {migr_warn}\nВсё равно поставить МУ?", kb.as_markup())
            return
    await asyncio.to_thread(sheets.set_reason, name, code)
    if code == sheets.DN_MIGR:
        await _warn_if_too_many_migr(event.message)
    await _send_reason_list(event.message, edit_event=event)


async def _warn_if_too_many_migr(target):
    """Если МУ за сегодня стало больше порога — отдельное предупреждение."""
    n = await asyncio.to_thread(sheets.count_migr_today)
    if n > sheets.MIGR_DAILY_THRESHOLD:
        await _send(target,
            f"⚠️ Сегодня на мигр.учёте уже {n} человек (порог {sheets.MIGR_DAILY_THRESHOLD}). "
            f"Риск вопросов от заказчика — проверьте обоснованность.")


@dp.message_callback(F.callback.payload.startswith("fmigr:"))
async def cb_force_migr(event: MessageCallback):
    name = event.callback.payload.split(":", 1)[1]
    await asyncio.to_thread(sheets.set_reason, name, sheets.DN_MIGR)
    await _warn_if_too_many_migr(event.message)
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
        await _send(target, txt, attachments=[kb.as_markup()])


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
    rot_warn = await asyncio.to_thread(sheets.check_rotation_return_conflict, name)
    if rot_warn:
        ar = _sess(event)["actual_return"]
        ar["name"] = name
        ar["active"] = True
        ar["action"] = "night"
        ar["flow"] = "evening"
        ar["page"] = _sess(event)["evening"]["page"]
        await _edit_or_send(
            event, f"⚠️ {rot_warn}\nУкажите дату фактического возврата (ДД.ММ):")
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
    await _show_problems(event)


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
        await _send(event.message, "Уволенных нет.")
        return
    lines = ["Уволенные сотрудники:"]
    for f in fired:
        lines.append(f"  ⚫ {f['name']} — уволен {f['fired_date'] or '—'}")
    await _send(event.message, "\n".join(lines))


@dp.message_callback(F.callback.payload == "menu:addemp")
async def cb_menu_addemp(event: MessageCallback):
    if not await _is_foreman(event):
        return
    _sess(event)["addemp"]["awaiting"] = True
    await _send(event.message, 
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
        await _send(event.message, f"⚠️ {name} уже есть в списке. Добавление отменено.")
        return
    await _send(event.message, f"Добавляю {name}… (это займёт несколько секунд)")
    ok = await asyncio.to_thread(sheets.add_employee, name)
    if ok:
        await _send(event.message, f"✅ {name} добавлен в табель.")
    else:
        await _send(event.message, "Не удалось добавить (возможно, уже существует).")


@dp.message_callback(F.callback.payload == "menu:upload")
async def cb_menu_upload(event: MessageCallback):
    if not await _is_foreman(event):
        return
    _sess(event)["upload"]["awaiting"] = True
    await _send(event.message, 
        "Пришлите файл Excel (.xlsx) со списком ФИО — по одному в строке.")


@dp.message_callback(F.callback.payload == "menu:clearall")
async def cb_menu_clearall(event: MessageCallback):
    if not await _is_admin(event):
        return
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Да, удалить всё", payload="clearall_yes"),
        CallbackButton(text="✖ Отмена", payload="clearall_no"),
    )
    await _send(event.message, 
        "⚠️ ТЕСТ: удалить отметки (день И ночь) у ВСЕХ за сегодня?",
        attachments=[kb.as_markup()])


@dp.message_callback(F.callback.payload == "clearall_yes")
async def cb_clearall_yes(event: MessageCallback):
    n = await asyncio.to_thread(sheets.clear_all_day)
    await _finish(event, f"🧹 Очищено за сегодня: {n} сотрудников (день+ночь).")


@dp.message_callback(F.callback.payload == "clearall_no")
async def cb_clearall_no(event: MessageCallback):
    await _finish(event, "Отменено.")


@dp.message_callback(F.callback.payload == "menu:clearmsgs")
async def cb_menu_clearmsgs(event: MessageCallback):
    """Удаляет все сообщения, отправленные ботом в этом чате с момента
    включения этой функции, затем показывает меню заново. Сообщения от
    людей бот удалить не может — так устроена платформа, не ограничение
    этого кода."""
    if not await _is_admin(event):
        return
    cid = _chat_id(event)
    msgs = _chat_sent_msgs.get(cid, [])
    deleted = 0
    for m in msgs:
        try:
            await m.delete()
            deleted += 1
        except Exception:
            pass
    _chat_sent_msgs[cid] = []
    try:
        await event.delete()
    except Exception:
        pass
    is_admin = await _is_admin(event)
    await _send(event.message, f"🗑 Удалено сообщений бота: {deleted}.\nВыберите действие:",
                attachments=[_main_menu(is_admin)])


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
    await _send(target, "Кого увольняем?", attachments=[kb.as_markup()])


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
        await _send(event.message, "Сотрудник не найден.")
        return
    fs = _sess(event)["fire"]
    fs["name"] = employees[idx]
    fs["awaiting_day"] = True
    await _send(event.message, 
        f"Увольняем: {employees[idx]}\n"
        f"Введите дату увольнения в формате ДД.ММ (например, 20.06):")


@dp.message_callback(F.callback.payload == "fireconfirm")
async def cb_fire_confirm(event: MessageCallback):
    fs = _sess(event)["fire"]
    name = fs["name"]
    fire_date = fs["day"]  # теперь строка ДД.ММ или ДД.ММ.ГГГГ
    if not name or not fire_date:
        await _send(event.message, "Данные увольнения потеряны, начните заново.")
        return
    ok = await asyncio.to_thread(sheets.fire_employee, name, fire_date)
    if not ok:
        await _send(event.message, "Не удалось обновить статус.")
        return
    await _send(event.message, f"⚫ {name} уволен с {fire_date}.")

    safe = "".join(ch for ch in name if ch.isalnum() or ch in " _-").strip().replace(" ", "_")
    out_path = f"/tmp/Otchet_{safe}.xlsx"
    path = await asyncio.to_thread(sheets.build_work_report, name, out_path)
    if path:
        try:
            await bot.send_message(
                chat_id=event.message.recipient.chat_id,
                attachments=[InputMedia(path=path)])
        except Exception as e:
            log.warning("send fire report failed: %s", e)
            await _send(event.message, "График сформирован, но отправка не удалась.")
    else:
        await _send(event.message, "График пуст — у сотрудника нет отметок.")


@dp.message_callback(F.callback.payload == "firecancel")
async def cb_fire_cancel(event: MessageCallback):
    _sess(event)["fire"].update({"name": None, "day": None, "awaiting_day": False})
    await _send(event.message, "Увольнение отменено.")


# ================= ВВОД ЧИСЕЛ / ДАТ =================


@dp.message_created(F.message.body.text.regexp(r"^\d{1,2}\.\d{1,2}(\.\d{4})?$"))
async def on_date_ddmm(event: MessageCreated):
    """
    Ввод даты ДД.ММ — разводится по активной сессии:
      - увольнение (fire.awaiting_day) — приоритет
      - межвахта (rotation.active)
    """
    s = _sess(event)
    fs = s["fire"]
    rot = s["rotation"]
    ar = s["actual_return"]
    text = event.message.body.text.strip()

    # 1. Увольнение
    if fs.get("awaiting_day"):
        fs["awaiting_day"] = False
        fs["day"] = text  # строка ДД.ММ
        name = fs["name"]
        kb = InlineKeyboardBuilder()
        kb.row(
            CallbackButton(text="✅ Уволить", payload="fireconfirm"),
            CallbackButton(text="✖ Отмена", payload="firecancel"),
        )
        await _send(event.message, 
            f"Уволить {name} с {text}?\nОн исчезнет из списка отметок.",
            attachments=[kb.as_markup()])
        return

    # 2. Фактический возврат с межвахты (после строгого предупреждения)
    if ar.get("active"):
        name = ar["name"]
        action = ar.get("action", "day")
        flow = ar.get("flow", "morning")
        page = ar.get("page", 0)
        ar["active"] = False
        ar["name"] = None
        if action == "night":
            await asyncio.to_thread(sheets.mark_night, name)
            slot_label = "Ночь"
        else:
            await asyncio.to_thread(sheets.mark_day, name)
            slot_label = "День"
        try:
            await event.message.delete()
        except Exception:
            pass
        await _send(event.message,
            f"✔ {name}: фактический возврат с межвахты {text} зафиксирован. "
            f"{slot_label} проставлен(а).")
        if flow == "evening":
            await _send_evening_list(event.message, page)
        else:
            await _send_morning_list(event.message, page)
        return

    # 3. Межвахта (постановка, дата примерная/ожидаемая)
    if rot.get("active"):
        name = rot["name"]
        rot["active"] = False
        rot["name"] = None
        prompt = rot.pop("prompt_event", None)
        await asyncio.to_thread(sheets.set_rotation_return, name, text)
        if prompt is not None:
            for obj in (prompt, getattr(prompt, "message", None)):
                if obj is None:
                    continue
                try:
                    await obj.delete()
                    break
                except Exception:
                    continue
        try:
            await event.message.delete()
        except Exception:
            pass  # платформа не разрешает — молча не удаляем, ничего не ломаем
        await _send(event.message,
            f"✔ {name}: межвахта до {text}. Напомню за 3 дня до возврата.")
        await _send_reason_list(event.message)
        return


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

# Приём Excel со списком ФИО: скачивание, парсинг, массовое добавление.
@dp.message_created(F.message.body.attachments)
async def on_file_upload(event: MessageCreated):
    s = _sess(event)
    if not s["upload"].get("awaiting"):
        return
    s["upload"]["awaiting"] = False

    atts = event.message.body.attachments
    file_att = next((a for a in atts if getattr(a, "type", "") == "file"), None)
    if file_att is None:
        await _send(event.message, "Во вложении нет файла. Пришлите .xlsx.")
        return

    filename = getattr(file_att, "filename", "") or ""
    if not filename.lower().endswith((".xlsx", ".xls")):
        await _send(event.message, "Нужен файл Excel (.xlsx).")
        return

    await _send(event.message, "Загружаю файл, обрабатываю…")

    tmp_path = f"/tmp/upload_{_chat_id(event)}.xlsx"
    try:
        await bot.download_file(file_att, tmp_path)
    except Exception as e:
        log.warning("download_file failed: %s", e)
        # запасной путь — по url из payload
        try:
            url = file_att.payload.url
            import urllib.request
            await asyncio.to_thread(urllib.request.urlretrieve, url, tmp_path)
        except Exception as e2:
            log.warning("url download failed: %s", e2)
            await _send(event.message, "Не удалось скачать файл.")
            return

    result = await asyncio.to_thread(sheets.add_employees_from_xlsx, tmp_path)
    if result.get("error"):
        await _send(event.message, result["error"])
        return

    lines = []
    if result["added"]:
        lines.append(f"✅ Добавлено ({len(result['added'])}):")
        lines += [f"  • {n}" for n in result["added"]]
    if result["fuzzy"]:
        lines.append(f"🔎 Похоже на существующего, не добавлены ({len(result['fuzzy'])}):")
        for m in result["fuzzy"]:
            existing = ", ".join(m["existing"])
            lines.append(f"  • {m['new']} ↔ {existing}")
    if result["fired"]:
        lines.append(f"⚫ Уволены, пропущены ({len(result['fired'])}):")
        lines += [f"  • {n}" for n in result["fired"]]
    if result["skipped"]:
        lines.append(f"⚠️ Пропущены (уже есть) ({len(result['skipped'])}):")
        lines += [f"  • {n}" for n in result["skipped"]]
    if result["invalid"]:
        lines.append(f"❌ Не распознаны как ФИО ({len(result['invalid'])}):")
        lines += [f"  • {n}" for n in result["invalid"]]
    if not lines:
        lines = ["Файл пуст или не содержит ФИО."]
    await _send(event.message, "\n".join(lines))


async def main():
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан")

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(rotation_reminders_job, CronTrigger(hour=9, minute=0))
    scheduler.start()
    log.info("Планировщик запущен (TZ=%s). Бот стартует...", TIMEZONE)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
