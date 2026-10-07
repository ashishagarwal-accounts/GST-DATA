"""One-time MySQL setup: creates the alcove_gst database and a dedicated gst_app user,
writes DATABASE_URL to .env, and creates the tables.

Run from the project folder:  .venv\\Scripts\\python scripts\\setup_mysql.py
You will be asked for the MySQL root password (it is not stored anywhere).
"""
import getpass
import os
import re
import secrets
import sys
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent
DB_NAME = "alcove_gst"
TEST_DB_NAME = "alcove_gst_test"  # scratch database for scripts/smoke_test.py; wiped on every run
APP_USER = "gst_app"


def main() -> None:
    # Non-interactive use: set MYSQL_ROOT_PASSWORD (and optionally MYSQL_HOST / MYSQL_PORT) for this run only.
    if os.environ.get("MYSQL_ROOT_PASSWORD") is not None:
        host = os.environ.get("MYSQL_HOST", "localhost")
        port = int(os.environ.get("MYSQL_PORT", "3306"))
        root_password = os.environ["MYSQL_ROOT_PASSWORD"]
    else:
        host = input("MySQL host [localhost]: ").strip() or "localhost"
        port = int(input("MySQL port [3306]: ").strip() or 3306)
        root_password = getpass.getpass("MySQL root password: ")
    app_password = secrets.token_urlsafe(24)

    try:
        conn = pymysql.connect(host=host, port=port, user="root", password=root_password, autocommit=True)
    except pymysql.err.OperationalError as exc:
        sys.exit(f"Could not connect as root: {exc}")
    with conn, conn.cursor() as cur:
        cur.execute(f"CREATE USER IF NOT EXISTS '{APP_USER}'@'localhost' IDENTIFIED BY %s", (app_password,))
        cur.execute(f"ALTER USER '{APP_USER}'@'localhost' IDENTIFIED BY %s", (app_password,))
        for db in (DB_NAME, TEST_DB_NAME):
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {db} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            cur.execute(f"GRANT ALL PRIVILEGES ON {db}.* TO '{APP_USER}'@'localhost'")
    print(f"Databases '{DB_NAME}', '{TEST_DB_NAME}' and user '{APP_USER}' ready.")

    base = f"mysql+pymysql://{APP_USER}:{app_password}@{host}:{port}"
    url = f"{base}/{DB_NAME}?charset=utf8mb4"
    env_path = ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    lines = [line for line in lines if not re.match(r"^\s*(TEST_)?DATABASE_URL\s*=", line)]
    lines.append(f"DATABASE_URL={url}")
    lines.append(f"TEST_DATABASE_URL={base}/{TEST_DB_NAME}?charset=utf8mb4")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote DATABASE_URL to {env_path}")

    sys.path.insert(0, str(ROOT))
    from app import migrate

    migrate.upgrade(url)
    print("Tables created (migrations applied). Start the app with:  .venv\\Scripts\\uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
