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

from . import cvm_bulk
from .cvm_bulk import url_cadastro, url_fii
from .fnet_client import BROWSER_HEADERS, FnetClient, detect_ext
from .parsing import CsvTable, LayoutError

log = logging.getLogger(__name__)

CAMPOS_FNET = ("id", "descricaoFundo", "categoriaDocumento", "dataEntrega")

LARGURA = 78


def _titulo(texto: str) -> None:
    print(f"\n{'=' * LARGURA}\n{texto}\n{'=' * LARGURA}")


def _resultado(ok: bool, mensagem: str) -> bool:
    print(f"\n  >>> {'OK' if ok else 'FALHA'}: {mensagem}")
    return ok


# Colunas de que os loaders dependem; ausência no CABEÇALHO é quebra de layout.
# Taxa de preenchimento é outra história: o começo do cad_fi é dominado por
# fundos dos anos 90, cancelados, com público-alvo/admin legitimamente vazios
# (constatado na 1ª execução real) — por isso a análise compara início e fim.
_COLUNAS_CADASTRO = (
    "CNPJ_FUNDO", "DENOM_SOCIAL", "SIT", "TP_FUNDO",
    "PUBLICO_ALVO", "ADMIN", "GESTOR", "DT_REG", "CLASSE",
)
_CAMPOS_TAXA = ("denominacao", "tipo_veiculo", "situacao", "publico_alvo", "administrador")


def _taxas(fundos: list[dict]) -> dict[str, int]:
    return {campo: sum(1 for f in fundos if f.get(campo)) for campo in _CAMPOS_TAXA}


def testar_cvm_cadastro() -> bool:
    """Valida o cad_fi.csv com o parser de produção, em amostras do início e do fim."""
    _titulo("1/4  CVM — cadastro de fundos (cad_fi.csv)")
    url = url_cadastro()
    print(f"GET {url}\n  (amostras de 400 KB do início e do fim, via header Range)")
    try:
        cabeca = httpx.get(
            url,
            headers={**BROWSER_HEADERS, "Range": "bytes=0-400000"},
            timeout=120,
            follow_redirects=True,
        )
        cabeca.raise_for_status()
    except httpx.HTTPError as exc:
        return _resultado(False, f"download falhou: {exc}")

    print(f"  HTTP {cabeca.status_code} · {len(cabeca.content):,} bytes (início)")
    linha_cabecalho, _, resto = cabeca.content.partition(b"\n")
    # a última linha do recorte quase sempre vem cortada ao meio
    tabela = CsvTable(linha_cabecalho + b"\n" + resto.rsplit(b"\n", 1)[0], "cad_fi.csv")
    print(f"\n  colunas encontradas ({len(tabela.columns)}):")
    print(f"    {sorted(tabela.columns)}")

    faltando = [c for c in _COLUNAS_CADASTRO if tabela.index_of(c) is None]
    if faltando:
        return _resultado(False, f"colunas ausentes no cabeçalho: {faltando}")

    try:
        inicio = cvm_bulk.parse_cadastro(tabela)
    except LayoutError as exc:
        return _resultado(False, f"parse_cadastro falhou: {exc}")
    print(f"\n  parse_cadastro no início do arquivo: {len(inicio)} fundos")
    print(f"    preenchimento: {_taxas(inicio)}")
    print("    (início = fundos antigos/cancelados; campos vazios aqui não indicam defeito)")

    # amostra do FIM: fundos recentes têm os campos preenchidos — se um campo
    # vier vazio também aqui, o conteúdo mudou de coluna
    fim: list[dict] = []
    if cabeca.status_code == 206:
        try:
            cauda = httpx.get(
                url,
                headers={**BROWSER_HEADERS, "Range": "bytes=-400000"},
                timeout=120,
                follow_redirects=True,
            )
            if cauda.status_code == 206:
                corpo = cauda.content.split(b"\n", 1)[-1]  # descarta a linha cortada
                fim = cvm_bulk.parse_cadastro(
                    CsvTable(linha_cabecalho + b"\n" + corpo, "cad_fi.csv (fim)")
                )
                print(f"\n  parse_cadastro no fim do arquivo: {len(fim)} fundos")
                print(f"    preenchimento: {_taxas(fim)}")
            else:
                print(f"\n  AVISO: servidor não honrou o Range do fim (HTTP {cauda.status_code})")
        except Exception as exc:  # a amostra extra é bônus, não requisito
            print(f"\n  AVISO: amostra do fim indisponível: {exc}")

    universo = inicio + fim
    if universo:
        print("\n  exemplo:")
        exemplo = (fim or inicio)[-1]
        for chave, valor in exemplo.items():
            print(f"    {chave:16} = {str(valor)[:60]}")
    vazios = [campo for campo, n in _taxas(universo).items() if n == 0]
    if vazios and fim:
        return _resultado(False, f"campos vazios em TODAS as amostras (coluna mudou?): {vazios}")
    if vazios:
        # sem a amostra do fim não há como distinguir coluna renomeada de dado
        # de época em branco — e o início vazio é o esperado; não falha por isso
        print(f"\n  AVISO: campos vazios no início e amostra do fim indisponível: {vazios};")
        print("  validação limitada a cabeçalho + parser nesta execução")
    return _resultado(True, f"{len(universo)} fundos extraídos pelo parser de produção")


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

    # todas as tabelas: um nome de coluna que mudou em qualquer uma delas
    # esvazia silenciosamente um campo da busca
    tabelas: dict[str, CsvTable] = {}
    for token in ("geral", "complemento", "ativo_passivo"):
        nome = next((n for n in arquivo.namelist() if token in n.lower()), None)
        if nome is None:
            print(f"\n  AVISO: nenhum arquivo '{token}' no zip")
            continue
        tabela = CsvTable(arquivo.read(nome), nome)
        tabelas[token] = tabela
        print(f"\n  colunas de {nome} ({len(tabela.columns)}):")
        print(f"    {sorted(tabela.columns)}")

    if "geral" not in tabelas:
        return _resultado(False, "arquivo 'geral' ausente: o loader não tem como funcionar")

    # roda os parsers de produção sobre os arquivos reais
    informes: dict = {}
    atributos: dict = {}
    try:
        cvm_bulk._load_fii_geral(tabelas["geral"], informes, atributos)
        if "complemento" in tabelas:
            cvm_bulk._load_fii_complemento(tabelas["complemento"], informes)
        if "ativo_passivo" in tabelas:
            cvm_bulk._load_fii_ativo_passivo(tabelas["ativo_passivo"], informes)
    except LayoutError as exc:
        return _resultado(False, f"parser do informe falhou: {exc}")

    com_pl = sum(1 for v in informes.values() if v.get("pl") is not None)
    com_cotistas = sum(1 for v in informes.values() if v.get("num_cotistas") is not None)
    com_cota = sum(1 for v in informes.values() if v.get("valor_cota") is not None)
    com_denom = sum(1 for v in atributos.values() if v.get("denominacao"))
    com_publico = sum(1 for v in atributos.values() if v.get("publico_alvo"))

    print(f"\n  parsers de produção sobre o arquivo real:")
    print(f"    informes montados      {len(informes):,}")
    print(f"    com PL                 {com_pl:,}")
    print(f"    com nº de cotistas     {com_cotistas:,}")
    print(f"    com valor de cota      {com_cota:,}")
    print(f"    fundos com denominação {com_denom:,} (de {len(atributos):,})")
    print(f"    fundos com público-alvo{com_publico:,} (de {len(atributos):,})")

    if not informes:
        return _resultado(False, "nenhum informe extraído: chaves CNPJ/competência mudaram")
    vazios = [
        nome
        for nome, valor in (
            ("PL", com_pl), ("cotistas", com_cotistas), ("valor da cota", com_cota),
            ("denominação", com_denom), ("público-alvo", com_publico),
        )
        if valor == 0
    ]
    if vazios:
        return _resultado(False, f"campos vazios em TODAS as linhas (coluna renomeada?): {vazios}")
    return _resultado(True, f"{len(informes):,} informes extraídos com todos os campos")


