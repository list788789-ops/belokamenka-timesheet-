"""
Разовое создание листа «Пользователи» с первым админом.

Запуск через RUN_USERS=1 (в main.py) или локально.
ADMIN_CHAT_ID берётся из переменной окружения (по умолчанию — известный админ).
"""

import os

from sheets import _ensure_users_sheet, add_user, ROLE_ADMIN, ROLE_FOREMAN

# chat_id администратора (Валерий). Можно переопределить переменной ADMIN_CHAT_ID.
DEFAULT_ADMIN = 372951174


def setup_users():
    _ensure_users_sheet()
    admin_id = int(os.getenv("ADMIN_CHAT_ID", str(DEFAULT_ADMIN)))
    add_user(admin_id, "Админ", ROLE_ADMIN)

    # Второй известный пользователь (прораб), если задан
    second = os.getenv("SECOND_CHAT_ID")
    if second:
        add_user(int(second), "Прораб", ROLE_FOREMAN)

    print(f"Лист «Пользователи» создан. Админ: {admin_id}")
    return admin_id


if __name__ == "__main__":
    setup_users()
