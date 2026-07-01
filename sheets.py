"""
Работа с Google Sheets для табеля учёта рабочего времени.

Логика:
  - 08:00 — заполнить все ячейки текущего дня кодом "Я" (явка)
  - в течение дня — бригадир меняет ячейки через выпадающий список
  - 20:00 — если за день НИ ОДНА ячейка не изменилась (у всех осталось "Я"),
            отправить вопрос; при отсутствии подтверждения — обнулить день.
"""

import calendar
import json
import os
import time
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

# --- Настройки ---
SPREADSHEET_ID = "1d7YqIAqWL9_cQQ7JpxqD_qV69q1NpVO3u58BzDlK73M"

# JSON-ключ service account.
# На Railway кладётся в переменную окружения GOOGLE_CREDENTIALS (весь JSON).
# Локально можно положить файл service_account.json рядом с кодом.
CREDENTIALS_FILE = "service_account.json"

# Коды статусов
CODE_PRESENT = "Я"   # явка
CODE_ABSENT = "Н"    # неявка
CODE_SICK = "Б"      # больничный
CODE_VACATION = "О"  # отпуск
CODE_WEEKEND = "В"   # выходной
ALL_CODES = [CODE_PRESENT, CODE_ABSENT, CODE_SICK, CODE_VACATION, CODE_WEEKEND]

# Русские названия месяцев = названия листов
MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

# Структура листа (после реорганизации):
#   строка 1 — заголовок месяца
#   строка 2 — шапка: A2="№", B2="ФИО", C2..="1","2",...
#   строки 3+ — сотрудники: A=номер, B=ФИО, C.. — дни
FIRST_DATA_ROW = 3
NUM_COL = 1           # столбец A = №
NAME_COL = 2          # столбец B = ФИО
FIRST_DAY_COL = 3     # столбец C = день 1

# Лист-справочник сотрудников
EMP_SHEET = "Сотрудники"
EMP_STATUS_ACTIVE = "активен"
EMP_STATUS_FIRED = "уволен"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _credentials():
    """
    Загружает Credentials.
    Приоритет: переменная окружения GOOGLE_CREDENTIALS (для Railway),
    иначе — локальный файл service_account.json.
    """
    raw = os.getenv("GOOGLE_CREDENTIALS")
    if raw:
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    return Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)


_cached_client = None
_cached_spreadsheet = None


def _client():
    global _cached_client
    if _cached_client is None:
        _cached_client = gspread.authorize(_credentials())
    return _cached_client


def _open():
    global _cached_spreadsheet
    if _cached_spreadsheet is None:
        _cached_spreadsheet = _client().open_by_key(SPREADSHEET_ID)
    return _cached_spreadsheet


def _worksheet_for(date: datetime):
    """Лист, соответствующий месяцу даты."""
    return _open().worksheet(MONTHS_RU[date.month - 1])


def _day_column(date: datetime) -> int:
    """Номер столбца для конкретного дня месяца (1-индексация gspread)."""
    return FIRST_DAY_COL + (date.day - 1)


def _employee_count(ws) -> int:
    """Сколько строк-сотрудников на листе."""
    names = ws.col_values(NAME_COL)  # включая шапку
    # names[0]=заголовок месяца (стр.1), names[1]=шапка (стр.2), дальше ФИО
    return max(0, len(names) - (FIRST_DATA_ROW - 1))


def fill_present(date: datetime | None = None):
    """
    08:00 — ставит "Я" во все ячейки дня.
    Выходные (сб/вс) помечает "В".
    """
    date = date or datetime.now()
    ws = _worksheet_for(date)
    col = _day_column(date)
    n = _employee_count(ws)
    if n == 0:
        return 0

    is_weekend = calendar.weekday(date.year, date.month, date.day) >= 5
    value = CODE_WEEKEND if is_weekend else CODE_PRESENT

    # Диапазон ячеек дня: от FIRST_DATA_ROW до FIRST_DATA_ROW+n-1
    start = gspread.utils.rowcol_to_a1(FIRST_DATA_ROW, col)
    end = gspread.utils.rowcol_to_a1(FIRST_DATA_ROW + n - 1, col)
    cell_range = f"{start}:{end}"

    cells = ws.range(cell_range)
    for c in cells:
        c.value = value
    ws.update_cells(cells)
    return n


