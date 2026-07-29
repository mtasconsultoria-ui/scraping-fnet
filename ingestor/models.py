"""Modelo de dados: fundos, informes mensais, métricas, documentos FNET e sync.

Fase 1: fundos / informes_mensais / fundos_metricas / sync_state.
Fase 2: documentos / documento (metadados e arquivos do FNET) e dominios.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Fundo(Base):
    __tablename__ = "fundos"

    # CNPJ com 14 dígitos, sem pontuação
    cnpj: Mapped[str] = mapped_column(String(14), primary_key=True)
    denominacao: Mapped[str | None] = mapped_column(Text)
    # FII, FIDC, FIAGRO, FIP, FIF, ... (ver cvm_bulk.map_tipo_veiculo)
    tipo_veiculo: Mapped[str | None] = mapped_column(String(20), index=True)
    publico_alvo: Mapped[str | None] = mapped_column(String(60), index=True)
    situacao: Mapped[str | None] = mapped_column(String(60), index=True)
    administrador: Mapped[str | None] = mapped_column(Text)
    gestor: Mapped[str | None] = mapped_column(Text)
    segmento: Mapped[str | None] = mapped_column(String(80))
    classe_cvm: Mapped[str | None] = mapped_column(Text)
    dt_registro: Mapped[dt.date | None] = mapped_column(Date)
    atualizado_em: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Os relationships não são usados para navegar (as consultas são explícitas),
    # mas sem eles o ORM desconhece a dependência entre as entidades e emite os
    # INSERTs fora de ordem — o que o PostgreSQL rejeita por violação de FK.
    informes: Mapped[list["InformeMensal"]] = relationship(back_populates="fundo")
    metricas: Mapped["FundoMetricas | None"] = relationship(back_populates="fundo")
    documentos: Mapped[list["Documento"]] = relationship(back_populates="fundo")


class InformeMensal(Base):
    __tablename__ = "informes_mensais"
    __table_args__ = (UniqueConstraint("cnpj", "competencia", name="uq_informe_cnpj_competencia"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cnpj: Mapped[str] = mapped_column(ForeignKey("fundos.cnpj"), index=True)
    # Primeiro dia do mês de competência
    competencia: Mapped[dt.date] = mapped_column(Date, index=True)
    versao: Mapped[int | None] = mapped_column(Integer)
    pl: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    num_cotistas: Mapped[int | None] = mapped_column(Integer)
    valor_cota: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    # ex.: fii_inf_mensal, fidc_inf_mensal
    fonte: Mapped[str] = mapped_column(String(30))

    fundo: Mapped["Fundo"] = relationship(back_populates="informes")


class FundoMetricas(Base):
    """Derivados recalculados após cada sync (consulta rápida na API)."""

    __tablename__ = "fundos_metricas"

    cnpj: Mapped[str] = mapped_column(ForeignKey("fundos.cnpj"), primary_key=True)
    competencia_ultima: Mapped[dt.date | None] = mapped_column(Date)
    pl_atual: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    pl_medio_12m: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    cotistas_atual: Mapped[int | None] = mapped_column(Integer)
    atualizado_em: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    fundo: Mapped["Fundo"] = relationship(back_populates="metricas")


class Documento(Base):
    """Metadados de documentos do FNET; o arquivo em si vai para o object storage."""

    __tablename__ = "documentos"

    # id do documento no FNET (downloadDocumento?id=...)
    id_fnet: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    cnpj: Mapped[str | None] = mapped_column(ForeignKey("fundos.cnpj"), index=True)
    denominacao_fundo: Mapped[str | None] = mapped_column(Text)
    categoria: Mapped[str | None] = mapped_column(String(120), index=True)
    tipo: Mapped[str | None] = mapped_column(String(120))
    especie: Mapped[str | None] = mapped_column(String(120))
    # dataReferencia do FNET varia de formato ("05/2026", "10/07/2026"); guardamos
    # a string original e a competência normalizada quando parseável
    data_referencia: Mapped[str | None] = mapped_column(String(20))
    competencia: Mapped[dt.date | None] = mapped_column(Date, index=True)
    data_entrega: Mapped[dt.datetime | None] = mapped_column(DateTime, index=True)
    situacao: Mapped[str | None] = mapped_column(String(20))
    versao: Mapped[int | None] = mapped_column(Integer)
    modalidade: Mapped[str | None] = mapped_column(String(20))
    # pendente | baixado | erro
    status_download: Mapped[str] = mapped_column(String(20), default="pendente", index=True)
    url_storage: Mapped[str | None] = mapped_column(Text)
    formato: Mapped[str | None] = mapped_column(String(10))
    baixado_em: Mapped[dt.datetime | None] = mapped_column(DateTime)
    # resposta original do FNET, para diagnóstico de mudanças de layout
    raw_json: Mapped[str | None] = mapped_column(Text)

    fundo: Mapped["Fundo | None"] = relationship(back_populates="documentos")
    texto_extraido: Mapped["DocumentoTexto | None"] = relationship(back_populates="documento")


class DocumentoTexto(Base):
    """Texto extraído de um documento, para busca por conteúdo.

    `texto_norm` é `textos.fold(texto)`: mesmo comprimento, minúsculo e sem
    acento — é sobre ele que a busca roda, e os offsets valem para os dois.
    """

    __tablename__ = "documento_textos"

    id_fnet: Mapped[int] = mapped_column(
        ForeignKey("documentos.id_fnet"), primary_key=True, autoincrement=False
    )
    texto: Mapped[str] = mapped_column(Text)
    texto_norm: Mapped[str] = mapped_column(Text)
    num_paginas: Mapped[int | None] = mapped_column(Integer)
    num_caracteres: Mapped[int] = mapped_column(Integer, default=0)
    # pdf | xml | texto | ocr | vazio (vazio = PDF sem camada de texto, fila de OCR)
    origem: Mapped[str] = mapped_column(String(20), index=True)
    extraido_em: Mapped[dt.datetime | None] = mapped_column(DateTime)

    documento: Mapped["Documento"] = relationship(back_populates="texto_extraido")


class Dominio(Base):
    """Tabelas de domínio raspadas dos filtros do FNET (tipos de fundo, categorias...)."""

    __tablename__ = "dominios"

    grupo: Mapped[str] = mapped_column(String(60), primary_key=True)
    id_fnet: Mapped[str] = mapped_column(String(20), primary_key=True)
    rotulo: Mapped[str] = mapped_column(Text)


class SyncState(Base):
    __tablename__ = "sync_state"

    fonte: Mapped[str] = mapped_column(String(40), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(String(80))
    atualizado_em: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
