from sqlalchemy import inspect

from app.database import Base


def test_schema_initialization(db) -> None:
    names = set(inspect(db.bind).get_table_names())
    assert set(Base.metadata.tables).issubset(names)
