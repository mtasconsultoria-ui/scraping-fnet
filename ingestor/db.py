from __future__ import annotations

import logging
import os
from typing import Iterable, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import Base

log = logging.getLogger(__name__)

DEFAULT_URL = "sqlite:///fnet.db"

# Limite conservador de parâmetros por statement (sqlite antigo: 999)
_CHUNK_ROWS = 200

# Acelera o LIKE '%termo%' da busca textual; sem ele a busca funciona igual,
# só varre mais. Exige a extensão pg_trgm (pode faltar permissão no provedor).
_PG_TRGM_DDL = (
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX IF NOT EXISTS ix_documento_textos_norm_trgm "
    "ON documento_textos USING gin (texto_norm gin_trgm_ops)",
)


def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or os.environ.get("DATABASE_URL", DEFAULT_URL))


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        _create_pg_trgm_index(engine)


def _create_pg_trgm_index(engine: Engine) -> None:
    for statement in _PG_TRGM_DDL:
        try:
            with engine.begin() as conn:
                conn.execute(text(statement))
        except Exception as exc:  # índice é otimização, não requisito
            log.warning("índice trigram não criado (%s): %s", statement.split()[1], exc)
            return


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
