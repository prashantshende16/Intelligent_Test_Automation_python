import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://postgres:postgres@localhost:5432/testing_automation"
)

# Robust database engine creation with fallback to SQLite
try:
    if DATABASE_URL.startswith("postgresql"):
        # Test connection quickly
        test_engine = create_engine(DATABASE_URL, connect_args={"connect_timeout": 2})
        with test_engine.connect() as conn:
            pass
        engine = create_engine(
            DATABASE_URL,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True
        )
    else:
        engine = create_engine(
            DATABASE_URL,
            connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
        )
except Exception:
    # Fallback to SQLite in development
    DATABASE_URL = "sqlite:///./testing_automation.db"
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False}
    )

from sqlalchemy.event import listen
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA temp_store = MEMORY")
        cursor.execute("PRAGMA journal_mode = WAL")
    except Exception:
        pass
    finally:
        cursor.close()

listen(engine, 'connect', set_sqlite_pragma)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
