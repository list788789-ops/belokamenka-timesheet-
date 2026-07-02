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

# Коды статусов (СТАРАЯ модель — пока оставлены для совместимости этапа перехода)
CODE_PRESENT = "Я"   # явка
CODE_ABSENT = "Н"    # неявка
CODE_SICK = "Б"      # больничный
CODE_VACATION = "О"  # отпуск
CODE_WEEKEND = "В"   # выходной
ALL_CODES = [CODE_PRESENT, CODE_ABSENT, CODE_SICK, CODE_VACATION, CODE_WEEKEND]

# --- НОВАЯ модель ДЕНЬ/НОЧЬ ---
# Дневной слот
DN_DAY = "Д"       # работал день
DN_REST = "О"      # отдых
DN_SICK = "Б"      # больничный
DN_ROTATION = "МЖ" # межвахта
DN_ABSENT = "Н"    # неявка
DN_MIGR = "МУ"     # миграционный учёт
# Ночной слот
DN_NIGHT = "НЧ"    # работал ночь

DAY_CODES = [DN_DAY, DN_REST, DN_SICK, DN_ROTATION, DN_ABSENT, DN_MIGR]
NIGHT_CODES = [DN_NIGHT, DN_REST]
# Причины отсутствия (для шага «оставшиеся»)
REASON_CODES = [DN_ABSENT, DN_SICK, DN_ROTATION, DN_MIGR]

# Русские названия месяцев = названия листов
MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

# Структура листа (модель ДЕНЬ/НОЧЬ):
#   строка 1 — заголовок месяца
#   строка 2 — числа дней (объединены над парой Д|Н)
#   строка 3 — подписи слотов: Д | Н | Д | Н ...
#   строки 4+ — сотрудники: A=№, B=ФИО, далее пары день/ночь
FIRST_DATA_ROW = 4
NUM_COL = 1           # столбец A = №
NAME_COL = 2          # столбец B = ФИО
FIRST_DAY_COL = 3     # столбец C = день 1 (дневной слот)

# Лист-справочник сотрудников
EMP_SHEET = "Сотрудники"
EMP_STATUS_ACTIVE = "активен"
EMP_STATUS_FIRED = "уволен"

# Лист пользователей бота (доступ)
USERS_SHEET = "Пользователи"
ROLE_ADMIN = "админ"
ROLE_FOREMAN = "прораб"

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


_ws_cache = {}


def _worksheet_for(date: datetime):
    """Лист месяца (кэшируется по названию, чтобы не читать метаданные книги)."""
    title = MONTHS_RU[date.month - 1]
    if title not in _ws_cache:
        _ws_cache[title] = _open().worksheet(title)
    return _ws_cache[title]


def _day_col(date: datetime) -> int:
    """Столбец ДНЕВНОГО слота для числа (1-based). Пары: С=1Д, D=1Н, E=2Д..."""
    return FIRST_DAY_COL + (date.day - 1) * 2


def _night_col(date: datetime) -> int:
    """Столбец НОЧНОГО слота для числа (1-based)."""
    return _day_col(date) + 1


# Совместимость со старым именем (если где-то ещё вызывается)
def _day_column(date: datetime) -> int:
    return _day_col(date)


def _employee_count(ws) -> int:
    """Сколько строк-сотрудников на листе."""
    names = ws.col_values(NAME_COL)  # включая шапку
    # names[0]=заголовок месяца (стр.1), names[1]=шапка (стр.2), дальше ФИО
    return max(0, len(names) - (FIRST_DATA_ROW - 1))


_grid_cache = {"data": None, "ts": 0, "sheet": None}
_GRID_TTL = 15  # секунд


