from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from urllib.parse import quote_plus
from config import *

# Encode password in case it contains special characters
encoded_password = quote_plus(MYSQL_PASSWORD)

DATABASE_URL = (
    f"mysql+pymysql://"
    f"{MYSQL_USER}:"
    f"{encoded_password}@"
    f"{MYSQL_HOST}:"
    f"{MYSQL_PORT}/"
    f"{MYSQL_DB}"
)

print("Connecting to TiDB Cloud...")
print("Host:", MYSQL_HOST)
print("Port:", MYSQL_PORT)
print("Database:", MYSQL_DB)
print("User:", MYSQL_USER)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_reset_on_return="rollback",
    echo=False,
    connect_args={
        "ssl_ca": MYSQL_SSL_CA,
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    },
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
