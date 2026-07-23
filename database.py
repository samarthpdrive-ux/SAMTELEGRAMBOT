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

print("Encoded Password:", encoded_password)
print("Database URL:", DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=False
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False  # IMPORTANT
)

Base = declarative_base()