def _read_grid(date: datetime | None = None):
    """
    Читает весь лист месяца (кэш 15 сек). Возвращает (ws, grid).
    При отметках кэш обновляется локально через _grid_set.
    """
    date = date or datetime.now()
    ws = _worksheet_for(date)
    now = time.time()
    if (_grid_cache["sheet"] != ws.title
            or now - _grid_cache["ts"] > _GRID_TTL
            or _grid_cache["data"] is None):
        _grid_cache["data"] = ws.get_all_values()
        _grid_cache["ts"] = now
        _grid_cache["sheet"] = ws.title
    return ws, _grid_cache["data"]


def _grid_set(ws, row: int, col: int, value: str):
    """Локально обновляет кэш grid после записи ячейки (row/col 1-based)."""
    if _grid_cache["sheet"] != ws.title or _grid_cache["data"] is None:
        return
    grid = _grid_cache["data"]
    ri, ci = row - 1, col - 1
    while len(grid) <= ri:
        grid.append([])
    while len(grid[ri]) <= ci:
        grid[ri].append("")
    grid[ri][ci] = value


def check_day_conflict(name: str, date: datetime | None = None) -> str | None:
    """
    Проверки перед простановкой ДНЯ (Д) сотруднику:
      - если у него в этот день уже стоит ночь (НЧ) → конфликт день/ночь
      - если вчера была ночь (НЧ) → работа сразу после ночи
    Возвращает текст предупреждения или None, если всё чисто.
    """
    date = date or datetime.now()
    ws, grid = _read_grid(date)
    n_idx = _night_col(date) - 1
    # текущий ночной слот
    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) > NAME_COL - 1 and r[NAME_COL - 1].strip() == name.strip():
            nval = r[n_idx].strip() if len(r) > n_idx else ""
            if nval == DN_NIGHT:
                return f"{name} уже отмечен в НОЧЬ за этот день."
            break
    # вчерашняя ночь
    from datetime import timedelta
    yday = date - timedelta(days=1)
    try:
        _, ygrid = _read_grid(yday)
        yn_idx = _night_col(yday) - 1
        for r in ygrid[FIRST_DATA_ROW - 1:]:
            if len(r) > NAME_COL - 1 and r[NAME_COL - 1].strip() == name.strip():
                yval = r[yn_idx].strip() if len(r) > yn_idx else ""
                if yval == DN_NIGHT:
                    return f"{name} вчера работал в НОЧЬ, положен отдых."
                break
    except Exception:
        pass
    return None


def check_night_conflict(name: str, date: datetime | None = None) -> str | None:
    """
    Проверка перед простановкой НОЧИ (НЧ):
      - если сотрудник уже отработал день (Д) → конфликт день/ночь.
    Возвращает текст предупреждения или None.
    """
    date = date or datetime.now()
    ws, grid = _read_grid(date)
    d_idx = _day_col(date) - 1
    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) > NAME_COL - 1 and r[NAME_COL - 1].strip() == name.strip():
            dval = r[d_idx].strip() if len(r) > d_idx else ""
            if dval == DN_DAY:
                return f"{name} уже отработал ДЕНЬ за эту дату."
            break
    return None


def mark_day(name: str, date: datetime | None = None) -> bool:
    """Прораб отметил присутствующего днём: ДЕНЬ=Д."""
    date = date or datetime.now()
    ws = _worksheet_for(date)
    row = _row_by_name(ws, name)
    if row is None:
        return False
    col = _day_col(date)
    ws.update_cell(row, col, DN_DAY)
    _grid_set(ws, row, col, DN_DAY)
    return True


def mark_night(name: str, date: datetime | None = None) -> bool:
    """Прораб отметил ночную смену: НОЧЬ=НЧ, ДЕНЬ=О (днём отдыхал)."""
    date = date or datetime.now()
    ws = _worksheet_for(date)
    row = _row_by_name(ws, name)
    if row is None:
        return False
    dcol, ncol = _day_col(date), _night_col(date)
    ws.update_cell(row, dcol, DN_REST)
    ws.update_cell(row, ncol, DN_NIGHT)
    _grid_set(ws, row, dcol, DN_REST)
    _grid_set(ws, row, ncol, DN_NIGHT)
    return True


