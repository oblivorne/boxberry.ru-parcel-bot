from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    DateTime,
    ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import os
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True)
    username = Column(
        String, unique=True, index=True, nullable=False
    )  # login yerine username, zorunlu alan
    password = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)

    parcels = relationship("Parcel", back_populates="user")


class Parcel(Base):
    __tablename__ = "parcels"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    tracking_number = Column(String, nullable=True)
    recipient_name = Column(String, nullable=True)
    recipient_surname = Column(String, nullable=True)
    last_status = Column(String, nullable=True)
    raw_json = Column(Text, nullable=True)

    user = relationship("User", back_populates="parcels")


def init_db():
    Base.metadata.create_all(bind=engine)


def recreate_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    recreate_db()
    print("Database recreated!")
