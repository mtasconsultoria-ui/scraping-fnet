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
from .fnet_client import FnetClient, detect_ext
from .http_client import fetch_bytes
from .parsing import CsvTable, cell, cnpj_valido, norm_cnpj
from .text_extract import extrair
from .textos import encontrar_trechos, fold, termo_regex

log = logging.getLogger(__name__)

SITUACAO_ATIVA = "EM FUNCIONAMENTO NORMAL"
# âncoras de dígito: não casar o miolo de um protocolo/processo de 16+ dígitos
_CNPJ_RE = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")


def carregar_cadastro(fetch=fetch_bytes) -> dict[str, dict]:
    """CNPJ -> dados cadastrais, do cad_fi.csv da CVM (para checar a situação)."""
    log.info("baixando cadastro de fundos da CVM...")
    tabela = CsvTable(fetch(url_cadastro()), "cad_fi.csv")
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


def _cnpj_do_texto(texto: str, cadastro: dict[str, dict]) -> str | None:
    """CNPJ do fundo a partir do texto do documento.

    O preâmbulo costuma citar mais de um CNPJ (fundo, administrador, gestor) e
    números longos que parecem CNPJ. Defesas, nesta ordem: âncora de dígitos no
    regex, validação dos dígitos verificadores, e preferência por um CNPJ que
    exista no cad_fi — administradores e gestores não estão lá, fundos sim.
    """
    candidatos: list[str] = []
    for match in _CNPJ_RE.finditer(texto[:4000]):
        cnpj = norm_cnpj(match.group())
        if cnpj and cnpj_valido(cnpj) and cnpj not in candidatos:
            candidatos.append(cnpj)
    for cnpj in candidatos:
        if cnpj in cadastro:
            return cnpj
    return candidatos[0] if candidatos else None


def prospectar(
    termos: list[str],
    *,
    alvo: int = 5,
    categoria: str = "Regulamento",
    tipo_fundo: str | None = None,
    max_documentos: int = 120,
    apenas_ativos: bool = True,
    max_trechos: int = 2,
    client: FnetClient | None = None,
    fetch=fetch_bytes,
) -> int:
    """Procura `alvo` fundos cujo regulamento cite algum dos termos.

    `client` e `fetch` são injetáveis para teste; por padrão vão à rede.
    """
    print(f"PROSPECÇÃO — termos: {termos}")
    print(f"meta: {alvo} fundos | categoria: {categoria} | teto: {max_documentos} documentos\n")

    cadastro = carregar_cadastro(fetch)
    client_proprio = client is None
    if client is None:
        client = FnetClient()
    try:
        id_categoria = resolver_dominio(client, "categoria", categoria)
        if tipo_fundo:
            id_tipo = resolver_dominio(client, "tipoFundo", tipo_fundo)
            if id_tipo is None:
                disponiveis = [rotulo for _, rotulo in client.listar_tipos_fundo()]
                print(f"tipo de fundo '{tipo_fundo}' não existe no FNET; opções: {disponiveis}")
                return 1
            tipos = [(int(id_tipo), tipo_fundo)]
        else:
            # a busca sem tipo não percorre o acervo (só os documentos do dia,
            # constatado em produção): a varredura é sempre por tipo
            tipos = client.listar_tipos_fundo()

        contexto = _Contexto(
            cadastro=cadastro,
            regexes={t: termo_regex(t) for t in termos},
            id_categoria=id_categoria,
            categoria=categoria,
            apenas_ativos=apenas_ativos,
            max_trechos=max_trechos,
            max_documentos=max_documentos,
            alvo=alvo,
        )
        for tf_id, tf_rotulo in tipos:
            if contexto.esgotado():
                break
            log.info("== varrendo %s (tipoFundo=%s) ==", tf_rotulo, tf_id)
            for doc in client.iter_documents(
                page_size=50,
                max_pages=40,
                id_categoria=int(id_categoria) if id_categoria else None,
                tipo_fundo=tf_id,
                ordem="desc",  # mais recentes primeiro: regulamentos vigentes
            ):
                if contexto.esgotado():
                    break
                _examinar_documento(doc, client, contexto)
    finally:
        if client_proprio:
            client.close()

    _imprimir(contexto.achados, termos, contexto.examinados, contexto.sem_texto, alvo)
    return 0 if contexto.achados else 1


