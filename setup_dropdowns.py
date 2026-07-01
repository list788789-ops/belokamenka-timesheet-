"""
Разовая настройка выпадающих списков (Data Validation) во всех ячейках табеля.
Запусти один раз после создания таблицы:  python setup_dropdowns.py

Добавляет в каждую ячейку дня выбор из: Я / Н / Б / О / В
"""

import calendar

from googleapiclient.discovery import build

from sheets import (
    SPREADSHEET_ID, MONTHS_RU, ALL_CODES,
    FIRST_DATA_ROW, FIRST_DAY_COL, _credentials,
)

YEAR = 2026


def main():
    service = build("sheets", "v4", credentials=_credentials())

    # Получаем метаданные: sheetId для каждого листа-месяца
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                 for s in meta["sheets"]}

    # Узнаём число сотрудников по первому листу
    rng = f"{MONTHS_RU[0]}!A:A"
    col = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=rng).execute().get("values", [])
    n_emp = max(0, len(col) - (FIRST_DATA_ROW - 1))
    if n_emp == 0:
        print("Не найдены сотрудники — проверь структуру листа.")
        return

    requests = []
    for month_idx, month_name in enumerate(MONTHS_RU, 1):
        sheet_id = sheet_ids.get(month_name)
        if sheet_id is None:
            continue
        days = calendar.monthrange(YEAR, month_idx)[1]

        requests.append({
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": FIRST_DATA_ROW - 1,           # 0-индексация
                    "endRowIndex": FIRST_DATA_ROW - 1 + n_emp,
                    "startColumnIndex": FIRST_DAY_COL - 1,         # столбец B
                    "endColumnIndex": FIRST_DAY_COL - 1 + days,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": c} for c in ALL_CODES],
                    },
                    "showCustomUi": True,       # выпадающий список (стрелка)
                    "strict": False,            # не блокировать ручной ввод
                },
            }
        })

    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": requests},
    ).execute()
    print(f"Выпадающие списки добавлены на {len(requests)} листах "
          f"({n_emp} сотрудников).")


if __name__ == "__main__":
    main()
