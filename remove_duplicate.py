"""
РАЗОВЫЙ скрипт: удаляет строку сотрудника-дубликата из листа «Сотрудники»
и из всех 12 листов месяцев по точному совпадению ФИО.

Использование: как и другие разовые скрипты (add_hire_date.py, reorganize.py
и т.п.) — вызвать remove_duplicate() один раз из main.py через переменную
окружения RUN_REMOVE_DUPLICATE=1, затем переменную убрать/выключить.

БЕЗОПАСНОСТЬ: перед удалением скрипт проверяет, что во всех месяцах у
дубликата НЕТ ни одной непустой ячейки (день/ночь) — если данные найдутся,
скрипт останавливается и ничего не удаляет, чтобы не потерять реальную
посещаемость.
"""

import sheets


# Точное ФИО дубликата, который удаляем (оставляем оригинал с полным ФИО).
DUPLICATE_NAME = "Иззатуллаев Элёр"


def _has_any_data(row_values: list, first_day_col_0based: int) -> bool:
    """Проверяет, есть ли хоть одна непустая ячейка день/ночь у строки."""
    for v in row_values[first_day_col_0based:]:
        if v and str(v).strip():
            return True
    return False


def remove_duplicate(name: str = DUPLICATE_NAME) -> dict:
    """
    Удаляет строку `name` из «Сотрудники» и всех 12 листов месяцев.
    Возвращает {"removed": [...], "skipped_has_data": [...], "not_found": [...]}.
    Ничего не удаляет, если хоть в одном месяце есть данные — сначала
    вернёт skipped_has_data, чтобы можно было проверить руками.
    """
    sp = sheets._open()
    nm = name.strip().lower()

    # 1. Собираем все sheetId и находим строки на удаление, СНАЧАЛА без
    # удаления — чтобы проверить данные перед тем как что-то трогать.
    to_delete = []  # [(sheet_title, sheetId, row_1based)]
    has_data_sheets = []

    # «Сотрудники»
    ws_emp = sp.worksheet(sheets.EMP_SHEET)
    emp_grid = ws_emp.get_all_values()
    emp_row = None
    for i, r in enumerate(emp_grid):
        if i == 0:
            continue
        if len(r) >= 2 and r[1].strip().lower() == nm:
            emp_row = i + 1
            break
    if emp_row is None:
        return {"removed": [], "skipped_has_data": [], "not_found": [name]}

    # 12 месяцев
    for month_name in sheets.MONTHS_RU:
        try:
            ws_m = sp.worksheet(month_name)
        except Exception:
            continue
        grid = ws_m.get_all_values()
        for i, r in enumerate(grid):
            if i < sheets.FIRST_DATA_ROW - 1:
                continue
            if len(r) > sheets.NAME_COL - 1 and r[sheets.NAME_COL - 1].strip().lower() == nm:
                row_1based = i + 1
                day_col_0 = sheets.FIRST_DAY_COL - 1
                if _has_any_data(r, day_col_0):
                    has_data_sheets.append(month_name)
                to_delete.append((month_name, row_1based))
                break

    if has_data_sheets:
        return {
            "removed": [],
            "skipped_has_data": has_data_sheets,
            "not_found": [],
        }

    # 2. Ничего не найдено с данными — безопасно удаляем.
    # Метаданные sheetId (кэш модуля sheets.py уже умеет их кэшировать).
    from googleapiclient.discovery import build as _build
    service = _build("sheets", "v4", credentials=sheets._credentials())
    sheet_ids = sheets._get_sheet_ids(service)

    requests = [{
        "deleteDimension": {
            "range": {
                "sheetId": sheet_ids[sheets.EMP_SHEET],
                "dimension": "ROWS",
                "startIndex": emp_row - 1,
                "endIndex": emp_row,
            }
        }
    }]
    for month_name, row_1based in to_delete:
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_ids[month_name],
                    "dimension": "ROWS",
                    "startIndex": row_1based - 1,
                    "endIndex": row_1based,
                }
            }
        })

    service.spreadsheets().batchUpdate(
        spreadsheetId=sheets.SPREADSHEET_ID, body={"requests": requests}).execute()

    # сбрасываем все кэши, раз структура листов поменялась
    sheets._ws_cache.clear()
    sheets._grid_cache["data"] = None
    sheets._rowmap_cache["data"] = {}
    sheets._status_cache["data"] = None

    return {
        "removed": [sheets.EMP_SHEET] + [m for m, _ in to_delete],
        "skipped_has_data": [],
        "not_found": [],
    }


if __name__ == "__main__":
    result = remove_duplicate()
    print(result)
