from sqlalchemy import BigInteger, any_, literal
from sqlalchemy.dialects.postgresql import ARRAY


def id_in(column, ids: list[int]):
    """`column = ANY(:ids)` with one array parameter; IN (...) breaks past asyncpg's 32767-parameter limit."""
    return column == any_(literal(list(ids), ARRAY(BigInteger)))