def set_reason(name: str, code: str, date: datetime | None = None) -> bool:
    """Причина отсутствия в дневной слот: Н / Б / МЖ."""
    date = date or datetime.now()
    ws = _worksheet_for(date)
    row = _row_by_name(ws, name)
    if row is None:
        return False
    col = _day_col(date)
    ws.update_cell(row, col, code)
    _grid_set(ws, row, col, code)
    return True


def set_rest(name: str, date: datetime | None = None) -> bool:
    """Автоотдых с ночи: ДЕНЬ=О."""
    date = date or datetime.now()
    ws = _worksheet_for(date)
    row = _row_by_name(ws, name)
    if row is None:
        return False
    col = _day_col(date)
    ws.update_cell(row, col, DN_REST)
    _grid_set(ws, row, col, DN_REST)
    return True


def clear_day_slot(name: str, date: datetime | None = None) -> bool:
    """Очистка дневного слота сотрудника (Д→пусто). Ночной не трогаем."""
    date = date or datetime.now()
    ws = _worksheet_for(date)
    row = _row_by_name(ws, name)
    if row is None:
        return False
    col = _day_col(date)
    ws.update_cell(row, col, "")
    _grid_set(ws, row, col, "")
    return True


def clear_night_slot(name: str, date: datetime | None = None) -> bool:
    """Очистка ночного слота сотрудника (Н→пусто). Дневной не трогаем."""
    date = date or datetime.now()
    ws = _worksheet_for(date)
    row = _row_by_name(ws, name)
    if row is None:
        return False
    col = _night_col(date)
    ws.update_cell(row, col, "")
    _grid_set(ws, row, col, "")
    return True


def get_marked_day(date: datetime | None = None) -> list[str]:
    """Активные, у кого дневной слот НЕ пуст (для очистки в «Утро»)."""
    date = date or datetime.now()
    ws, grid = _read_grid(date)
    col_idx = _day_col(date) - 1
    active = get_employees(date)
    val = {}
    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) > NAME_COL - 1:
            nm = r[NAME_COL - 1].strip()
            val[nm] = r[col_idx].strip() if len(r) > col_idx else ""
    return [n for n in active if val.get(n, "")]


def get_marked_night(date: datetime | None = None) -> list[str]:
    """Активные, у кого ночной слот = НЧ (для очистки в «Вечер»)."""
    date = date or datetime.now()
    ws, grid = _read_grid(date)
    col_idx = _night_col(date) - 1
    active = get_employees(date)
    val = {}
    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) > NAME_COL - 1:
            nm = r[NAME_COL - 1].strip()
            val[nm] = r[col_idx].strip() if len(r) > col_idx else ""
    return [n for n in active if val.get(n, "") == DN_NIGHT]


def morning_progress(date: datetime | None = None) -> dict:
    """
    Состояние утренней отметки:
      marked  — сколько с непустым дневным слотом
      unmarked — сколько с пустым
    Прерванная отметка = marked > 0 И unmarked > 0.
    """
    date = date or datetime.now()
    ws, grid = _read_grid(date)
    col_idx = _day_col(date) - 1
    active = set(get_employees(date))
    marked = unmarked = 0
    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) <= NAME_COL - 1:
            continue
        nm = r[NAME_COL - 1].strip()
        if nm not in active:
            continue
        v = r[col_idx].strip() if len(r) > col_idx else ""
        if v:
            marked += 1
        else:
            unmarked += 1
    return {"marked": marked, "unmarked": unmarked,
            "interrupted": marked > 0 and unmarked > 0}


def get_night_rest(date: datetime | None = None) -> list[str]:
    """
    Кто вчера работал в ночь (НОЧЬ=НЧ) — тем сегодня положен отдых днём.
    Корректно смотрит в прошлый месяц при переходе через 1-е число.
    """
    date = date or datetime.now()
    from datetime import timedelta
    yday = date - timedelta(days=1)
    try:
        ws, grid = _read_grid(yday)
    except Exception:
        return []
    night_idx = _night_col(yday) - 1  # 0-based
    active = set(get_employees(date))
    result = []
    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) <= NAME_COL - 1:
            continue
        name = r[NAME_COL - 1].strip()
        if name in active and len(r) > night_idx and r[night_idx].strip() == DN_NIGHT:
            result.append(name)
    return result


