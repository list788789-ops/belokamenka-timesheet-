"""
РАЗОВЫЙ скрипт: у всех строк, добавленных через add_employee до правки
границ (см. sheets.py) — это строки 38+ во всех 12 листах месяцев —
отсутствовала граница на столбцах A (№) и B (ФИО). add_employee строил
границы только для дневных/ночных столбцов, про A/B забыл с самого начала.

Скрипт заново накатывает тонкую чёрную границу на A:B для ВСЕХ строк с
данными в каждом из 12 месяцев — идемпотентно: у строк 1-37 граница там
уже есть и просто перезапишется той же, ничего не испортит.

Запуск: RUN_REPAIR_AB_BORDERS=1 в Railway, один раз, затем убрать.
"""

import sheets


def repair_ab_borders() -> dict:
    from googleapiclient.discovery import build as _build

    sp = sheets._open()
    service = _build("sheets", "v4", credentials=sheets._credentials())
    sheet_ids = sheets._get_sheet_ids(service)

    black_thin = {"style": "SOLID", "color": {"red": 0, "green": 0, "blue": 0}}
    requests = []
    report = {}

    for month_name in sheets.MONTHS_RU:
        try:
            ws_m = sheets._ws_by_title(sp, month_name)
        except Exception:
            continue
        grid = ws_m.get_all_values()
        last_row = sheets.FIRST_DATA_ROW - 1
        for i, r in enumerate(grid):
            if i < sheets.FIRST_DATA_ROW - 1:
                continue
            if len(r) > sheets.NAME_COL - 1 and r[sheets.NAME_COL - 1].strip():
                last_row = i + 1
        if last_row < sheets.FIRST_DATA_ROW:
            continue
        sheet_id = sheet_ids.get(month_name)
        if sheet_id is None:
            continue
        requests.append({
            "updateBorders": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": sheets.FIRST_DATA_ROW - 1, "endRowIndex": last_row,
                    "startColumnIndex": 0, "endColumnIndex": 2,
                },
                "top": black_thin, "bottom": black_thin,
                "left": black_thin, "right": black_thin,
                "innerHorizontal": black_thin, "innerVertical": black_thin,
            }
        })
        report[month_name] = last_row - (sheets.FIRST_DATA_ROW - 1)

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheets.SPREADSHEET_ID, body={"requests": requests}).execute()

    return report


if __name__ == "__main__":
    print(repair_ab_borders())
