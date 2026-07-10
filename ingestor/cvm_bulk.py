"""Carga em massa a partir do Portal de Dados Abertos da CVM (dados.cvm.gov.br).

Datasets da Fase 1:
- cadastro  : cad_fi.csv — dimensão `fundos` para todos os tipos de veículo.
- fii       : inf_mensal_fii_{ano}.zip — Informe Mensal Estruturado de FII
              (inclui FIAGRO-FII); alimenta `informes_mensais` e enriquece `fundos`.
- fidc      : inf_mensal_fidc_{aaaamm}.zip — EXPERIMENTAL: o layout exato das
              tabelas (tab_I..tab_X) precisa ser validado na primeira execução com
              rede; o loader varre as tabelas do zip procurando colunas candidatas
              de PL e cotistas e falha de forma descritiva se nada casar.

Todos os loaders são idempotentes (upsert por chave natural).
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import os
import zipfile
from collections import defaultdict
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import bulk_upsert
from .models import Fundo, InformeMensal, SyncState
from .parsing import (
    CsvTable,
    LayoutError,
    cell,
    norm_cnpj,
    parse_competencia,
    parse_date,
    parse_decimal,
    parse_int,
)

log = logging.getLogger(__name__)

FetchFn = Callable[[str], bytes]


def base_url() -> str:
    return os.environ.get("DADOS_CVM_BASE", "https://dados.cvm.gov.br/dados").rstrip("/")


def url_cadastro() -> str:
    return f"{base_url()}/FI/CAD/DADOS/cad_fi.csv"


def url_fii(ano: int) -> str:
    return f"{base_url()}/FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_{ano}.zip"


def url_fidc(competencia: str) -> str:
    return f"{base_url()}/FIDC/DOC/INF_MENSAL/DADOS/inf_mensal_fidc_{competencia}.zip"


def map_tipo_veiculo(tp_fundo: str | None, denominacao: str | None) -> str | None:
    """Normaliza TP_FUNDO do cadastro para o vocabulário usado nos filtros."""
    denom = (denominacao or "").upper()
    tp = (tp_fundo or "").upper().strip()
    if "FIAGRO" in tp or "FIAGRO" in denom:
        return "FIAGRO"
    if "FIDC" in tp:
        return "FIDC"
    if tp.startswith("FII"):
        return "FII"
    if tp.startswith("FIP") or tp == "FMIEE":
        return "FIP"
    if tp in {"FI", "FIF", "FICFI", "FIC"}:
        return "FIF"
    return tp or None


# ---------------------------------------------------------------------------
# Cadastro (cad_fi.csv)
# ---------------------------------------------------------------------------

_SIT_ATIVA = "EM FUNCIONAMENTO NORMAL"


def sync_cadastro(session: Session, fetch: FetchFn) -> dict:
    table = CsvTable(fetch(url_cadastro()), "cad_fi.csv")
    idx_cnpj = table.require("CNPJ_FUNDO", "CNPJ_FUNDO_CLASSE")
    idx_denom = table.require("DENOM_SOCIAL")
    idx_tp = table.index_of("TP_FUNDO", "TP_FUNDO_CLASSE")
    idx_sit = table.index_of("SIT")
    idx_admin = table.index_of("ADMIN")
    idx_gestor = table.index_of("GESTOR")
    idx_publico = table.index_of("PUBLICO_ALVO")
    idx_classe = table.index_of("CLASSE")
    idx_dt_reg = table.index_of("DT_REG")

    # O cadastro traz mais de um registro por CNPJ (reativações, classes);
    # fica o registro ativo e, entre iguais, o de registro mais recente.
    escolhidos: dict[str, tuple[tuple[int, dt.date], dict]] = {}
    for row in table.rows:
        cnpj = norm_cnpj(cell(row, idx_cnpj))
        if not cnpj:
            continue
        situacao = cell(row, idx_sit)
        dt_reg = parse_date(cell(row, idx_dt_reg))
        prioridade = (
            1 if (situacao or "").upper() == _SIT_ATIVA else 0,
            dt_reg or dt.date.min,
        )
        atual = escolhidos.get(cnpj)
        if atual and atual[0] >= prioridade:
            continue
        denominacao = cell(row, idx_denom)
        escolhidos[cnpj] = (
            prioridade,
            {
                "cnpj": cnpj,
                "denominacao": denominacao,
                "tipo_veiculo": map_tipo_veiculo(cell(row, idx_tp), denominacao),
                "situacao": situacao,
                "administrador": cell(row, idx_admin),
                "gestor": cell(row, idx_gestor),
                "publico_alvo": cell(row, idx_publico),
                "classe_cvm": cell(row, idx_classe),
                "dt_registro": dt_reg,
            },
        )

    rows = [dados for _, dados in escolhidos.values()]
    bulk_upsert(
        session,
        Fundo,
        rows,
        key_cols=["cnpj"],
        update_cols=[
            "denominacao",
            "tipo_veiculo",
            "situacao",
            "administrador",
            "gestor",
            "publico_alvo",
            "classe_cvm",
            "dt_registro",
        ],
    )
    _touch_sync_state(session, "cvm_cadastro", cursor=str(len(rows)))
    log.info("cadastro: %s fundos", len(rows))
    return {"fundos": len(rows)}


# ---------------------------------------------------------------------------
# FII — Informe Mensal Estruturado (zip anual: geral, complemento, ativo_passivo)
# ---------------------------------------------------------------------------


def sync_fii_informes(session: Session, fetch: FetchFn, anos: list[int]) -> dict:
    total = 0
    ultima_competencia: dt.date | None = None
    for ano in anos:
        archive = zipfile.ZipFile(io.BytesIO(fetch(url_fii(ano))))
        informes: dict[tuple[str, dt.date], dict] = {}
        fundos_attrs: dict[str, dict] = {}

        geral = _zip_table(archive, "geral")
        if geral is None:
            raise LayoutError(
                f"inf_mensal_fii_{ano}.zip: arquivo 'geral' não encontrado; "
                f"conteúdo: {archive.namelist()}"
            )
        _load_fii_geral(geral, informes, fundos_attrs)

        complemento = _zip_table(archive, "complemento")
        if complemento is not None:
            _load_fii_complemento(complemento, informes)

        ativo_passivo = _zip_table(archive, "ativo_passivo")
        if ativo_passivo is not None:
            _load_fii_ativo_passivo(ativo_passivo, informes)

        _upsert_informes(session, informes, fonte="fii_inf_mensal")
        _enrich_fundos(session, fundos_attrs, tipo_default="FII")
        total += len(informes)
        if informes:
            maior = max(comp for _, comp in informes)
            ultima_competencia = max(ultima_competencia or maior, maior)
        log.info("fii %s: %s informes", ano, len(informes))

    _touch_sync_state(
        session,
        "cvm_fii_inf_mensal",
        cursor=ultima_competencia.isoformat() if ultima_competencia else None,
    )
    return {"informes": total}


def _zip_table(archive: zipfile.ZipFile, token: str) -> CsvTable | None:
    for name in archive.namelist():
        if token in name.lower() and name.lower().endswith(".csv"):
            return CsvTable(archive.read(name), name)
    return None


def _load_fii_geral(
    table: CsvTable,
    informes: dict[tuple[str, dt.date], dict],
    fundos_attrs: dict[str, dict],
) -> None:
    idx_cnpj = table.require("CNPJ_FUNDO", "CNPJ_FUNDO_CLASSE")
    idx_comp = table.require("DATA_REFERENCIA", "DT_COMPTC")
    idx_versao = table.index_of("VERSAO")
    idx_publico = table.index_of("PUBLICO_ALVO")
    idx_segmento = table.index_of("SEGMENTO_ATUACAO")
    idx_denom = table.index_of("NOME_FUNDO", "DENOM_SOCIAL")

    for row in table.rows:
        cnpj = norm_cnpj(cell(row, idx_cnpj))
        competencia = parse_competencia(cell(row, idx_comp))
        if not cnpj or not competencia:
            continue
        versao = parse_int(cell(row, idx_versao))
        chave = (cnpj, competencia)
        registro = informes.setdefault(chave, {})
        if versao is not None and versao < (registro.get("versao") or 0):
            continue  # o arquivo pode conter reapresentações; fica a versão maior
        registro["versao"] = versao

        # atributos cadastrais vêm da competência mais recente
        atual = fundos_attrs.get(cnpj)
        if atual is None or competencia >= atual["_competencia"]:
            fundos_attrs[cnpj] = {
                "_competencia": competencia,
                "publico_alvo": cell(row, idx_publico),
                "segmento": cell(row, idx_segmento),
                "denominacao": cell(row, idx_denom),
            }


def _load_fii_complemento(table: CsvTable, informes: dict[tuple[str, dt.date], dict]) -> None:
    idx_cnpj = table.require("CNPJ_FUNDO", "CNPJ_FUNDO_CLASSE")
    idx_comp = table.require("DATA_REFERENCIA", "DT_COMPTC")
    idx_cotistas = table.index_of("TOTAL_NUMERO_COTISTAS", "QT_COTST", "NUMERO_COTISTAS")
    idx_pl = table.index_of("PATRIMONIO_LIQUIDO", "VL_PATRIM_LIQ")
    idx_vcota = table.index_of("VALOR_PATRIMONIAL_COTAS", "VL_PATRIM_COTA", "VALOR_PATRIMONIAL_COTA")

    for row in table.rows:
        cnpj = norm_cnpj(cell(row, idx_cnpj))
        competencia = parse_competencia(cell(row, idx_comp))
        if not cnpj or not competencia:
            continue
        registro = informes.setdefault((cnpj, competencia), {})
        if idx_cotistas is not None:
            registro["num_cotistas"] = parse_int(cell(row, idx_cotistas))
        if idx_pl is not None:
            registro["pl"] = parse_decimal(cell(row, idx_pl))
        if idx_vcota is not None:
            registro["valor_cota"] = parse_decimal(cell(row, idx_vcota))


def _load_fii_ativo_passivo(table: CsvTable, informes: dict[tuple[str, dt.date], dict]) -> None:
    idx_cnpj = table.require("CNPJ_FUNDO", "CNPJ_FUNDO_CLASSE")
    idx_comp = table.require("DATA_REFERENCIA", "DT_COMPTC")
    idx_pl = table.index_of("PATRIMONIO_LIQUIDO", "VL_PATRIM_LIQ")
    if idx_pl is None:
        return

    for row in table.rows:
        cnpj = norm_cnpj(cell(row, idx_cnpj))
        competencia = parse_competencia(cell(row, idx_comp))
        if not cnpj or not competencia:
            continue
        registro = informes.setdefault((cnpj, competencia), {})
        if registro.get("pl") is None:  # complemento tem precedência
            registro["pl"] = parse_decimal(cell(row, idx_pl))


# ---------------------------------------------------------------------------
# FIDC — Informe Mensal (zip mensal, tabelas tab_I..tab_X) — EXPERIMENTAL
# ---------------------------------------------------------------------------

_FIDC_PL_CANDIDATES = ("VL_PATRIM_LIQ", "TAB_IV_VL_PL", "VL_PL", "PATRIMONIO_LIQUIDO")
_FIDC_COTISTAS_CANDIDATES = ("QT_COTST", "QT_COTISTAS", "NR_COTST", "TAB_X_QT_COTST")
_FIDC_CLASSE_COLS = ("CNPJ_CLASSE", "CLASSE", "CLASSE_SERIE", "TAB_X_CLASSE_SERIE")


def sync_fidc_informes(session: Session, fetch: FetchFn, competencias: list[str]) -> dict:
    """competencias no formato AAAAMM (um zip por mês no portal da CVM)."""
    total = 0
    for competencia_str in competencias:
        archive = zipfile.ZipFile(io.BytesIO(fetch(url_fidc(competencia_str))))
        # (chave -> valor, prioridade da tabela de origem)
        pls: dict[tuple[str, dt.date], tuple] = {}
        cotistas: dict[tuple[str, dt.date], tuple] = {}
        denominacoes: dict[str, str] = {}
        for name in archive.namelist():
            if not name.lower().endswith(".csv"):
                continue
            table = CsvTable(archive.read(name), name)
            _scan_fidc_table(table, pls, cotistas, denominacoes)

        if not pls and not cotistas:
            raise LayoutError(
                f"inf_mensal_fidc_{competencia_str}.zip: nenhuma tabela com colunas de "
                f"PL {_FIDC_PL_CANDIDATES} ou cotistas {_FIDC_COTISTAS_CANDIDATES}; "
                f"conteúdo: {archive.namelist()}"
            )

        informes: dict[tuple[str, dt.date], dict] = {}
        for chave, (valor, _) in pls.items():
            informes.setdefault(chave, {})["pl"] = valor
        for chave, (valor, _) in cotistas.items():
            informes.setdefault(chave, {})["num_cotistas"] = valor

        _upsert_informes(session, informes, fonte="fidc_inf_mensal")
        _enrich_fundos(
            session,
            {cnpj: {"denominacao": denom} for cnpj, denom in denominacoes.items()},
            tipo_default="FIDC",
        )
        total += len(informes)
        log.info("fidc %s: %s informes", competencia_str, len(informes))

    _touch_sync_state(session, "cvm_fidc_inf_mensal", cursor=max(competencias, default=None))
    return {"informes": total}


def _scan_fidc_table(
    table: CsvTable,
    pls: dict,
    cotistas: dict,
    denominacoes: dict[str, str],
) -> None:
    idx_cnpj = table.index_of("CNPJ_FUNDO", "CNPJ_FUNDO_CLASSE")
    idx_comp = table.index_of("DT_COMPTC", "DATA_REFERENCIA", "DATA_COMPETENCIA")
    if idx_cnpj is None or idx_comp is None:
        return
    idx_pl = table.index_of(*_FIDC_PL_CANDIDATES)
    idx_cotistas = table.index_of(*_FIDC_COTISTAS_CANDIDATES)
    if idx_pl is None and idx_cotistas is None:
        return
    idx_denom = table.index_of("DENOM_SOCIAL")
    # com classes/séries na tabela, os valores são somados por fundo+competência
    tem_classe = table.index_of(*_FIDC_CLASSE_COLS) is not None
    nome = table.filename.lower()
    prioridade_pl = 0 if "tab_iv" in nome else 1
    prioridade_cot = 0 if "tab_x" in nome else 1

    pls_arquivo: dict[tuple[str, dt.date], object] = defaultdict(lambda: None)
    cot_arquivo: dict[tuple[str, dt.date], object] = defaultdict(lambda: None)
    for row in table.rows:
        cnpj = norm_cnpj(cell(row, idx_cnpj))
        competencia = parse_competencia(cell(row, idx_comp))
        if not cnpj or not competencia:
            continue
        chave = (cnpj, competencia)
        denom = cell(row, idx_denom)
        if denom:
            denominacoes.setdefault(cnpj, denom)
        if idx_pl is not None:
            valor = parse_decimal(cell(row, idx_pl))
            if valor is not None:
                atual = pls_arquivo[chave]
                pls_arquivo[chave] = valor + atual if (tem_classe and atual is not None) else valor
        if idx_cotistas is not None:
            valor_int = parse_int(cell(row, idx_cotistas))
            if valor_int is not None:
                atual = cot_arquivo[chave]
                cot_arquivo[chave] = (
                    valor_int + atual if (tem_classe and atual is not None) else valor_int
                )

    # entre tabelas do mesmo zip, vale a de maior prioridade (tab_IV p/ PL, tab_X p/ cotistas)
    for chave, valor in pls_arquivo.items():
        if valor is not None and (chave not in pls or prioridade_pl < pls[chave][1]):
            pls[chave] = (valor, prioridade_pl)
    for chave, valor in cot_arquivo.items():
        if valor is not None and (chave not in cotistas or prioridade_cot < cotistas[chave][1]):
            cotistas[chave] = (valor, prioridade_cot)


# ---------------------------------------------------------------------------
# Comuns
# ---------------------------------------------------------------------------


def _upsert_informes(
    session: Session, informes: dict[tuple[str, dt.date], dict], fonte: str
) -> None:
    if not informes:
        return
    # garante a FK: cria fundos ainda desconhecidos com o mínimo (enriquecidos depois)
    cnpjs = {cnpj for cnpj, _ in informes}
    existentes = set(
        session.execute(select(Fundo.cnpj).where(Fundo.cnpj.in_(cnpjs))).scalars()
    )
    novos = [{"cnpj": cnpj} for cnpj in sorted(cnpjs - existentes)]
    if novos:
        bulk_upsert(session, Fundo, novos, key_cols=["cnpj"], update_cols=["denominacao"])

    rows = [
        {
            "cnpj": cnpj,
            "competencia": competencia,
            "versao": dados.get("versao"),
            "pl": dados.get("pl"),
            "num_cotistas": dados.get("num_cotistas"),
            "valor_cota": dados.get("valor_cota"),
            "fonte": fonte,
        }
        for (cnpj, competencia), dados in informes.items()
    ]
    bulk_upsert(
        session,
        InformeMensal,
        rows,
        key_cols=["cnpj", "competencia"],
        update_cols=["versao", "pl", "num_cotistas", "valor_cota", "fonte"],
    )


def _enrich_fundos(session: Session, fundos_attrs: dict[str, dict], tipo_default: str) -> None:
    """Atualiza atributos de fundos sem sobrescrever dado existente com nulo."""
    for cnpj, attrs in fundos_attrs.items():
        fundo = session.get(Fundo, cnpj)
        if fundo is None:
            fundo = Fundo(cnpj=cnpj)
            session.add(fundo)
        for campo in ("denominacao", "publico_alvo", "segmento"):
            valor = attrs.get(campo)
            if valor:
                setattr(fundo, campo, valor)
        if not fundo.tipo_veiculo:
            fundo.tipo_veiculo = map_tipo_veiculo(tipo_default, fundo.denominacao)


def _touch_sync_state(session: Session, fonte: str, cursor: str | None) -> None:
    state = session.get(SyncState, fonte)
    if state is None:
        state = SyncState(fonte=fonte)
        session.add(state)
    state.cursor = cursor
    state.atualizado_em = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
