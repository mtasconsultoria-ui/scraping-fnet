"""Helpers de parsing tolerantes aos layouts dos CSVs da CVM.

Os arquivos variam de encoding (latin-1 vs utf-8), de caixa nos nomes de coluna
e, pontualmente, de nome de coluna entre anos/resoluções (ex.: CVM 175 renomeou
CNPJ_FUNDO para CNPJ_FUNDO_CLASSE em alguns datasets). Tudo aqui é defensivo.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import unicodedata
from decimal import Decimal, InvalidOperation


class LayoutError(RuntimeError):
    """Colunas obrigatórias ausentes — o layout do arquivo mudou."""


def decode_csv_bytes(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def normalize_col(name: str) -> str:
    """'Público-Alvo ' -> 'PUBLICO_ALVO' (sem acento, maiúsculo, _ como separador)."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text.strip())
    return text.strip("_").upper()


class CsvTable:
    """CSV da CVM (separador ';') com acesso a colunas por nomes candidatos."""

    def __init__(self, data: bytes, filename: str = "") -> None:
        self.filename = filename
        reader = csv.reader(io.StringIO(decode_csv_bytes(data)), delimiter=";")
        try:
            header = next(reader)
        except StopIteration:
            header = []
        self.columns = {normalize_col(h): i for i, h in enumerate(header)}
        self.rows = [r for r in reader if any(cell.strip() for cell in r)]

    def index_of(self, *candidates: str) -> int | None:
        for cand in candidates:
            idx = self.columns.get(normalize_col(cand))
            if idx is not None:
                return idx
        return None

    def require(self, *candidates: str) -> int:
        idx = self.index_of(*candidates)
        if idx is None:
            raise LayoutError(
                f"{self.filename}: nenhuma das colunas {candidates} encontrada. "
                f"Colunas presentes: {sorted(self.columns)}"
            )
        return idx


def cell(row: list[str], idx: int | None) -> str | None:
    if idx is None or idx >= len(row):
        return None
    value = row[idx].strip()
    return value or None


def norm_cnpj(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    return digits.zfill(14)


def cnpj_valido(cnpj: str) -> bool:
    """Valida os dígitos verificadores de um CNPJ já normalizado (14 dígitos).

    Usado quando o CNPJ é extraído de texto livre, onde um número de protocolo
    ou processo pode ter cara de CNPJ; os DVs eliminam quase todos.
    """
    if len(cnpj) != 14 or not cnpj.isdigit() or cnpj == cnpj[0] * 14:
        return False

    def dv(digitos: str, pesos: list[int]) -> str:
        resto = sum(int(d) * p for d, p in zip(digitos, pesos)) % 11
        return "0" if resto < 2 else str(11 - resto)

    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6, *pesos1]
    return cnpj[12] == dv(cnpj[:12], pesos1) and cnpj[13] == dv(cnpj[:13], pesos2)


def parse_decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    text = value.strip()
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_int(value: str | None) -> int | None:
    number = parse_decimal(value)
    return int(number) if number is not None else None


def parse_competencia(value: str | None) -> dt.date | None:
    """'2026-05', '2026-05-31' ou '05/2026' -> date(2026, 5, 1)."""
    if not value:
        return None
    text = value.strip()
    match = re.match(r"^(\d{4})-(\d{2})(?:-\d{2})?", text)
    if match:
        return dt.date(int(match.group(1)), int(match.group(2)), 1)
    match = re.match(r"^(\d{2})/(\d{4})$", text)
    if match:
        return dt.date(int(match.group(2)), int(match.group(1)), 1)
    return None


def parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
