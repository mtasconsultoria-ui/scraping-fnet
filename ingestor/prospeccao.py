"""Prospecção rápida: acha fundos cujo regulamento menciona um termo.

É o pipeline completo em escala reduzida e sem persistência — vai ao FNET, baixa
regulamentos, extrai o texto e procura o termo, parando assim que junta o número
de fundos pedido. Serve para responder a uma pergunta pontual ("quais fundos
admitem CDCA?") sem esperar o backfill, e para validar a rotina com dados reais.

O que a ingestão completa faz melhor: cobre todo o universo em vez de uma
amostra dos documentos mais recentes, e guarda tudo para consultas futuras.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from .cvm_bulk import url_cadastro
from .fnet_client import FnetClient
from .http_client import fetch_bytes
from .parsing import CsvTable, cell, norm_cnpj
from .text_extract import extrair
from .textos import encontrar_trechos, fold, termo_regex

log = logging.getLogger(__name__)

SITUACAO_ATIVA = "EM FUNCIONAMENTO NORMAL"
_CNPJ_RE = re.compile(r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}")


def carregar_cadastro() -> dict[str, dict]:
    """CNPJ -> dados cadastrais, do cad_fi.csv da CVM (para checar a situação)."""
    log.info("baixando cadastro de fundos da CVM...")
    tabela = CsvTable(fetch_bytes(url_cadastro()), "cad_fi.csv")
    idx = {
        "cnpj": tabela.require("CNPJ_FUNDO", "CNPJ_FUNDO_CLASSE"),
        "denom": tabela.index_of("DENOM_SOCIAL"),
        "sit": tabela.index_of("SIT"),
        "tipo": tabela.index_of("TP_FUNDO", "TP_FUNDO_CLASSE"),
        "publico": tabela.index_of("PUBLICO_ALVO"),
        "admin": tabela.index_of("ADMIN"),
        "gestor": tabela.index_of("GESTOR"),
        "classe": tabela.index_of("CLASSE"),
    }
    cadastro: dict[str, dict] = {}
    for linha in tabela.rows:
        cnpj = norm_cnpj(cell(linha, idx["cnpj"]))
        if not cnpj:
            continue
        situacao = cell(linha, idx["sit"])
        anterior = cadastro.get(cnpj)
        # entre registros do mesmo CNPJ, o ativo prevalece
        if anterior and anterior.get("situacao") == SITUACAO_ATIVA:
            continue
        cadastro[cnpj] = {
            "denominacao": cell(linha, idx["denom"]),
            "situacao": situacao,
            "tipo": cell(linha, idx["tipo"]),
            "publico_alvo": cell(linha, idx["publico"]),
            "administrador": cell(linha, idx["admin"]),
            "gestor": cell(linha, idx["gestor"]),
            "classe": cell(linha, idx["classe"]),
        }
    log.info("cadastro: %s fundos", len(cadastro))
    return cadastro


def resolver_dominio(client: FnetClient, grupo_hint: str, rotulo: str) -> str | None:
    """Descobre o id de um filtro do FNET pelo rótulo (ex.: 'Regulamento').

    Os ids não são estáveis entre versões do site, então são lidos da própria
    página de filtros em vez de ficarem fixos no código.
    """
    try:
        grupos = client.fetch_domains()
    except Exception as exc:
        log.warning("não foi possível ler os domínios do FNET: %s", exc)
        return None
    alvo = fold(rotulo)
    for grupo, opcoes in grupos.items():
        if grupo_hint.lower() not in grupo.lower():
            continue
        for valor, texto in opcoes:
            if fold(texto).strip() == alvo and valor not in ("", "0"):
                log.info("domínio %s: '%s' = %s", grupo, texto, valor)
                return valor
    log.warning("rótulo '%s' não encontrado no grupo '%s'", rotulo, grupo_hint)
    return None


def _cnpj_do_documento(doc: dict) -> str | None:
    cnpj = norm_cnpj(doc.get("cnpjFundo"))
    if cnpj:
        return cnpj
    descricao = doc.get("descricaoFundo") or ""
    achado = _CNPJ_RE.search(descricao)
    return norm_cnpj(achado.group()) if achado else None


def prospectar(
    termos: list[str],
    *,
    alvo: int = 5,
    categoria: str = "Regulamento",
    tipo_fundo: str | None = None,
    max_documentos: int = 120,
    apenas_ativos: bool = True,
    max_trechos: int = 2,
) -> int:
    """Procura `alvo` fundos cujo regulamento cite algum dos termos."""
    print(f"PROSPECÇÃO — termos: {termos}")
    print(f"meta: {alvo} fundos | categoria: {categoria} | teto: {max_documentos} documentos\n")

    cadastro = carregar_cadastro()
    client = FnetClient()
    try:
        id_categoria = resolver_dominio(client, "categoria", categoria)
        id_tipo = resolver_dominio(client, "tipoFundo", tipo_fundo) if tipo_fundo else None

        regexes = {t: termo_regex(t) for t in termos}
        achados: dict[str, dict] = {}
        examinados = 0
        sem_texto = 0
        vistos: set[str] = set()

        # documentos mais recentes primeiro: regulamentos vigentes de fundos vivos
        iterador = client.iter_documents(
            page_size=50,
            max_pages=40,
            id_categoria=int(id_categoria) if id_categoria else None,
            tipo_fundo=int(id_tipo) if id_tipo else None,
            ordem="desc",
        )

        for doc in iterador:
            if len(achados) >= alvo or examinados >= max_documentos:
                break
            cnpj = _cnpj_do_documento(doc)
            if not cnpj or cnpj in vistos:
                continue
            categoria_doc = (doc.get("categoriaDocumento") or "").strip()
            if id_categoria is None and fold(categoria) not in fold(categoria_doc):
                continue  # sem o id do filtro, seleciona pelo rótulo da resposta
            vistos.add(cnpj)

            info = cadastro.get(cnpj)
            situacao = (info or {}).get("situacao")
            if apenas_ativos and info is not None and situacao != SITUACAO_ATIVA:
                continue

            doc_id = int(doc["id"])
            try:
                conteudo = client.download_document(doc_id)
                from .fnet_client import detect_ext

                extracao = extrair(conteudo, detect_ext(conteudo))
            except Exception as exc:
                log.warning("documento %s: %s", doc_id, exc)
                continue

            examinados += 1
            if not extracao.texto:
                sem_texto += 1
                log.info("[%s/%s] %s — sem camada de texto (precisaria de OCR)",
                         examinados, max_documentos, cnpj)
                continue

            texto_norm = fold(extracao.texto)
            casados = [t for t, rx in regexes.items() if rx.search(texto_norm)]
            marca = "ACHOU" if casados else "     "
            log.info("[%s/%s] %s %s %s", examinados, max_documentos, marca, cnpj,
                     (info or {}).get("denominacao") or doc.get("descricaoFundo", "")[:50])
            if not casados:
                continue

            achados[cnpj] = {
                "cnpj": cnpj,
                "doc_id": doc_id,
                "denominacao": (info or {}).get("denominacao")
                or doc.get("descricaoFundo"),
                "situacao": situacao or "(não consta no cad_fi)",
                "tipo": (info or {}).get("tipo"),
                "publico_alvo": (info or {}).get("publico_alvo"),
                "administrador": (info or {}).get("administrador"),
                "gestor": (info or {}).get("gestor"),
                "data_referencia": doc.get("dataReferencia"),
                "data_entrega": doc.get("dataEntrega"),
                "paginas": extracao.num_paginas,
                "trechos": encontrar_trechos(
                    extracao.texto, texto_norm, casados, max_trechos=max_trechos
                ),
            }
    finally:
        client.close()

    _imprimir(achados, termos, examinados, sem_texto, alvo)
    return 0 if achados else 1


def _imprimir(achados: dict, termos: list[str], examinados: int, sem_texto: int, alvo: int) -> None:
    print(f"\n{'=' * 78}")
    print(f"RESULTADO — {len(achados)} de {alvo} fundo(s) encontrados")
    print(f"{'=' * 78}")
    print(f"regulamentos examinados: {examinados} (sem camada de texto: {sem_texto})")

    for i, dados in enumerate(achados.values(), 1):
        print(f"\n{'-' * 78}")
        print(f"{i}. {dados['denominacao']}")
        print(f"   CNPJ: {dados['cnpj']}  |  situação: {dados['situacao']}")
        print(f"   tipo: {dados['tipo'] or 'n/d'}  |  público-alvo: {dados['publico_alvo'] or 'n/d'}")
        print(f"   administrador: {dados['administrador'] or 'n/d'}")
        print(f"   gestor: {dados['gestor'] or 'n/d'}")
        print(f"   regulamento: doc {dados['doc_id']} · ref {dados['data_referencia']}"
              f" · {dados['paginas']} páginas")
        print(f"   https://fnet.bmfbovespa.com.br/fnet/publico/downloadDocumento?id={dados['doc_id']}")
        for trecho in dados["trechos"]:
            aviso = "  [POSSÍVEL VEDAÇÃO]" if trecho.possivel_vedacao else ""
            print(f"\n   ...{trecho.destacado()}...{aviso}")

    if len(achados) < alvo:
        print(f"\n{'=' * 78}")
        print(f"AVISO: só {len(achados)} fundo(s) encontrados nos {examinados} regulamentos")
        print("examinados. Aumente --max-documentos, amplie os termos ou filtre por tipo")
        print("de fundo mais aderente ao ativo procurado.")
    print(f"\nData da consulta: {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC")