class _Contexto:
    """Estado compartilhado da varredura (atravessa os tipos de fundo)."""

    def __init__(self, *, cadastro, regexes, id_categoria, categoria,
                 apenas_ativos, max_trechos, max_documentos, alvo):
        self.cadastro = cadastro
        self.regexes = regexes
        self.id_categoria = id_categoria
        self.categoria = categoria
        self.apenas_ativos = apenas_ativos
        self.max_trechos = max_trechos
        self.max_documentos = max_documentos
        self.alvo = alvo
        self.achados: dict[str, dict] = {}
        self.vistos: set[str] = set()
        self.examinados = 0
        self.sem_texto = 0

    def esgotado(self) -> bool:
        return len(self.achados) >= self.alvo or self.examinados >= self.max_documentos

    def fundo_elegivel(self, cnpj: str) -> bool:
        """Marca o CNPJ como visto e diz se ele passa no filtro de situação."""
        if cnpj in self.vistos:
            return False
        self.vistos.add(cnpj)
        info = self.cadastro.get(cnpj)
        if self.apenas_ativos and info is not None and info.get("situacao") != SITUACAO_ATIVA:
            return False
        return True


def _examinar_documento(doc: dict, client: FnetClient, ctx: _Contexto) -> None:
    categoria_doc = (doc.get("categoriaDocumento") or "").strip()
    if ctx.id_categoria is None and fold(ctx.categoria) not in fold(categoria_doc):
        return  # sem o id do filtro, seleciona pelo rótulo da resposta

    # Quando os metadados trazem o CNPJ, o filtro de situação roda ANTES do
    # download (poupa requisições); quando não trazem — caso comum, constatado
    # em produção — o CNPJ é extraído do texto do próprio documento, depois.
    cnpj = _cnpj_do_documento(doc)
    if cnpj is not None and not ctx.fundo_elegivel(cnpj):
        return

    doc_id = int(doc["id"])
    try:
        conteudo = client.download_document(doc_id)
        extracao = extrair(conteudo, detect_ext(conteudo))
    except Exception as exc:
        log.warning("documento %s: %s", doc_id, exc)
        return

    ctx.examinados += 1
    if not extracao.texto:
        ctx.sem_texto += 1
        log.info("[%s/%s] doc %s — sem camada de texto (fila de OCR)",
                 ctx.examinados, ctx.max_documentos, doc_id)
        return

    if cnpj is None:
        cnpj = _cnpj_do_texto(extracao.texto, ctx.cadastro)
        if cnpj is None:
            log.info("[%s/%s] doc %s — sem CNPJ identificável, ignorado",
                     ctx.examinados, ctx.max_documentos, doc_id)
            return
        if not ctx.fundo_elegivel(cnpj):
            return

    info = ctx.cadastro.get(cnpj)
    texto_norm = fold(extracao.texto)
    casados = [t for t, rx in ctx.regexes.items() if rx.search(texto_norm)]
    log.info("[%s/%s] %s %s %s", ctx.examinados, ctx.max_documentos,
             "ACHOU" if casados else "     ", cnpj,
             (info or {}).get("denominacao") or (doc.get("descricaoFundo") or "")[:50])
    if not casados:
        return

    ctx.achados[cnpj] = {
        "cnpj": cnpj,
        "doc_id": doc_id,
        "denominacao": (info or {}).get("denominacao") or doc.get("descricaoFundo"),
        "situacao": (info or {}).get("situacao") or "(não consta no cad_fi)",
        "tipo": (info or {}).get("tipo"),
        "publico_alvo": (info or {}).get("publico_alvo"),
        "administrador": (info or {}).get("administrador"),
        "gestor": (info or {}).get("gestor"),
        "data_referencia": doc.get("dataReferencia"),
        "data_entrega": doc.get("dataEntrega"),
        "paginas": extracao.num_paginas,
        "trechos": encontrar_trechos(
            extracao.texto, texto_norm, casados, max_trechos=ctx.max_trechos
        ),
    }


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
