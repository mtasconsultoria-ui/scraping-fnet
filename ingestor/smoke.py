"""Teste de fumaça: uma requisição a cada portal, para conferir os formatos.

Não usa banco nem storage. Serve para responder, em minutos e sem backfill, a
pergunta que só a rede responde: os parâmetros da API não documentada do FNET e
os nomes de coluna dos CSVs da CVM continuam válidos?

A saída é deliberadamente crua (colunas encontradas, chaves do JSON, primeiros
bytes) — é o que permite corrigir um parser sem ter de adivinhar.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile

import httpx

from .cvm_bulk import url_cadastro, url_fii
from .fnet_client import BROWSER_HEADERS, FnetClient, detect_ext
from .parsing import CsvTable

log = logging.getLogger(__name__)

# Colunas de que os loaders dependem; se sumirem, a ingestão quebra
COLUNAS_CADASTRO = ("CNPJ_FUNDO", "DENOM_SOCIAL", "SIT", "TP_FUNDO")
COLUNAS_FII_GERAL = ("CNPJ_FUNDO", "DATA_REFERENCIA")
CAMPOS_FNET = ("id", "descricaoFundo", "categoriaDocumento", "dataEntrega")

LARGURA = 78


def _titulo(texto: str) -> None:
    print(f"\n{'=' * LARGURA}\n{texto}\n{'=' * LARGURA}")


def _resultado(ok: bool, mensagem: str) -> bool:
    print(f"\n  >>> {'OK' if ok else 'FALHA'}: {mensagem}")
    return ok


def testar_cvm_cadastro() -> bool:
    """Baixa só o começo do cad_fi.csv (Range) e confere o cabeçalho."""
    _titulo("1/4  CVM — cadastro de fundos (cad_fi.csv)")
    url = url_cadastro()
    print(f"GET {url}\n  (apenas os primeiros 400 KB, via header Range)")
    try:
        resposta = httpx.get(
            url,
            headers={**BROWSER_HEADERS, "Range": "bytes=0-400000"},
            timeout=120,
            follow_redirects=True,
        )
        resposta.raise_for_status()
    except httpx.HTTPError as exc:
        return _resultado(False, f"download falhou: {exc}")

    print(f"  HTTP {resposta.status_code} · {len(resposta.content):,} bytes recebidos")
    # a última linha do recorte quase sempre vem cortada ao meio
    conteudo = resposta.content.rsplit(b"\n", 1)[0]
    tabela = CsvTable(conteudo, "cad_fi.csv")
    print(f"\n  colunas encontradas ({len(tabela.columns)}):")
    print(f"    {sorted(tabela.columns)}")

    faltando = [c for c in COLUNAS_CADASTRO if tabela.index_of(c) is None]
    if tabela.rows:
        print("\n  primeira linha:")
        for coluna, indice in sorted(tabela.columns.items(), key=lambda kv: kv[1])[:12]:
            valor = tabela.rows[0][indice] if indice < len(tabela.rows[0]) else ""
            print(f"    {coluna:24} = {valor[:60]}")

    if faltando:
        return _resultado(False, f"colunas obrigatórias ausentes: {faltando}")
    return _resultado(True, f"{len(tabela.rows)} linhas no recorte, colunas esperadas presentes")


def testar_cvm_informe_fii(ano: int) -> bool:
    _titulo(f"2/4  CVM — Informe Mensal Estruturado de FII ({ano})")
    url = url_fii(ano)
    print(f"GET {url}")
    try:
        resposta = httpx.get(url, headers=BROWSER_HEADERS, timeout=300, follow_redirects=True)
        resposta.raise_for_status()
    except httpx.HTTPError as exc:
        return _resultado(False, f"download falhou: {exc}")

    print(f"  HTTP {resposta.status_code} · {len(resposta.content):,} bytes")
    try:
        arquivo = zipfile.ZipFile(io.BytesIO(resposta.content))
    except zipfile.BadZipFile as exc:
        return _resultado(False, f"não é um zip válido: {exc}")

    print(f"\n  arquivos no zip ({len(arquivo.namelist())}):")
    for nome in arquivo.namelist()[:10]:
        print(f"    {nome}")

    geral = next((n for n in arquivo.namelist() if "geral" in n.lower()), None)
    if geral is None:
        return _resultado(False, "nenhum arquivo 'geral' no zip")

    tabela = CsvTable(arquivo.read(geral), geral)
    print(f"\n  colunas de {geral} ({len(tabela.columns)}):")
    print(f"    {sorted(tabela.columns)}")
    faltando = [c for c in COLUNAS_FII_GERAL if tabela.index_of(c) is None]
    if faltando:
        return _resultado(False, f"colunas obrigatórias ausentes: {faltando}")
    return _resultado(True, f"{len(tabela.rows):,} linhas em {geral}")


def testar_fnet_busca(client: FnetClient) -> dict | None:
    _titulo("3/4  FNET — API de busca de documentos")
    print(f"GET {client.base_url}/pesquisarGerenciadorDocumentosDados  (l=3)")
    try:
        payload = client.search_documents(start=0, length=3, ordem="desc")
    except Exception as exc:
        _resultado(False, f"{type(exc).__name__}: {exc}")
        return None

    print(f"\n  chaves do JSON: {sorted(payload)}")
    print(f"  recordsTotal: {payload.get('recordsTotal')}")
    dados = payload.get("data") or []
    print(f"  itens retornados: {len(dados)}")
    if not dados:
        _resultado(False, "a busca não retornou nenhum documento")
        return None

    print("\n  primeiro item (campos e valores):")
    for chave, valor in dados[0].items():
        print(f"    {chave:26} = {str(valor)[:60]}")

    faltando = [c for c in CAMPOS_FNET if c not in dados[0]]
    if faltando:
        _resultado(False, f"campos esperados ausentes: {faltando}")
        return None
    _resultado(True, "shape da resposta compatível com o parser")
    return dados[0]


def testar_fnet_download(client: FnetClient, doc_id: int) -> bool:
    _titulo("4/4  FNET — download de documento")
    print(f"GET {client.base_url}/downloadDocumento?id={doc_id}")
    try:
        bruto = client._get("downloadDocumento", params={"id": doc_id}).content
    except Exception as exc:
        return _resultado(False, f"{type(exc).__name__}: {exc}")

    print(f"  bytes recebidos: {len(bruto):,}")
    print(f"  primeiros 60 bytes crus: {bruto[:60]!r}")
    print(f"  formato antes de decodificar: {detect_ext(bruto)}")

    from .fnet_client import maybe_base64

    conteudo = maybe_base64(bruto)
    formato = detect_ext(conteudo)
    print(f"  veio em base64: {'sim' if conteudo is not bruto and conteudo != bruto else 'nao'}")
    print(f"  formato final: {formato} · {len(conteudo):,} bytes")

    if formato == "bin":
        return _resultado(False, "conteúdo não reconhecido (nem PDF, nem XML, nem ZIP)")
    if formato == "pdf":
        try:
            from .text_extract import extrair_pdf

            extracao = extrair_pdf(conteudo)
            print(f"\n  extração: {extracao.num_paginas} página(s), "
                  f"{len(extracao.texto):,} caracteres, origem={extracao.origem}")
            print(f"  trecho: {extracao.texto[:200]!r}")
        except Exception as exc:
            return _resultado(False, f"extração de texto falhou: {exc}")
    return _resultado(True, f"documento {doc_id} baixado e legível ({formato})")


def testar_dominios(client: FnetClient) -> bool:
    _titulo("Extra — tabelas de domínio dos filtros do FNET")
    try:
        grupos = client.fetch_domains()
    except Exception as exc:
        return _resultado(False, f"{type(exc).__name__}: {exc}")

    for grupo, opcoes in sorted(grupos.items()):
        if not opcoes:
            continue
        print(f"\n  {grupo} ({len(opcoes)} opções):")
        for valor, rotulo in opcoes[:25]:
            print(f"    {valor:>6} = {rotulo}")
    return _resultado(True, f"{len(grupos)} grupos de filtro lidos")


def executar(ano_fii: int = 2026) -> int:
    """Roda todos os testes. Retorna 0 se todos passaram."""
    print("TESTE DE FUMAÇA — conectividade e formatos dos portais")
    print("Nenhum banco de dados é usado; nada é gravado.")

    resultados: dict[str, bool] = {}
    resultados["cvm_cadastro"] = testar_cvm_cadastro()
    resultados["cvm_informe_fii"] = testar_cvm_informe_fii(ano_fii)

    client = FnetClient()
    try:
        primeiro = testar_fnet_busca(client)
        resultados["fnet_busca"] = primeiro is not None
        if primeiro is not None:
            resultados["fnet_download"] = testar_fnet_download(client, int(primeiro["id"]))
        resultados["fnet_dominios"] = testar_dominios(client)
    finally:
        client.close()

    _titulo("RESUMO")
    for nome, ok in resultados.items():
        print(f"  {'OK   ' if ok else 'FALHA'}  {nome}")
    falhas = [n for n, ok in resultados.items() if not ok]
    print()
    if falhas:
        print(f"{len(falhas)} teste(s) falharam: {falhas}")
        return 1
    print("Todos os testes passaram: os portais respondem no formato esperado.")
    return 0