def read_day(date: datetime | None = None) -> list[str]:
    """Возвращает список значений ячеек дня (по сотрудникам)."""
    date = date or datetime.now()
    ws = _worksheet_for(date)
    col = _day_column(date)
    n = _employee_count(ws)
    if n == 0:
        return []
    start = gspread.utils.rowcol_to_a1(FIRST_DATA_ROW, col)
    end = gspread.utils.rowcol_to_a1(FIRST_DATA_ROW + n - 1, col)
    cells = ws.range(f"{start}:{end}")
    return [c.value for c in cells]


def is_untouched(date: datetime | None = None) -> bool:
    """
    True, если за день НИ ОДНА ячейка не менялась —
    то есть у всех стоит "Я" (для будней) или "В" (для выходных).
    """
    date = date or datetime.now()
    is_weekend = calendar.weekday(date.year, date.month, date.day) >= 5
    expected = CODE_WEEKEND if is_weekend else CODE_PRESENT
    values = read_day(date)
    if not values:
        return False
    return all(v == expected for v in values)


def clear_day(date: datetime | None = None):
    """
    20:00 — если день не подтверждён: обнулить присутствие.
    Ставит "Н" (неявка) во все ячейки дня.
    """
    date = date or datetime.now()
    ws = _worksheet_for(date)
    col = _day_column(date)
    n = _employee_count(ws)
    if n == 0:
        return 0
    start = gspread.utils.rowcol_to_a1(FIRST_DATA_ROW, col)
    end = gspread.utils.rowcol_to_a1(FIRST_DATA_ROW + n - 1, col)
    cells = ws.range(f"{start}:{end}")
    for c in cells:
        c.value = CODE_ABSENT
    ws.update_cells(cells)
    return n


def _all_month_names(date: datetime | None = None) -> list[str]:
    """Все ФИО из листа месяца (по порядку строк)."""
    date = date or datetime.now()
    ws = _worksheet_for(date)
    names = ws.col_values(NAME_COL)
    return names[FIRST_DATA_ROW - 1:]


_status_cache = {"data": None, "ts": 0}
_STATUS_TTL = 30  # секунд


def get_status_list(force: bool = False) -> list[dict]:
    """
    Читает лист «Сотрудники» (с кэшем на 30 сек).
    Возвращает список {"name", "status", "fired_date"} по порядку.
    Если листа нет — пустой список.
    """
    now = time.time()
    if not force and _status_cache["data"] is not None \
            and now - _status_cache["ts"] < _STATUS_TTL:
        return _status_cache["data"]
    try:
        ws = _open().worksheet(EMP_SHEET)
    except Exception:
        return []
    rows = ws.get_all_values()[1:]  # без шапки
    result = []
    for r in rows:
        if len(r) >= 2 and r[1].strip():
            result.append({
                "name": r[1].strip(),
                "status": (r[2].strip() if len(r) > 2 else EMP_STATUS_ACTIVE),
                "fired_date": (r[3].strip() if len(r) > 3 else ""),
            })
    _status_cache["data"] = result
    _status_cache["ts"] = now
    return result


def get_employees(date: datetime | None = None) -> list[str]:
    """
    Список ФИО активных сотрудников.
    Если есть лист «Сотрудники» — берём только активных оттуда.
    Иначе — все из листа месяца (обратная совместимость).
    """
    status = get_status_list()
    if status:
        return [e["name"] for e in status if e["status"] == EMP_STATUS_ACTIVE]
    return _all_month_names(date)


def get_fired() -> list[dict]:
    """Список уволенных: [{"name", "fired_date"}, ...]."""
    return [
        {"name": e["name"], "fired_date": e["fired_date"]}
        for e in get_status_list()
        if e["status"] == EMP_STATUS_FIRED
    ]


def _row_by_name(ws, name: str) -> int | None:
    """Находит номер строки сотрудника в листе месяца по ФИО."""
    names = ws.col_values(NAME_COL)
    for i, n in enumerate(names):
        if i >= FIRST_DATA_ROW - 1 and n.strip() == name.strip():
            return i + 1  # gspread 1-based
    return None


def get_current_status(emp_index: int, date: datetime | None = None) -> str:
    """
    Возвращает текущее значение ячейки сотрудника за день.
    Нужно для предупреждения о перезаписи.
    """
    date = date or datetime.now()
    active = get_employees(date)
    if emp_index >= len(active):
        return ""
    name = active[emp_index]
    ws = _worksheet_for(date)
    row = _row_by_name(ws, name)
    if row is None:
        return ""
    col = _day_column(date)
    val = ws.cell(row, col).value
    return (val or "").strip()


