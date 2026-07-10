import io
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ingestor.db import init_db


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    init_db(engine)
    with Session(engine) as sess:
        yield sess


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
