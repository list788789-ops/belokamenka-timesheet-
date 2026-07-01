"""
ЭТАП 1 — Пересоздание структуры табеля под модель ДЕНЬ/НОЧЬ (с косметикой).

Структура каждого листа-месяца:
    строка 1: заголовок месяца
    строка 2: числа дней (объединены над парой Д|Н)
    строка 3: подписи слотов  Д | Н | Д | Н ...
    строки 4+: сотрудники (A=№, B=ФИО, далее пары день/ночь)

    Дневной слот (Д): Д / О / Б / МЖ / Н
    Ночной слот (Н):  НЧ / О

ВНИМАНИЕ: обнуляет все старые данные в листах месяцев.
Запуск через RUN_REBUILD_DN=1 (в main.py) или локально.
"""

import calendar

from googleapiclient.discovery import build

from sheets import (
    SPREADSHEET_ID, MONTHS_RU, _credentials,
    DAY_CODES, NIGHT_CODES,
)
from reorganize import EMPLOYEES

YEAR = 2026
FIRST_DATA_ROW = 4          # данные теперь с 4-й строки
FIRST_DAY_COL_IDX = 2       # 0-based индекс столбца C (первый дневной слот)


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


def _merge_request(sheet_id, row, c0, c1):
    return {
        "mergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row, "endRowIndex": row + 1,
                "startColumnIndex": c0, "endColumnIndex": c1,
            },
            "mergeType": "MERGE_ALL",
        }
    }


def _weekend_bg_request(sheet_id, c0, c1, r0, r1):
    """Розовая заливка для выходных столбцов."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": r0, "endRowIndex": r1,
                "startColumnIndex": c0, "endColumnIndex": c1,
            },
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.99, "green": 0.89, "blue": 0.84}
            }},
            "fields": "userEnteredFormat.backgroundColor",
        }
    }


def rebuild_daynight():
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

        # 1. Очистка
        service.spreadsheets().values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{month_name}!A1:BZ{last_row + 5}",
        ).execute()

        # 2. Значения: строка1 заголовок, строка2 числа, строка3 слоты
        row1 = [f"{month_name} {YEAR}"]
        row2 = ["", ""]   # под № и ФИО
        row3 = ["№", "ФИО"]
        for d in range(1, days + 1):
            row2.append(str(d)); row2.append("")   # число над парой (вторая ячейка пустая — объединим)
            row3.append("Д"); row3.append("Н")

        rows = [row1, row2, row3]
        for i, name in enumerate(EMPLOYEES, 1):
            rows.append([i, name] + [""] * (days * 2))

        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{month_name}!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()

        # 3. Оформление: объединения, выпадающие списки, выходные
        requests = []

        # объединяем число дня над парой Д|Н (строка 2, 0-based index 1)
        for d in range(days):
            c0 = FIRST_DAY_COL_IDX + d * 2
            requests.append(_merge_request(sheet_id, 1, c0, c0 + 2))

        # центрируем строку чисел (строка 2) над парами
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1, "endRowIndex": 2,
                    "startColumnIndex": FIRST_DAY_COL_IDX,
                    "endColumnIndex": FIRST_DAY_COL_IDX + days * 2,
                },
                "cell": {"userEnteredFormat": {
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "textFormat": {"bold": True},
                }},
                "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,textFormat)",
            }
        })

        # выпадающие списки + подсветка выходных
        for d in range(days):
            day_col = FIRST_DAY_COL_IDX + d * 2
            night_col = day_col + 1
            requests.append(_dv_request(
                sheet_id, FIRST_DATA_ROW - 1, last_row,
                day_col, day_col + 1, DAY_CODES))
            requests.append(_dv_request(
                sheet_id, FIRST_DATA_ROW - 1, last_row,
                night_col, night_col + 1, NIGHT_CODES))
            # выходной?
            weekday = calendar.weekday(YEAR, month_idx, d + 1)
            if weekday >= 5:
                requests.append(_weekend_bg_request(
                    sheet_id, day_col, night_col + 1, 1, last_row))

        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID, body={"requests": requests}).execute()

    print(f"Структура ДЕНЬ/НОЧЬ пересоздана: {n_emp} сотрудников, 12 листов.")
    return n_emp


if __name__ == "__main__":
    rebuild_daynight()
