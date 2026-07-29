import datetime as dt
from decimal import Decimal

from ingestor.parsing import (
    CsvTable,
    norm_cnpj,
    normalize_col,
    parse_competencia,
    parse_date,
    parse_decimal,
    parse_int,
)


def test_normalize_col():
    assert normalize_col("Público-Alvo ") == "PUBLICO_ALVO"
    assert normalize_col("CNPJ_Fundo") == "CNPJ_FUNDO"
    assert normalize_col("Segmento de Atuação") == "SEGMENTO_DE_ATUACAO"


def test_norm_cnpj():
    assert norm_cnpj("11.026.627/0001-38") == "11026627000138"
    assert norm_cnpj("1026627000138") == "01026627000138"
    assert norm_cnpj("") is None
    assert norm_cnpj(None) is None


def test_cnpj_valido():
    from ingestor.parsing import cnpj_valido

    assert cnpj_valido("11222333000181")  # exemplo canônico válido
    assert cnpj_valido("11444777000161")
    assert cnpj_valido("11111111000191")  # CNPJ clássico de teste, DV correto
    assert not cnpj_valido("33333333000153")  # DV errado
    assert not cnpj_valido("20260729000123")  # protocolo com cara de CNPJ
    assert not cnpj_valido("11111111111111")  # sequência repetida
    assert not cnpj_valido("123")
    assert not cnpj_valido("1122233300018a")


def test_parse_decimal_formats():
    assert parse_decimal("1234.56") == Decimal("1234.56")
    assert parse_decimal("1.234,56") == Decimal("1234.56")
    assert parse_decimal("1234,56") == Decimal("1234.56")
    assert parse_decimal("") is None
    assert parse_decimal("N/D") is None


def test_parse_int():
    assert parse_int("42") == 42
    assert parse_int("42.0") == 42
    assert parse_int(None) is None


def test_parse_competencia():
    assert parse_competencia("2026-05-31") == dt.date(2026, 5, 1)
    assert parse_competencia("2026-05") == dt.date(2026, 5, 1)
    assert parse_competencia("05/2026") == dt.date(2026, 5, 1)
    assert parse_competencia("banana") is None


def test_parse_date():
    assert parse_date("2020-01-15") == dt.date(2020, 1, 15)
    assert parse_date("15/01/2020") == dt.date(2020, 1, 15)


def test_csv_table_latin1_and_lookup():
    data = "CNPJ_Fundo;Público_Alvo\n123;Investidores Qualificados\n".encode("latin-1")
    table = CsvTable(data, "x.csv")
    assert table.index_of("CNPJ_FUNDO", "CNPJ_FUNDO_CLASSE") == 0
    assert table.index_of("PUBLICO_ALVO") == 1
    assert len(table.rows) == 1


def test_csv_table_utf8_bom():
    data = "﻿CNPJ_FUNDO;SIT\n123;ATIVO\n".encode("utf-8")
    table = CsvTable(data, "x.csv")
    assert table.index_of("CNPJ_FUNDO") == 0
