"""
Разовый скрипт: добавляет столбец F «Дата приёма» в лист «Сотрудники».
Активным проставляет 01.07.2026, уволенных не трогает.

Запуск через RUN_HIRE_DATE=1 (в main.py) или локально.
Данные (статус, увольнение, межвахта) не затрагивает — пишет только столбец F.
"""

from sheets import _open, EMP_SHEET, EMP_STATUS_ACTIVE

HIRE_DATE_DEFAULT = "01.07.2026"
HIRE_COL = 6  # столбец F


def add_hire_date():
    ws = _open().worksheet(EMP_SHEET)
    grid = ws.get_all_values()

    # Шапка: ставим заголовок F1, если пусто
    header = grid[0] if grid else []
    if len(header) < HIRE_COL or not header[HIRE_COL - 1].strip():
        ws.update_cell(1, HIRE_COL, "Дата приёма")

    updated = 0
    for i, r in enumerate(grid):
        if i == 0:
            continue  # шапка
        if len(r) < 2 or not r[1].strip():
            continue
        status = r[2].strip() if len(r) > 2 else ""
        existing = r[HIRE_COL - 1].strip() if len(r) > HIRE_COL - 1 else ""
        # только активным и только если дата приёма ещё не стоит
        if status == EMP_STATUS_ACTIVE and not existing:
            ws.update_cell(i + 1, HIRE_COL, HIRE_DATE_DEFAULT)
            updated += 1

    print(f"Дата приёма {HIRE_DATE_DEFAULT} проставлена: {updated} активным.")
    return updated


if __name__ == "__main__":
    add_hire_date()
