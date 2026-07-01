"""
Разовая реорганизация табеля во всех 12 листах:
  - столбец A = "№" (нумерация 1..N)
  - столбец B = "ФИО" (отсортировано по алфавиту)
  - дни месяца сдвигаются на столбец C и далее
  - выпадающие списки перенастраиваются под новую структуру

Запускается один раз через переменную RUN_REORG=1 (в main.py) или локально:
    python reorganize.py
"""

import calendar

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

from sheets import SPREADSHEET_ID, MONTHS_RU, ALL_CODES, _credentials

YEAR = 2026
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Отсортированный по алфавиту список сотрудников
EMPLOYEES = [
    "Алдабергенов Агыбай Абдикулулы",
    "Амирхан Бауыржан Амандыкулы",
    "Балгабаев Канат Серикович",
    "Бердалы Омар Жарабекулы",
    "Бердилла Турар Нуралиулы",
    "Деканов Эргаш",
    "Есбенбетов Жанабек",
    "Жанен Аскар Бакытжанулы",
    "Жузимбеков Бакберген Асилбекулы",
    "Жунисов Габид Калдарбекович",
    "Зулпиханов Жамбул",
    "Иззатуллаев Элёр Маматрасулулы",
    "Канжигит Нурбек Мусырманкулулы",
    "Канжигитов Айбек",
    "Кулбаев Ерлан Серикбайугли",
    "Лесбеков Шукирали Умбеталиевич",
    "Маман Нурбек Усенкулулы",
    "Маматрасулов Билол Отабекович",
    "Мейрамбай Айтбай Бакытулы",
    "Мейрамбай Нурлыбай Бакытулы",
    "Пулатов Бахтиёр",
    "Садирханов Майрамбек Габидович",
    "Саргулжаев Медгат Медельханович",
    "Сарсенбаев Серикжан Турдыкулович",
    "Совет Сагат Сейлханулы",
    "Султанбеков Сабит",
    "Торебек Сергазы Дуйбенбекулы",
    "Туребек Алибек Дуйсенбекулы",
    "Умбетали Казыбек Шукиралиулы",
    "Уристемов Аскар",
    "Хидиров Ардак Аскарбаевич",
    "Шайхудинов Темир Ералиевич",
    "Шахмет Асыл Абдималикулы",
    "Шахмет Ерзат Абдималикулы",
    "Шерзатулы Рамазан",
]

FIRST_DATA_ROW = 3          # строки с сотрудниками начинаются с 3-й
NEW_FIRST_DAY_COL_IDX = 2   # 0-индекс столбца C (день 1) в новой структуре


def reorganize():
    service = build("sheets", "v4", credentials=_credentials())
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                 for s in meta["sheets"]}

    n = len(EMPLOYEES)

    for month_idx, month_name in enumerate(MONTHS_RU, 1):
        sheet_id = sheet_ids.get(month_name)
        if sheet_id is None:
            continue
        days = calendar.monthrange(YEAR, month_idx)[1]

        # 1. Полностью очищаем область A:AF (шапка + данные), чтобы переписать
        clear_range = f"{month_name}!A2:AG{FIRST_DATA_ROW + n}"
        service.spreadsheets().values().clear(
            spreadsheetId=SPREADSHEET_ID, range=clear_range).execute()

        # 2. Формируем новые значения
        # Шапка (строка 2): №, ФИО, 1, 2, ..., days
        header = ["№", "ФИО"] + [str(d) for d in range(1, days + 1)]
        rows = [header]
        # Данные: номер, ФИО, пустые ячейки под дни
        for i, name in enumerate(EMPLOYEES, 1):
            rows.append([i, name] + [""] * days)

        body = {"values": rows}
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{month_name}!A2",
            valueInputOption="RAW",
            body=body,
        ).execute()

        # 3. Перенастраиваем выпадающие списки на новые дни (столбцы C..)
        req = [{
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": FIRST_DATA_ROW - 1,
                    "endRowIndex": FIRST_DATA_ROW - 1 + n,
                    "startColumnIndex": NEW_FIRST_DAY_COL_IDX,       # столбец C
                    "endColumnIndex": NEW_FIRST_DAY_COL_IDX + days,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": c} for c in ALL_CODES],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        }]
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID, body={"requests": req}).execute()

    print(f"Реорганизация завершена: {n} сотрудников, 12 листов.")
    return n


if __name__ == "__main__":
    reorganize()