def _sondar_variantes(client: FnetClient) -> None:
    """Testa combinações de parâmetros e mostra qual retorna resultados.

    O valor de "todos" difere entre os filtros do FNET (string vazia em uns, 0
    em outros). Em vez de apostar numa hipótese, mede-se cada variante.
    """
    import time as _time

    variantes: list[tuple[str, dict]] = [
        ("tipoFundo vazio (todos)", {"tipoFundo": ""}),
        ("tipoFundo=0", {"tipoFundo": 0}),
        ("sem o parâmetro tipoFundo", {}),
        ("tipoFundo=2 (FIDC)", {"tipoFundo": 2}),
        ("tipoFundo=11 (FIAGRO)", {"tipoFundo": 11}),
    ]
    print("\n  sondagem de parâmetros (recordsTotal por variante):")
    for rotulo, extra in variantes:
        params = {
            "d": 1, "s": 0, "l": 1, "o[0][dataEntrega]": "desc",
            "idCategoriaDocumento": 0, "idTipoDocumento": 0, "idEspecieDocumento": 0,
            "paginaCertificados": "false", "_": int(_time.time() * 1000),
            **extra,
        }
        try:
            resposta = client._get(
                "pesquisarGerenciadorDocumentosDados",
                params=params,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{client.base_url}/abrirGerenciadorDocumentosCVM",
                },
            )
            corpo = resposta.json()
            total = corpo.get("recordsTotal")
            itens = len(corpo.get("data") or [])
            print(f"    {rotulo:30} -> recordsTotal={total!s:>10}  itens={itens}")
        except Exception as exc:
            print(f"    {rotulo:30} -> {type(exc).__name__}: {str(exc)[:50]}")


def testar_fnet_busca(client: FnetClient) -> dict | None:
    _titulo("3/4  FNET — API de busca de documentos")
    _sondar_variantes(client)

    print(f"\nGET {client.base_url}/pesquisarGerenciadorDocumentosDados  (l=3, como o código usa)")
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
        for valor, rotulo in opcoes:  # sem truncar: os ids alimentam os filtros
            print(f"    {valor:>6} = {rotulo}")

    # o id de Regulamento é o que a prospecção usa para achar os documentos certos
    categorias = grupos.get("categoriaDocumento") or []
    regulamento = next((v for v, r in categorias if r.strip().lower() == "regulamento"), None)
    print(f"\n  id da categoria 'Regulamento': {regulamento or 'NÃO ENCONTRADO'}")
    if regulamento is None:
        return _resultado(False, "categoria 'Regulamento' ausente na lista de filtros")
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
