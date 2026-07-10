"""Modelo de dados da Fase 1: fundos, informes mensais, métricas e estado de sync.

As tabelas de documentos (FNET) entram na Fase 2.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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


class SyncState(Base):
    __tablename__ = "sync_state"

    fonte: Mapped[str] = mapped_column(String(40), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(String(80))
    atualizado_em: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
