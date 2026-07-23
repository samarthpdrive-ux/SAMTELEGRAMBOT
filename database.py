from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from urllib.parse import quote_plus
from config import *

encoded_password = quote_plus(MYSQL_PASSWORD)

DATABASE_URL = (
    f"mysql+pymysql://"
    f"{MYSQL_USER}:"
    f"{encoded_password}@"
    f"{MYSQL_HOST}:"
    f"{MYSQL_PORT}/"
    f"{MYSQL_DB}"
)

print("Database Host:", MYSQL_HOST)
print("Database Port:", MYSQL_PORT)
print("Database Name:", MYSQL_DB)
print("Database User:", MYSQL_USER)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=False,
    connect_args={
        "ssl": {}
    }
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False
)

Base = declarative_base()
