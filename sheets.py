"""
Работа с Google Sheets для табеля учёта рабочего времени.

Логика:
  - 08:00 — заполнить все ячейки текущего дня кодом "Я" (явка)
  - в течение дня — бригадир меняет ячейки через выпадающий список
  - 20:00 — если за день НИ ОДНА ячейка не изменилась (у всех осталось "Я"),
            отправить вопрос; при отсутствии подтверждения — обнулить день.
"""

import calendar
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

# --- Настройки ---
SPREADSHEET_ID = "1d7YqIAqWL9_cQQ7JpxqD_qV69q1NpVO3u58BzDlK73M"
CREDENTIALS_FILE = "service_account.json"  # JSON-ключ service account

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

# Структура листа:
#   строка 1 — заголовок месяца
#   строка 2 — шапка: A2="Сотрудник", B2..="1","2",...
#   строки 3+ — сотрудники, столбец A — ФИО
FIRST_DATA_ROW = 3
NAME_COL = 1          # столбец A
FIRST_DAY_COL = 2     # столбец B = день 1

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _client():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def _open():
    return _client().open_by_key(SPREADSHEET_ID)


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


def get_employees(date: datetime | None = None) -> list[str]:
    """Список ФИО с листа текущего месяца."""
    date = date or datetime.now()
    ws = _worksheet_for(date)
    names = ws.col_values(NAME_COL)
    return names[FIRST_DATA_ROW - 1:]  # с третьей строки