def get_day_slot(name: str, date: datetime | None = None) -> str:
    """Текущее значение дневного слота сотрудника."""
    date = date or datetime.now()
    ws, grid = _read_grid(date)
    col_idx = _day_col(date) - 1
    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) > NAME_COL - 1 and r[NAME_COL - 1].strip() == name.strip():
            return r[col_idx].strip() if len(r) > col_idx else ""
    return ""


def get_unmarked_day(date: datetime | None = None) -> list[str]:
    """Активные, у кого дневной слот ПУСТ (ещё не отмечены утром)."""
    date = date or datetime.now()
    ws, grid = _read_grid(date)
    col_idx = _day_col(date) - 1
    active = get_employees(date)
    present = {}
    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) > NAME_COL - 1:
            nm = r[NAME_COL - 1].strip()
            present[nm] = r[col_idx].strip() if len(r) > col_idx else ""
    return [n for n in active if not present.get(n, "")]


def clear_all_day(date: datetime | None = None) -> int:
    """
    ТЕСТОВАЯ: очищает ОБА слота (день+ночь) у всех активных за день.
    Батч-запись. Возвращает число очищенных сотрудников.
    """
    date = date or datetime.now()
    active = get_employees(date)
    ws = _worksheet_for(date)
    dcol, ncol = _day_col(date), _night_col(date)
    cells = []
    for name in active:
        row = _row_by_name(ws, name)
        if row:
            cells.append(gspread.Cell(row, dcol, ""))
            cells.append(gspread.Cell(row, ncol, ""))
            _grid_set(ws, row, dcol, "")
            _grid_set(ws, row, ncol, "")
    if cells:
        ws.update_cells(cells)
    return len(active)


def fill_unmarked_absent(date: datetime | None = None) -> int:
    """
    Всем активным с пустым дневным слотом ставит Н (неявка).
    Батч-запись одним запросом. Возвращает число проставленных.
    """
    date = date or datetime.now()
    unmarked = get_unmarked_day(date)
    if not unmarked:
        return 0
    ws = _worksheet_for(date)
    col = _day_col(date)
    cells = []
    for name in unmarked:
        row = _row_by_name(ws, name)
        if row:
            cells.append(gspread.Cell(row, col, DN_ABSENT))
            _grid_set(ws, row, col, DN_ABSENT)
    if cells:
        ws.update_cells(cells)
    return len(cells)


def get_not_worked_day(date: datetime | None = None) -> list[str]:
    """
    Для вечера: активные, кто НЕ работал днём (слот != Д).
    Их можно поставить в ночь.
    """
    date = date or datetime.now()
    ws, grid = _read_grid(date)
    col_idx = _day_col(date) - 1
    active = get_employees(date)
    day_val = {}
    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) > NAME_COL - 1:
            nm = r[NAME_COL - 1].strip()
            day_val[nm] = r[col_idx].strip() if len(r) > col_idx else ""
    return [n for n in active if day_val.get(n, "") != DN_DAY]


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


def employee_exists(name: str) -> bool:
    """Есть ли уже такой сотрудник (активный или уволенный) в «Сотрудники»."""
    nm = name.strip().lower()
    return any(e["name"].strip().lower() == nm for e in get_status_list())


