"""Sincronização de documentos do FNET: metadados incrementais + download priorizado."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import bulk_upsert
from .fnet_client import FnetClient, detect_ext
from .models import Documento, Dominio, Fundo, SyncState
from .parsing import norm_cnpj, parse_competencia
from .storage import Storage

log = logging.getLogger(__name__)

# âncoras de dígito: não casar o miolo de um protocolo/processo de 16+ dígitos
_CNPJ_RE = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")


def _as_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_data_entrega(value) -> dt.datetime | None:
    text = _as_str(value)
    if not text:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_competencia_ref(value) -> dt.date | None:
    """dataReferencia do FNET -> primeiro dia do mês.

    Formatos observados em produção: '05/2026', '10/07/2026' e
    '29/07/2026 13:00' (com hora, em atas de assembleia).
    """
    text = _as_str(value)
    if not text:
        return None
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})", text)
    if match:
        return dt.date(int(match.group(3)), int(match.group(2)), 1)
    return parse_competencia(text)


def _doc_to_row(doc: dict) -> dict | None:
    doc_id = doc.get("id")
    if doc_id is None:
        return None
    descricao = _as_str(doc.get("descricaoFundo"))
    cnpj = norm_cnpj(_as_str(doc.get("cnpjFundo")))
    if not cnpj and descricao:
        match = _CNPJ_RE.search(descricao)
        cnpj = norm_cnpj(match.group()) if match else None
    versao_raw = doc.get("versao")
    try:
        versao = int(versao_raw) if versao_raw is not None else None
    except (TypeError, ValueError):
        versao = None
    return {
        "id_fnet": int(doc_id),
        "cnpj": cnpj,
        "denominacao_fundo": descricao,
        "categoria": _as_str(doc.get("categoriaDocumento")),
        "tipo": _as_str(doc.get("tipoDocumento")),
        "especie": _as_str(doc.get("especieDocumento")),
        "data_referencia": _as_str(doc.get("dataReferencia")),
        "competencia": _parse_competencia_ref(doc.get("dataReferencia")),
        "data_entrega": _parse_data_entrega(doc.get("dataEntrega")),
        "situacao": _as_str(doc.get("situacaoDocumento")),
        "versao": versao,
        "modalidade": _as_str(doc.get("modalidade")),
        "status_download": "pendente",
        "raw_json": json.dumps(doc, ensure_ascii=False),
    }


def sync_documentos(
    session: Session,
    client: FnetClient,
    *,
    tipo_fundo: int | None = None,
    id_categoria: int | None = None,
    desde: dt.date | None = None,
    page_size: int = 100,
    max_pages: int | None = 2000,
) -> dict:
    """Upsert incremental de metadados, com cursor por dataEntrega.

    Sem `tipo_fundo`, varre um tipo por vez (ids lidos dos filtros do site):
    constatado em produção que a busca com tipoFundo vazio devolve apenas os
    documentos mais recentes (~centenas), não o acervo — FIDC sozinho tem
    centenas de milhares. Cada tipo mantém cursor próprio, e o progresso é
    commitado POR TIPO: falha num tipo não desfaz os anteriores.

    Sem `desde`, retoma do cursor persistido (com 1 dia de sobreposição — o
    filtro do FNET é por data, e o upsert absorve repetidos). O backfill do
    acervo é progressivo: quando `max_pages` trunca a varredura, o cursor fica
    no ponto alcançado e a execução seguinte continua dali.
    """
    tipos = [tipo_fundo] if tipo_fundo is not None else [
        tf for tf, _ in client.listar_tipos_fundo()
    ]
    total = 0
    truncados: list[int] = []
    falhas: list[str] = []
    for tf in tipos:
        try:
            quantos, truncado = _sync_documentos_tipo(
                session,
                client,
                tipo_fundo=tf,
                id_categoria=id_categoria,
                desde=desde,
                page_size=page_size,
                max_pages=max_pages,
            )
        except Exception as exc:
            # um tipo quebrado não pode impedir os demais nem descartar o que
            # os anteriores já commitaram; a falha é re-lançada no final para o
            # registro de execução ficar como erro
            session.rollback()
            log.error("sync do tipoFundo=%s falhou: %s", tf, exc)
            falhas.append(f"tipoFundo={tf}: {type(exc).__name__}: {exc}")
            continue
        session.commit()  # persiste documentos E cursor deste tipo desde já
        total += quantos
        if truncado:
            truncados.append(tf)
    if truncados:
        log.warning(
            "varredura truncada em max_pages para tipoFundo=%s; "
            "a próxima execução retoma do cursor",
            truncados,
        )
    if falhas:
        raise RuntimeError(f"sync falhou em {len(falhas)} tipo(s): " + " | ".join(falhas))
    return {"documentos": total, "tipos_truncados": truncados}


def _sync_documentos_tipo(
    session: Session,
    client: FnetClient,
    *,
    tipo_fundo: int,
    id_categoria: int | None,
    desde: dt.date | None,
    page_size: int,
    max_pages: int | None,
) -> tuple[int, bool]:
    fonte = f"fnet_documentos:tf={tipo_fundo}:cat={id_categoria or 'all'}"
    state = session.get(SyncState, fonte)
    if desde is None and state and state.cursor:
        desde = dt.datetime.fromisoformat(state.cursor).date() - dt.timedelta(days=1)
    elif desde is None:
        # bancos criados antes da varredura por tipo guardavam um cursor único
        # em tf=all; aproveitá-lo evita rebaixar o sync a um backfill do zero
        legado = session.get(
            SyncState, f"fnet_documentos:tf=all:cat={id_categoria or 'all'}"
        )
        if legado and legado.cursor:
            desde = dt.datetime.fromisoformat(legado.cursor).date() - dt.timedelta(days=1)

    rows: dict[int, dict] = {}
    for doc in client.iter_documents(
        page_size=page_size,
        max_pages=max_pages,
        tipo_fundo=tipo_fundo,
        id_categoria=id_categoria,
        data_inicial=desde.strftime("%d/%m/%Y") if desde else None,
    ):
        row = _doc_to_row(doc)
        if row:
            rows[row["id_fnet"]] = row

    if rows:
        _ensure_fundos(session, {r["cnpj"] for r in rows.values() if r["cnpj"]})
        # status_download/url_storage/formato/baixado_em ficam fora do update:
        # re-sync de metadados não pode desfazer downloads já realizados
        bulk_upsert(
            session,
            Documento,
            list(rows.values()),
            key_cols=["id_fnet"],
            update_cols=[
                "cnpj",
                "denominacao_fundo",
                "categoria",
                "tipo",
                "especie",
                "data_referencia",
                "competencia",
                "data_entrega",
                "situacao",
                "versao",
                "modalidade",
                "raw_json",
            ],
        )
        entregas = [r["data_entrega"] for r in rows.values() if r["data_entrega"]]
        if entregas:
            cursor = max(entregas)
            if state is None:
                state = SyncState(fonte=fonte)
                session.add(state)
            state.cursor = cursor.isoformat()
            state.atualizado_em = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    log.info("%s: %s documentos sincronizados", fonte, len(rows))
    truncado = max_pages is not None and len(rows) >= max_pages * page_size
    return len(rows), truncado


def download_documentos(
    session: Session,
    client: FnetClient,
    storage: Storage,
    *,
    categorias: list[str] | None = None,
    limite: int = 100,
) -> dict:
    """Baixa documentos pendentes (mais recentes primeiro) para o storage."""
    stmt = (
        select(Documento)
        .where(Documento.status_download == "pendente")
        .order_by(Documento.data_entrega.desc())
        .limit(limite)
    )
    if categorias:
        stmt = stmt.where(Documento.categoria.in_(categorias))

    baixados = erros = 0
    for doc in session.execute(stmt).scalars():
        try:
            data = client.download_document(doc.id_fnet)
            ext = detect_ext(data)
            doc.url_storage = storage.put(f"fnet/{doc.id_fnet}.{ext}", data)
            doc.formato = ext
            doc.status_download = "baixado"
            doc.baixado_em = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            baixados += 1
        except Exception as exc:
            log.error("download do documento %s falhou: %s", doc.id_fnet, exc)
            doc.status_download = "erro"
            erros += 1
        session.flush()

    log.info("download: %s baixados, %s erros", baixados, erros)
    return {"baixados": baixados, "erros": erros}


def sync_dominios(session: Session, client: FnetClient) -> dict:
    grupos = client.fetch_domains()
    rows = [
        {"grupo": grupo, "id_fnet": valor, "rotulo": rotulo}
        for grupo, opcoes in grupos.items()
        for valor, rotulo in opcoes
    ]
    bulk_upsert(session, Dominio, rows, key_cols=["grupo", "id_fnet"], update_cols=["rotulo"])
    log.info("dominios: %s opções em %s grupos", len(rows), len(grupos))
    return {"grupos": len(grupos), "opcoes": len(rows)}


def _ensure_fundos(session: Session, cnpjs: set[str]) -> None:
    if not cnpjs:
        return
    existentes = set(session.execute(select(Fundo.cnpj).where(Fundo.cnpj.in_(cnpjs))).scalars())
    novos = [{"cnpj": cnpj} for cnpj in sorted(cnpjs - existentes)]
    if novos:
        bulk_upsert(session, Fundo, novos, key_cols=["cnpj"], update_cols=["denominacao"])
