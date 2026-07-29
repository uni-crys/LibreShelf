import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlmodel import create_engine, Session

from app.config import BACKEND_DIR, settings

database_path = Path(settings.LIBROVIA_DATABASE_PATH).expanduser().resolve()
database_path.parent.mkdir(parents=True, exist_ok=True)
database_path.touch(mode=0o600, exist_ok=True)
os.chmod(database_path, 0o600)
sqlite_url = f"sqlite:///{database_path}"

# 建立 DB 連線 Engine (timeout 設為 30 秒以應對頻繁寫入)
connect_args = {"check_same_thread": False, "timeout": 30}
engine = create_engine(sqlite_url, connect_args=connect_args, echo=False)

# ----------------------------------------------------------------------
# 自動啟用 WAL (Write-Ahead Logging) 模式
# 每當與 SQLite 建立連線時自動執行，有效解決 database is locked 併發鎖檔問題
# ----------------------------------------------------------------------
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()

def run_migrations(database_url: str = sqlite_url):
    """Apply all committed schema migrations."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


def init_db():
    run_migrations()

def get_session():
    """提供 FastAPI Dependency Injection 使用的 Session 產生器"""
    with Session(engine) as session:
        yield session

# 保持相容性，使舊寫法也指向同一個 generator
get_db_session = get_session
