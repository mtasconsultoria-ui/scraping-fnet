from __future__ import annotations

import os
from typing import Iterable, Sequence

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import Base

DEFAULT_URL = "sqlite:///fnet.db"

# Limite conservador de parâmetros por statement (sqlite antigo: 999)
_CHUNK_ROWS = 200


def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or os.environ.get("DATABASE_URL", DEFAULT_URL))


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def bulk_upsert(
    session: Session,
    model,
    rows: Sequence[dict],
    key_cols: Iterable[str],
    update_cols: Iterable[str],
) -> None:
    """Upsert em lote com ON CONFLICT, funcionando em SQLite e PostgreSQL."""
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - dialetos não suportados
        raise NotImplementedError(f"bulk_upsert não suporta dialeto {dialect}")

    key_cols = list(key_cols)
    update_cols = list(update_cols)
    for start in range(0, len(rows), _CHUNK_ROWS):
        chunk = rows[start : start + _CHUNK_ROWS]
        stmt = insert(model).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=key_cols,
            set_={c: getattr(stmt.excluded, c) for c in update_cols},
        )
        session.execute(stmt)
