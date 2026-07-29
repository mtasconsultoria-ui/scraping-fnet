"""Consulta combinando filtros estruturados e busca por conteúdo de documentos.

É a camada que a API da Fase 4 serializa. A busca textual roda em dois passos:

1. prefiltro no SQL (`texto_norm LIKE '%token%'`) — reduz o universo e usa o
   índice trigram no Postgres;
2. confirmação em Python com regex tolerante a separadores, que elimina os
   falsos positivos do LIKE e recorta os trechos para exibição.

O passo 2 é o que distingue "CPR-F" de um "CPR" solto no meio do regulamento.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import Documento, DocumentoTexto, Fundo, FundoMetricas
from .textos import Trecho, encontrar_trechos, termo_regex, token_prefiltro

log = logging.getLogger(__name__)

CATEGORIAS_REGULAMENTO = ["Regulamento"]
# Quantos candidatos buscar por fundo pedido, já que o regex descarta parte deles
FATOR_CANDIDATOS = 8


@dataclass
class Filtros:
    tipos_veiculo: list[str] = field(default_factory=list)
    publico_alvo: list[str] = field(default_factory=list)
    situacao: str | None = None
    pl_min: Decimal | None = None
    pl_max: Decimal | None = None
    cotistas_min: int | None = None
    cotistas_max: int | None = None
    termos: list[str] = field(default_factory=list)
    # exige todos os termos no mesmo documento; padrão é qualquer um (sinônimos)
    exigir_todos_termos: bool = False
    # descarta documentos em que todo trecho encontrado é uma vedação
    # ("vedada a aquisição de CPR-F") — útil para "permite expressamente"
    excluir_vedacoes: bool = False
    # busca literal, sem tolerar plural nem troca de conectores
    literal: bool = False
    categorias_documento: list[str] = field(default_factory=lambda: list(CATEGORIAS_REGULAMENTO))
    apenas_vigente: bool = True
    max_trechos: int = 3
    limite: int = 50
    offset: int = 0


@dataclass
class DocumentoMatch:
    id_fnet: int
    categoria: str | None
    tipo: str | None
    data_referencia: str | None
    data_entrega: object | None
    formato: str | None
    url_storage: str | None
    trechos: list[Trecho]


@dataclass
class FundoResultado:
    cnpj: str
    denominacao: str | None
    tipo_veiculo: str | None
    publico_alvo: str | None
    situacao: str | None
    administrador: str | None
    gestor: str | None
    segmento: str | None
    pl_atual: Decimal | None
    pl_medio_12m: Decimal | None
    cotistas_atual: int | None
    competencia_ultima: object | None
    documentos: list[DocumentoMatch] = field(default_factory=list)


@dataclass
class Resultado:
    fundos: list[FundoResultado]
    total_aproximado: int
    truncado: bool = False


def _aplica_filtros_fundo(stmt, filtros: Filtros):
    if filtros.tipos_veiculo:
        stmt = stmt.where(Fundo.tipo_veiculo.in_(filtros.tipos_veiculo))
    if filtros.situacao:
        stmt = stmt.where(func.lower(Fundo.situacao) == filtros.situacao.lower())
    if filtros.publico_alvo:
        # os rótulos da CVM variam ("Investidor Qualificado", "Investidores
        # Qualificados"); casa por substring em minúsculas
        stmt = stmt.where(
            or_(
                *[
                    func.lower(Fundo.publico_alvo).like(f"%{termo.lower()}%")
                    for termo in filtros.publico_alvo
                ]
            )
        )
    if filtros.pl_min is not None:
        stmt = stmt.where(FundoMetricas.pl_medio_12m >= filtros.pl_min)
    if filtros.pl_max is not None:
        stmt = stmt.where(FundoMetricas.pl_medio_12m <= filtros.pl_max)
    if filtros.cotistas_min is not None:
        stmt = stmt.where(FundoMetricas.cotistas_atual >= filtros.cotistas_min)
    if filtros.cotistas_max is not None:
        stmt = stmt.where(FundoMetricas.cotistas_atual <= filtros.cotistas_max)
    return stmt


def _vigente_subquery():
    """Só o documento mais recente de cada (cnpj, categoria)."""
    doc2 = Documento.__table__.alias("doc_vigente")
    return (
        select(doc2.c.id_fnet)
        .where(doc2.c.cnpj == Documento.cnpj, doc2.c.categoria == Documento.categoria)
        .order_by(doc2.c.data_entrega.desc(), doc2.c.id_fnet.desc())
        .limit(1)
        .scalar_subquery()
    )


def buscar(session: Session, filtros: Filtros) -> Resultado:
    if filtros.termos:
        return _buscar_com_texto(session, filtros)
    return _buscar_sem_texto(session, filtros)


def _buscar_sem_texto(session: Session, filtros: Filtros) -> Resultado:
    stmt = (
        select(Fundo, FundoMetricas)
        .outerjoin(FundoMetricas, FundoMetricas.cnpj == Fundo.cnpj)
        .order_by(FundoMetricas.pl_medio_12m.desc().nullslast(), Fundo.cnpj)
    )
    stmt = _aplica_filtros_fundo(stmt, filtros)
    total = session.execute(
        _aplica_filtros_fundo(
            select(func.count())
            .select_from(Fundo)
            .outerjoin(FundoMetricas, FundoMetricas.cnpj == Fundo.cnpj),
            filtros,
        )
    ).scalar_one()

    linhas = session.execute(stmt.offset(filtros.offset).limit(filtros.limite)).all()
    return Resultado(
        fundos=[_monta_fundo(fundo, metricas) for fundo, metricas in linhas],
        total_aproximado=total,
    )


def _buscar_com_texto(session: Session, filtros: Filtros) -> Resultado:
    stmt = (
        select(Fundo, FundoMetricas, Documento, DocumentoTexto)
        .join(Documento, Documento.cnpj == Fundo.cnpj)
        .join(DocumentoTexto, DocumentoTexto.id_fnet == Documento.id_fnet)
        .outerjoin(FundoMetricas, FundoMetricas.cnpj == Fundo.cnpj)
        .order_by(FundoMetricas.pl_medio_12m.desc().nullslast(), Fundo.cnpj)
    )
    stmt = _aplica_filtros_fundo(stmt, filtros)
    if filtros.categorias_documento:
        stmt = stmt.where(Documento.categoria.in_(filtros.categorias_documento))
    if filtros.apenas_vigente:
        stmt = stmt.where(Documento.id_fnet == _vigente_subquery())

    # prefiltro: um LIKE por termo, unidos conforme o modo
    condicoes = [
        DocumentoTexto.texto_norm.like(f"%{token_prefiltro(termo)}%") for termo in filtros.termos
    ]
    if filtros.exigir_todos_termos:
        for condicao in condicoes:
            stmt = stmt.where(condicao)
    else:
        stmt = stmt.where(or_(*condicoes))

    # confirmação por regex descarta parte dos candidatos: busca com folga
    teto = (filtros.offset + filtros.limite) * FATOR_CANDIDATOS
    candidatos = session.execute(stmt.limit(teto)).all()

    regexes = {
        termo: termo_regex(termo, flexivel=not filtros.literal) for termo in filtros.termos
    }
    por_fundo: dict[str, FundoResultado] = {}
    for fundo, metricas, documento, texto in candidatos:
        casados = [t for t, rx in regexes.items() if rx.search(texto.texto_norm)]
        if not casados:
            continue
        if filtros.exigir_todos_termos and len(casados) != len(filtros.termos):
            continue
        trechos = encontrar_trechos(
            texto.texto,
            texto.texto_norm,
            casados,
            max_trechos=filtros.max_trechos,
            flexivel=not filtros.literal,
        )
        if filtros.excluir_vedacoes and trechos and all(t.possivel_vedacao for t in trechos):
            continue
        resultado = por_fundo.get(fundo.cnpj)
        if resultado is None:
            resultado = _monta_fundo(fundo, metricas)
            por_fundo[fundo.cnpj] = resultado
        resultado.documentos.append(
            DocumentoMatch(
                id_fnet=documento.id_fnet,
                categoria=documento.categoria,
                tipo=documento.tipo,
                data_referencia=documento.data_referencia,
                data_entrega=documento.data_entrega,
                formato=documento.formato,
                url_storage=documento.url_storage,
                trechos=trechos,
            )
        )

    fundos = list(por_fundo.values())
    pagina = fundos[filtros.offset : filtros.offset + filtros.limite]
    return Resultado(
        fundos=pagina,
        total_aproximado=len(fundos),
        truncado=len(candidatos) >= teto,
    )


def _monta_fundo(fundo: Fundo, metricas: FundoMetricas | None) -> FundoResultado:
    return FundoResultado(
        cnpj=fundo.cnpj,
        denominacao=fundo.denominacao,
        tipo_veiculo=fundo.tipo_veiculo,
        publico_alvo=fundo.publico_alvo,
        situacao=fundo.situacao,
        administrador=fundo.administrador,
        gestor=fundo.gestor,
        segmento=fundo.segmento,
        pl_atual=metricas.pl_atual if metricas else None,
        pl_medio_12m=metricas.pl_medio_12m if metricas else None,
        cotistas_atual=metricas.cotistas_atual if metricas else None,
        competencia_ultima=metricas.competencia_ultima if metricas else None,
    )
