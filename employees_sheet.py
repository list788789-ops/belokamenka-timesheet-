"""
Разовое создание листа «Сотрудники» — источник списка активных/уволенных.

Структура листа «Сотрудники»:
    A: №
    B: ФИО
    C: Статус (активен / уволен)
    D: Дата увольнения (пусто у активных)

Запускается один раз через RUN_EMPLOYEES=1 (в main.py) или локально:
    python employees_sheet.py
"""

from googleapiclient.discovery import build

from sheets import SPREADSHEET_ID, _credentials
from reorganize import EMPLOYEES  # отсортированный список ФИО

SHEET_NAME = "Сотрудники"
STATUS_ACTIVE = "активен"
STATUS_FIRED = "уволен"


def create_employees_sheet():
    service = build("sheets", "v4", credentials=_credentials())
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    titles = {s["properties"]["title"] for s in meta["sheets"]}

    # 1. Создаём лист, если его ещё нет
    if SHEET_NAME not in titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
        ).execute()

    # 2. Заполняем шапку и данные
    rows = [["№", "ФИО", "Статус", "Дата увольнения"]]
    for i, name in enumerate(EMPLOYEES, 1):
        rows.append([i, name, STATUS_ACTIVE, ""])

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()

    print(f"Лист «{SHEET_NAME}» создан/обновлён: {len(EMPLOYEES)} активных.")
    return len(EMPLOYEES)


if __name__ == "__main__":
    create_employees_sheet()
