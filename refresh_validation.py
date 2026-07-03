"""
Перенастройка выпадающих списков под коды с МУ (миграционный учёт).

Обновляет data validation во всех 12 листах:
  Д-столбцы → DAY_CODES (с МУ)
  Н-столбцы → NIGHT_CODES
Данные в ячейках не трогает.

Запуск через RUN_REFRESH_DV=1 (в main.py) или локально.
"""

import calendar

from googleapiclient.discovery import build

from sheets import (
    SPREADSHEET_ID, MONTHS_RU, _credentials,
    DAY_CODES, NIGHT_CODES,
)
from reorganize import EMPLOYEES

YEAR = 2026
FIRST_DATA_ROW = 4
FIRST_DAY_COL_IDX = 2  # 0-based столбец C


def _dv_request(sheet_id, r0, r1, c0, c1, codes):
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": r0, "endRowIndex": r1,
                "startColumnIndex": c0, "endColumnIndex": c1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": c} for c in codes],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    }


def refresh_validation():
    service = build("sheets", "v4", credentials=_credentials())
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                 for s in meta["sheets"]}

    n_emp = len(EMPLOYEES)
    for month_idx, month_name in enumerate(MONTHS_RU, 1):
        sheet_id = sheet_ids.get(month_name)
        if sheet_id is None:
            continue
        days = calendar.monthrange(YEAR, month_idx)[1]
        # ставим списки с запасом (до строки 50), чтобы покрыть возможные
        # сдвиги и будущие добавления сотрудников
        last_row = 50
        total_cols = FIRST_DAY_COL_IDX + days * 2

        requests = []

        # 1. Снимаем ЛЮБУЮ validation со строк шапки (0..FIRST_DATA_ROW-1),
        #    чтобы убрать зависшие правила от старой структуры.
        #    setDataValidation без "rule" очищает правило в диапазоне.
        requests.append({
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": FIRST_DATA_ROW - 1,   # строки 1..3
                    "startColumnIndex": 0,
                    "endColumnIndex": total_cols,
                }
            }
        })

        # 2. Ставим списки только на строки сотрудников
        for d in range(days):
            day_col = FIRST_DAY_COL_IDX + d * 2
            night_col = day_col + 1
            requests.append(_dv_request(
                sheet_id, FIRST_DATA_ROW - 1, last_row,
                day_col, day_col + 1, DAY_CODES))
            requests.append(_dv_request(
                sheet_id, FIRST_DATA_ROW - 1, last_row,
                night_col, night_col + 1, NIGHT_CODES))

        # 3. Форматируем строку слотов Д/Н (строка 3) как синюю шапку дней:
        #    синий фон, белый жирный текст, по центру. Столбцы только слотов.
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 2, "endRowIndex": 3,        # строка 3
                    "startColumnIndex": FIRST_DAY_COL_IDX,
                    "endColumnIndex": total_cols,
                },
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.26, "green": 0.45, "blue": 0.76},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "textFormat": {"bold": True,
                                   "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                }},
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
            }
        })

        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID, body={"requests": requests}).execute()

    print(f"Выпадающие списки обновлены (с МУ) на 12 листах.")
    return n_emp


if __name__ == "__main__":
    refresh_validation()
