import io
import zipfile

import os

import pytest
from sqlalchemy.orm import Session

from ingestor.db import get_engine, init_db


@pytest.fixture()
def session(tmp_path):
    """Sessão de teste.

    Por padrão usa SQLite (com foreign keys ativadas, como no PostgreSQL). Com
    TEST_DATABASE_URL definido, roda a mesma suíte contra PostgreSQL — é assim
    que o caminho de produção (ON CONFLICT, ordem de INSERT, FKs) é exercido.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    engine = get_engine(url or f"sqlite:///{tmp_path}/test.db")
    if url:
        from ingestor.models import Base

        Base.metadata.drop_all(engine)
    init_db(engine)
    with Session(engine) as sess:
        yield sess
        sess.rollback()


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeFetch:
    """fetch(url) servindo bytes pré-definidos por trecho da URL."""

    def __init__(self, responses: dict[str, bytes]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        for token, data in self.responses.items():
            if token in url:
                return data
        raise AssertionError(f"URL inesperada no teste: {url}")
