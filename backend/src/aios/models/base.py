"""The declarative base every platform model shares, so its metadata sees every table."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