def add_employee(name: str, year: int = 2026) -> bool:
    """
    Добавляет нового сотрудника:
      - в лист «Сотрудники» (в конец, статус активен)
      - строкой в конец всех 12 листов месяцев (№, ФИО)
      - настраивает выпадающие списки Д/Н на новую строку
    Возвращает False, если уже существует.
    """
    import calendar as _cal
    from googleapiclient.discovery import build as _build

    name = " ".join(name.split())  # нормализуем пробелы
    if employee_exists(name):
        return False

    sp = _open()

    # 1. Лист «Сотрудники» — добавляем в конец
    try:
        ws_emp = sp.worksheet(EMP_SHEET)
    except Exception:
        return False
    emp_rows = ws_emp.get_all_values()
    next_num = len([r for r in emp_rows[1:] if r and r[0].strip()]) + 1
    ws_emp.append_row([str(next_num), name, EMP_STATUS_ACTIVE, "", ""])
    _status_cache["data"] = None

    # 2. Во все листы месяцев — строка в конец + validation
    service = _build("sheets", "v4", credentials=_credentials())
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                 for s in meta["sheets"]}

    requests = []
    for month_idx, month_name in enumerate(MONTHS_RU, 1):
        try:
            ws_m = sp.worksheet(month_name)
        except Exception:
            continue
        grid = ws_m.get_all_values()
        # номер новой строки = после последней заполненной
        last = FIRST_DATA_ROW - 1
        for i, r in enumerate(grid):
            if i >= FIRST_DATA_ROW - 1 and len(r) > NAME_COL - 1 and r[NAME_COL - 1].strip():
                last = i + 1
        new_row = last + 1
        # № и ФИО
        ws_m.update(f"A{new_row}:B{new_row}", [[next_num, name]])

        days = _cal.monthrange(year, month_idx)[1]
        sheet_id = sheet_ids.get(month_name)
        if sheet_id is None:
            continue
        for d in range(days):
            day_col = (FIRST_DAY_COL - 1) + d * 2  # 0-based
            night_col = day_col + 1
            requests.append(_dv_row(sheet_id, new_row - 1, day_col, DAY_CODES))
            requests.append(_dv_row(sheet_id, new_row - 1, night_col, NIGHT_CODES))

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID, body={"requests": requests}).execute()

    # сбрасываем кэши
    _ws_cache.clear()
    _grid_cache["data"] = None
    _rowmap_cache["data"] = {}
    return True


def _dv_row(sheet_id, row_idx0, col_idx0, codes):
    """Data validation для одной ячейки (row/col 0-based)."""
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_idx0, "endRowIndex": row_idx0 + 1,
                "startColumnIndex": col_idx0, "endColumnIndex": col_idx0 + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": c} for c in codes],
                },
                "showCustomUi": True, "strict": False,
            },
        }
    }


_rowmap_cache = {"data": {}, "ts": 0, "sheet": None}
_ROWMAP_TTL = 60  # секунд


def _row_by_name(ws, name: str) -> int | None:
    """
    Номер строки сотрудника по ФИО. Карта ФИО→строка кэшируется на 60 сек
    для листа, чтобы не читать столбец на каждый тап.
    """
    now = time.time()
    if (_rowmap_cache["sheet"] != ws.title
            or now - _rowmap_cache["ts"] > _ROWMAP_TTL
            or not _rowmap_cache["data"]):
        names = ws.col_values(NAME_COL)
        m = {}
        for i, n in enumerate(names):
            if i >= FIRST_DATA_ROW - 1 and n.strip():
                m[n.strip()] = i + 1
        _rowmap_cache["data"] = m
        _rowmap_cache["ts"] = now
        _rowmap_cache["sheet"] = ws.title
    return _rowmap_cache["data"].get(name.strip())


def set_rotation_return(name: str, return_date: str) -> bool:
    """Сохраняет дату возврата с межвахты в лист «Сотрудники» (столбец E)."""
    try:
        ws = _open().worksheet(EMP_SHEET)
    except Exception:
        return False
    grid = ws.get_all_values()
    for i, r in enumerate(grid):
        if i == 0:
            continue
        if len(r) >= 2 and r[1].strip() == name.strip():
            ws.update_cell(i + 1, 5, return_date)  # E = Межвахта до
            _status_cache["data"] = None
            return True
    return False


