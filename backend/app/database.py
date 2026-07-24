import os
from sqlalchemy import event
from sqlmodel import SQLModel, create_engine, Session

# 確保資料庫儲存目錄存在
db_dir = "data"
os.makedirs(db_dir, exist_ok=True)

sqlite_file_name = os.path.join(db_dir, "ebooks.db")
sqlite_url = f"sqlite:///{sqlite_file_name}"

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

def init_db():
    """初始化資料庫並建立所有定義的資料表"""
    # 匯入 models 確保 SQLModel 註冊所有 Table
    from app import models
    SQLModel.metadata.create_all(engine)

def get_session():
    """提供 FastAPI Dependency Injection 使用的 Session 產生器"""
    with Session(engine) as session:
        yield session

# 保持相容性，使舊寫法也指向同一個 generator
get_db_session = get_session