def set_status(emp_index: int, code: str, date: datetime | None = None):
    """
    Ставит статус сотруднику за конкретный день.
    emp_index — индекс в списке get_employees() (активные).
    Запись идёт по ФИО (поиск строки в листе месяца), чтобы уволенные
    не сдвигали адресацию.
    Возвращает (ФИО, код).
    """
    date = date or datetime.now()
    active = get_employees(date)
    if emp_index >= len(active):
        return None, code
    name = active[emp_index]

    ws = _worksheet_for(date)
    row = _row_by_name(ws, name)
    if row is None:
        return name, code
    col = _day_column(date)
    ws.update_cell(row, col, code)
    return name, code


def day_summary(date: datetime | None = None) -> dict:
    """
    Сводка за день по активным сотрудникам.
    Одним запросом читает весь лист месяца, считает в памяти.
    """
    date = date or datetime.now()
    active = set(get_employees(date))  # активные ФИО
    ws = _worksheet_for(date)
    grid = ws.get_all_values()  # ОДИН запрос на весь лист

    day_col_idx = _day_column(date) - 1  # 0-based
    counts = {c: 0 for c in ALL_CODES}
    absent = []

    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) < NAME_COL:
            continue
        name = r[NAME_COL - 1].strip()
        if not name or name not in active:
            continue
        val = r[day_col_idx].strip() if len(r) > day_col_idx else ""
        if val in counts:
            counts[val] += 1
        if val in (CODE_ABSENT, CODE_SICK, CODE_VACATION):
            absent.append((name, val))
    return {"counts": counts, "absent": absent, "total": len(active)}


def fire_employee(name: str, fire_day: int, date: datetime | None = None) -> bool:
    """
    Помечает сотрудника уволенным в листе «Сотрудники»:
    статус → 'уволен', дата увольнения → ДД.ММ.ГГГГ.
    Строки в листах месяцев не трогает (история сохраняется).
    """
    date = date or datetime.now()
    try:
        ws = _open().worksheet(EMP_SHEET)
    except Exception:
        return False
    grid = ws.get_all_values()
    fire_date_str = f"{fire_day:02d}.{date.month:02d}.{date.year}"
    for i, r in enumerate(grid):
        if i == 0:
            continue  # шапка
        if len(r) >= 2 and r[1].strip() == name.strip():
            row = i + 1
            # C = статус, D = дата увольнения
            ws.update_cell(row, 3, EMP_STATUS_FIRED)
            ws.update_cell(row, 4, fire_date_str)
            _status_cache["data"] = None  # сброс кэша
            return True
    return False


def build_work_report(name: str, out_path: str, year: int = 2026) -> str | None:
    """
    Формирует Excel-график работы сотрудника за месяцы, где он работал
    (есть непустые ячейки). Возвращает путь к файлу или None.
    """
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side

    sp = _open()
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    thin = Border(*[Side(style="thin")] * 4)
    center = Alignment(horizontal="center", vertical="center")
    bold = Font(bold=True)

    any_data = False
    for month_idx, month_name in enumerate(MONTHS_RU, 1):
        try:
            ws_src = sp.worksheet(month_name)
        except Exception:
            continue
        grid = ws_src.get_all_values()
        # ищем строку сотрудника
        emp_row = None
        for r in grid[FIRST_DATA_ROW - 1:]:
            if len(r) >= NAME_COL and r[NAME_COL - 1].strip() == name.strip():
                emp_row = r
                break
        if not emp_row:
            continue
        # значения дней (с FIRST_DAY_COL)
        day_vals = emp_row[FIRST_DAY_COL - 1:]
        if not any(v.strip() for v in day_vals):
            continue  # месяц пустой — пропускаем

        any_data = True
        ws_out = wb.create_sheet(month_name)
        ws_out["A1"] = f"{name} — {month_name} {year}"
        ws_out["A1"].font = Font(bold=True, size=12)
        ws_out["A2"] = "День"
        ws_out["B2"] = "Статус"
        ws_out["A2"].font = bold
        ws_out["B2"].font = bold
        ws_out["A2"].border = thin
        ws_out["B2"].border = thin
        for d, v in enumerate(day_vals, 1):
            ws_out.cell(row=d + 2, column=1, value=d).border = thin
            c = ws_out.cell(row=d + 2, column=2, value=v.strip())
            c.border = thin
            c.alignment = center
        ws_out.column_dimensions["A"].width = 6
        ws_out.column_dimensions["B"].width = 10

    if not any_data:
        return None
    wb.save(out_path)
    return out_path
