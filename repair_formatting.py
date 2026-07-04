"""
РАЗОВЫЙ скрипт: чинит два пробела, обнаруженных после сбоев по write-квоте
при массовой загрузке 04.07.2026:

  - Алгабас Мейиржан Галымжанулы (Таб.№58) — есть в «Сотрудники»,
    но отсутствует во ВСЕХ 12 листах месяцев (строка не была создана).
  - Мырзакулов Абдирахим Абдиманапович (Таб.№63, последний в списке) —
    строка есть, но не применены выпадающие списки/розовый/границы
    (сбой произошёл именно на структурном batchUpdate для этой строки).

Логика для каждого имени из TARGET_NAMES, для каждого из 12 месяцев:
  1. Ищем строку по ФИО. Если нет — создаём (следующая свободная строка).
  2. Независимо от того, была строка или создана заново — применяем
     заново dropdown Д/Н, розовый фон выходных, границы. Безопасно
     применять повторно, даже если формат уже стоял.

Запуск: RUN_REPAIR_FORMATTING=1 в Railway, один раз, затем убрать.
"""

import calendar as _cal

import sheets


TARGET_NAMES = [
    "Алгабас Мейиржан Галымжанулы",
    "Мырзакулов Абдирахим Абдиманапович",
]


def repair_formatting(names: list[str] = TARGET_NAMES, year: int = 2026) -> dict:
    from googleapiclient.discovery import build as _build

    sp = sheets._open()
    service = _build("sheets", "v4", credentials=sheets._credentials())
    sheet_ids = sheets._get_sheet_ids(service)

    # Таб.№ из «Сотрудники» — нужен только если придётся создавать строку.
    ws_emp = sp.worksheet(sheets.EMP_SHEET)
    emp_grid = ws_emp.get_all_values()
    num_by_name = {}
    for r in emp_grid[1:]:
        if len(r) >= 2 and r[1].strip():
            try:
                num_by_name[r[1].strip()] = int(r[0].strip())
            except ValueError:
                pass

    value_data = []
    requests = []
    report = {"created_rows": [], "reformatted_rows": [], "unknown_names": []}

    for name in names:
        if name not in num_by_name:
            report["unknown_names"].append(name)

    for month_idx, month_name in enumerate(sheets.MONTHS_RU, 1):
        try:
            ws_m = sheets._ws_by_title(sp, month_name)
        except Exception:
            continue
        grid = ws_m.get_all_values()

        last_row = sheets.FIRST_DATA_ROW - 1
        row_by_name = {}
        for i, r in enumerate(grid):
            if i < sheets.FIRST_DATA_ROW - 1:
                continue
            if len(r) > sheets.NAME_COL - 1 and r[sheets.NAME_COL - 1].strip():
                nm = r[sheets.NAME_COL - 1].strip()
                row_by_name[nm] = i + 1
                last_row = i + 1

        days = _cal.monthrange(year, month_idx)[1]
        sheet_id = sheet_ids.get(month_name)

        for name in names:
            row = row_by_name.get(name)
            if row is None:
                last_row += 1
                row = last_row
                num = num_by_name.get(name, "")
                value_data.append({
                    "range": f"{month_name}!A{row}:B{row}",
                    "values": [[num, name]],
                })
                report["created_rows"].append(f"{month_name}: {name} (строка {row})")
            else:
                report["reformatted_rows"].append(f"{month_name}: {name} (строка {row})")

            if sheet_id is None:
                continue
            pink = {"red": 0.99, "green": 0.89, "blue": 0.84}
            thick = {"style": "SOLID_THICK", "color": {"red": 0, "green": 0, "blue": 0}}
            thin = {"style": "SOLID", "color": {"red": 0.6, "green": 0.6, "blue": 0.6}}
            for d in range(days):
                day_col = (sheets.FIRST_DAY_COL - 1) + d * 2
                night_col = day_col + 1
                requests.append(sheets._dv_row(sheet_id, row - 1, day_col, sheets.DAY_CODES))
                requests.append(sheets._dv_row(sheet_id, row - 1, night_col, sheets.NIGHT_CODES))
                if _cal.weekday(year, month_idx, d + 1) >= 5:
                    requests.append({
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": row - 1, "endRowIndex": row,
                                "startColumnIndex": day_col, "endColumnIndex": day_col + 2,
                            },
                            "cell": {"userEnteredFormat": {"backgroundColor": pink}},
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    })
                requests.append({
                    "updateBorders": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row - 1, "endRowIndex": row,
                            "startColumnIndex": day_col, "endColumnIndex": day_col + 2,
                        },
                        "left": thick, "right": thick, "bottom": thin,
                        "innerVertical": thin,
                    }
                })

    if value_data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheets.SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": value_data}
        ).execute()

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheets.SPREADSHEET_ID, body={"requests": requests}).execute()

    sheets._grid_cache["data"] = None
    sheets._rowmap_cache["data"] = {}

    return report


if __name__ == "__main__":
    print(repair_formatting())
