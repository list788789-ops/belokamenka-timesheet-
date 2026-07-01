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
        last_row = FIRST_DATA_ROW + n_emp

        requests = []
        for d in range(days):
            day_col = FIRST_DAY_COL_IDX + d * 2
            night_col = day_col + 1
            requests.append(_dv_request(
                sheet_id, FIRST_DATA_ROW - 1, last_row,
                day_col, day_col + 1, DAY_CODES))
            requests.append(_dv_request(
                sheet_id, FIRST_DATA_ROW - 1, last_row,
                night_col, night_col + 1, NIGHT_CODES))

        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID, body={"requests": requests}).execute()

    print(f"Выпадающие списки обновлены (с МУ) на 12 листах.")
    return n_emp


if __name__ == "__main__":
    refresh_validation()
