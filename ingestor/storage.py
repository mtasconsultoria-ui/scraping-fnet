"""Armazenamento dos arquivos baixados (PDFs/XMLs do FNET).

Selecionado por STORAGE_URL:
- caminho local ou file://...  -> LocalStorage (default ./storage)
- s3://bucket[/prefixo]        -> S3Storage (S3, R2, Supabase Storage via protocolo
                                  S3; requer `pip install -e .[s3]` e credenciais
                                  AWS_* padrão; S3_ENDPOINT_URL para não-AWS)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> str:
        """Grava e retorna a URL/caminho persistido em documentos.url_storage."""
        ...


class LocalStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, key: str, data: bytes) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path.resolve())


class S3Storage:
    def __init__(self, bucket: str, prefix: str = "") -> None:
        import boto3  # dependência opcional (.[s3])

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL"))

    def put(self, key: str, data: bytes) -> str:
        full_key = f"{self.prefix}/{key}" if self.prefix else key
        self._client.put_object(Bucket=self.bucket, Key=full_key, Body=data)
        return f"s3://{self.bucket}/{full_key}"


def storage_from_env() -> Storage:
    url = os.environ.get("STORAGE_URL", "./storage")
    if url.startswith("s3://"):
        bucket, _, prefix = url[len("s3://") :].partition("/")
        return S3Storage(bucket, prefix)
    if url.startswith("file://"):
        url = url[len("file://") :]
    return LocalStorage(url)