def get_rotation_reminders(days_before: int = 3) -> list[dict]:
    """
    Возвращает тех, кто возвращается с межвахты в пределах days_before дней.
    [{"name", "return_date"}]. Дата в формате ДД.ММ.
    """
    from datetime import timedelta
    try:
        ws = _open().worksheet(EMP_SHEET)
    except Exception:
        return []
    grid = ws.get_all_values()
    today = datetime.now().date()
    result = []
    for i, r in enumerate(grid):
        if i == 0 or len(r) < 5:
            continue
        raw = r[4].strip()
        if not raw:
            continue
        # парсим ДД.ММ или ДД.ММ.ГГГГ
        parts = raw.split(".")
        try:
            d = int(parts[0]); m = int(parts[1])
            y = int(parts[2]) if len(parts) > 2 else today.year
            ret = datetime(y, m, d).date()
        except Exception:
            continue
        delta = (ret - today).days
        if 0 <= delta <= days_before:
            result.append({"name": r[1].strip(), "return_date": raw})
    return result


def get_current_status(emp_index: int, date: datetime | None = None) -> str:
    """
    Возвращает текущее значение ДНЕВНОГО слота сотрудника.
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
    col = _day_col(date)
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
    Сводка за день (модель день/ночь) по активным сотрудникам.
    Считает дневной и ночной слоты. Один запрос на лист.
    """
    date = date or datetime.now()
    active = set(get_employees(date))
    ws = _worksheet_for(date)
    grid = ws.get_all_values()

    d_idx = _day_col(date) - 1
    n_idx = _night_col(date) - 1

    day_work = night_work = rest = sick = rotation = absent = 0
    absent_list = []

    for r in grid[FIRST_DATA_ROW - 1:]:
        if len(r) < NAME_COL:
            continue
        name = r[NAME_COL - 1].strip()
        if not name or name not in active:
            continue
        dval = r[d_idx].strip() if len(r) > d_idx else ""
        nval = r[n_idx].strip() if len(r) > n_idx else ""

        if dval == DN_DAY:
            day_work += 1
        elif dval == DN_REST:
            rest += 1
        elif dval == DN_SICK:
            sick += 1
            absent_list.append((name, dval))
        elif dval == DN_ROTATION:
            rotation += 1
            absent_list.append((name, dval))
        elif dval == DN_ABSENT:
            absent += 1
            absent_list.append((name, dval))

        if nval == DN_NIGHT:
            night_work += 1

    return {
        "day": day_work, "night": night_work, "rest": rest,
        "sick": sick, "rotation": rotation, "absent": absent,
        "absent_list": absent_list, "total": len(active),
    }


def fire_employee(name: str, fire_date_str: str) -> bool:
    """
    Помечает сотрудника уволенным в листе «Сотрудники»:
    статус → 'уволен', дата увольнения → строка (ДД.ММ.ГГГГ или ДД.ММ).
    Строки в листах месяцев не трогает (история сохраняется).
    """
    try:
        ws = _open().worksheet(EMP_SHEET)
    except Exception:
        return False
    grid = ws.get_all_values()
    for i, r in enumerate(grid):
        if i == 0:
            continue  # шапка
        if len(r) >= 2 and r[1].strip() == name.strip():
            row = i + 1
            ws.update_cell(row, 3, EMP_STATUS_FIRED)
            ws.update_cell(row, 4, fire_date_str)
            _status_cache["data"] = None
            return True
    return False


def build_work_report(name: str, out_path: str, year: int = 2026) -> str | None:
    """
    Excel-график работы уволенного за месяцы, где он работал (модель день/ночь).
    Колонки: Число | День | Ночь. Возвращает путь или None.
    """
    import calendar as _cal
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
        emp_row = None
        for r in grid[FIRST_DATA_ROW - 1:]:
            if len(r) >= NAME_COL and r[NAME_COL - 1].strip() == name.strip():
                emp_row = r
                break
        if not emp_row:
            continue

        days = _cal.monthrange(year, month_idx)[1]
        # пары день/ночь начиная с FIRST_DAY_COL
        has_data = False
        rows_out = []
        for d in range(1, days + 1):
            d_col = FIRST_DAY_COL - 1 + (d - 1) * 2   # 0-based
            n_col = d_col + 1
            dval = emp_row[d_col].strip() if len(emp_row) > d_col else ""
            nval = emp_row[n_col].strip() if len(emp_row) > n_col else ""
            if dval or nval:
                has_data = True
            rows_out.append((d, dval, nval))
        if not has_data:
            continue

        any_data = True
        ws_out = wb.create_sheet(month_name)
        ws_out["A1"] = f"{name} — {month_name} {year}"
        ws_out["A1"].font = Font(bold=True, size=12)
        for col, title in enumerate(["Число", "День", "Ночь"], 1):
            c = ws_out.cell(row=2, column=col, value=title)
            c.font = bold
            c.border = thin
            c.alignment = center
        for i, (d, dval, nval) in enumerate(rows_out, 1):
            ws_out.cell(row=i + 2, column=1, value=d).border = thin
            cd = ws_out.cell(row=i + 2, column=2, value=dval)
            cn = ws_out.cell(row=i + 2, column=3, value=nval)
            for c in (cd, cn):
                c.border = thin
                c.alignment = center
        ws_out.column_dimensions["A"].width = 7
        ws_out.column_dimensions["B"].width = 8
        ws_out.column_dimensions["C"].width = 8

    if not any_data:
        return None
    wb.save(out_path)
    return out_path


# ================= ПОЛЬЗОВАТЕЛИ (ДОСТУП) =================

_users_cache = {"data": None, "ts": 0}
_USERS_TTL = 20


def _ensure_users_sheet():
    """Создаёт лист «Пользователи», если его нет. Заполняет шапку."""
    sp = _open()
    try:
        sp.worksheet(USERS_SHEET)
        return
    except Exception:
        pass
    sp.batch_update({"requests": [
        {"addSheet": {"properties": {"title": USERS_SHEET}}}
    ]})
    ws = sp.worksheet(USERS_SHEET)
    ws.update("A1", [["chat_id", "Имя", "Роль"]])


def get_users(force: bool = False) -> list[dict]:
    """Список пользователей бота: [{chat_id, name, role}]."""
    now = time.time()
    if (not force and _users_cache["data"] is not None
            and now - _users_cache["ts"] < _USERS_TTL):
        return _users_cache["data"]
    try:
        ws = _open().worksheet(USERS_SHEET)
    except Exception:
        return []
    rows = ws.get_all_values()[1:]
    result = []
    for r in rows:
        if r and r[0].strip():
            try:
                cid = int(r[0].strip())
            except ValueError:
                continue
            result.append({
                "chat_id": cid,
                "name": (r[1].strip() if len(r) > 1 else ""),
                "role": (r[2].strip() if len(r) > 2 else ROLE_FOREMAN),
            })
    _users_cache["data"] = result
    _users_cache["ts"] = now
    return result


def is_allowed(chat_id: int) -> bool:
    """Разрешён ли пользователь (есть в списке)."""
    return any(u["chat_id"] == chat_id for u in get_users())


def get_role(chat_id: int) -> str | None:
    for u in get_users():
        if u["chat_id"] == chat_id:
            return u["role"]
    return None


def add_user(chat_id: int, name: str = "", role: str = ROLE_FOREMAN) -> bool:
    """Добавляет пользователя в лист «Пользователи»."""
    _ensure_users_sheet()
    if is_allowed(chat_id):
        return False  # уже есть
    ws = _open().worksheet(USERS_SHEET)
    ws.append_row([str(chat_id), name, role])
    _users_cache["data"] = None
    return True


def get_admins() -> list[int]:
    """chat_id всех админов."""
    return [u["chat_id"] for u in get_users() if u["role"] == ROLE_ADMIN]
