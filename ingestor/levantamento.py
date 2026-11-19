"""Levantamento de taxas em regulamentos: consultoria especializada por administrador.

Fluxo: cad_fi.csv (CVM) seleciona os fundos do administrador -> FNET localiza o
regulamento vigente de cada um -> o texto é extraído e analisado:

- classificação de cedência (monocedente / multicedente / indeterminado), pelo
  vocabulário do próprio regulamento;
- cláusula de consultoria especializada: percentuais ao ano e valores mínimos
  mensais encontrados na vizinhança do termo, SEMPRE acompanhados do trecho
  original — os números parseados são apoio, o trecho é a fonte.

Os valores saem num CSV (separador ';', BOM UTF-8, para o Excel) e num resumo
no log. Como toda extração de texto livre, isto é um rascunho de triagem:
conferir o trecho antes de usar o número.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import zipfile

from .fnet_client import FnetClient, detect_ext
from .http_client import fetch_bytes
from .parsing import CsvTable, cell, norm_cnpj
from .prospeccao import _CNPJ_RE, carregar_cadastro, esta_ativo, resolver_dominio
from .text_extract import extrair
from .textos import encontrar_trechos, fold

log = logging.getLogger(__name__)

# O TP_FUNDO do cad_fi pode trazer a sigla ou o nome por extenso, e isso já
# mudou entre safras do arquivo; o filtro aceita qualquer uma das formas.
SINONIMOS_TIPO = {
    "fidc": ("fidc", "direitos creditorios"),
    "fii": ("fii", "imobiliario"),
    "fiagro": ("fiagro", "cadeias produtivas agroindustriais"),
    "fip": ("fip", "participacoes"),
}


def _tipo_casa(tipo_pedido: str, tipo_cadastro: str | None) -> bool:
    alvo = fold(tipo_pedido)
    chaves = SINONIMOS_TIPO.get(alvo, (alvo,))
    tipo_cad = fold(tipo_cadastro or "")
    return any(chave in tipo_cad for chave in chaves)


# O cad_fi.csv virou o cadastro LEGADO (constatado em produção: os fundos da
# administradora pesquisada apareciam lá só como cancelados). Os fundos vivos,
# pós-CVM 175, estão no Registro de Fundos — um zip com registro_fundo.csv,
# registro_classe.csv e registro_subclasse.csv. A URL com underscore final é a
# contingência que o portal já usou; tenta-se as duas.
ARQUIVOS_REGISTRO = ("registro_fundo_classe.zip", "registro_fundo_classe_.zip")


def carregar_registro(fetch=fetch_bytes) -> dict[str, dict]:
    """CNPJ -> dados do Registro de Fundos (CVM 175), com público-alvo das classes."""
    data = None
    ultimo_erro: Exception | None = None
    for nome in ARQUIVOS_REGISTRO:
        url = f"{cvm_base()}/FI/CAD/DADOS/{nome}"
        try:
            data = fetch(url)
            log.info("registro de fundos carregado de %s", nome)
            break
        except Exception as exc:
            ultimo_erro = exc
    if data is None:
        raise RuntimeError(f"registro_fundo_classe indisponível: {ultimo_erro}")

    archive = zipfile.ZipFile(io.BytesIO(data))
    nomes = {n.rsplit("/", 1)[-1].lower(): n for n in archive.namelist()}
    nome_fundos = next((n for b, n in nomes.items() if b.startswith("registro_fundo")), None)
    if nome_fundos is None:
        raise RuntimeError(f"zip do registro sem registro_fundo.csv: {archive.namelist()}")

    tabela = CsvTable(archive.read(nome_fundos), nome_fundos)
    idx = {
        "cnpj": tabela.require("CNPJ_FUNDO", "CNPJ_FUNDO_CLASSE"),
        "id": tabela.index_of("ID_REGISTRO_FUNDO"),
        "denom": tabela.index_of("DENOMINACAO_SOCIAL", "DENOM_SOCIAL"),
        "tipo": tabela.index_of("TIPO_FUNDO", "TP_FUNDO"),
        "sit": tabela.index_of("SITUACAO", "SIT"),
        "admin": tabela.index_of("ADMINISTRADOR", "ADMIN"),
        "gestor": tabela.index_of("GESTOR"),
    }
    registro: dict[str, dict] = {}
    id_para_cnpj: dict[str, str] = {}
    for linha in tabela.rows:
        cnpj = norm_cnpj(cell(linha, idx["cnpj"]))
        if not cnpj:
            continue
        anterior = registro.get(cnpj)
        if anterior and esta_ativo(anterior.get("situacao")):
            continue
        registro[cnpj] = {
            "denominacao": cell(linha, idx["denom"]),
            "situacao": cell(linha, idx["sit"]),
            "tipo": cell(linha, idx["tipo"]),
            "publico_alvo": None,
            "administrador": cell(linha, idx["admin"]),
            "gestor": cell(linha, idx["gestor"]),
            "classe": None,
            "cnpjs_classes": [],
        }
        id_fundo = cell(linha, idx["id"])
        if id_fundo:
            id_para_cnpj[id_fundo] = cnpj

    nome_classes = next(
        (n for b, n in nomes.items() if b.startswith("registro_classe")), None
    )
    if nome_classes:
        classes = CsvTable(archive.read(nome_classes), nome_classes)
        idx_id = classes.index_of("ID_REGISTRO_FUNDO")
        idx_publico = classes.index_of("PUBLICO_ALVO")
        idx_cnpj_classe = classes.index_of("CNPJ_CLASSE")
        if idx_id is not None:
            for linha in classes.rows:
                cnpj = id_para_cnpj.get(cell(linha, idx_id) or "")
                if not cnpj:
                    continue
                info = registro[cnpj]
                publico = cell(linha, idx_publico)
                if publico and publico not in (info["publico_alvo"] or ""):
                    info["publico_alvo"] = (
                        f"{info['publico_alvo']} | {publico}" if info["publico_alvo"] else publico
                    )
                cnpj_classe = norm_cnpj(cell(linha, idx_cnpj_classe))
                if cnpj_classe and cnpj_classe != cnpj:
                    info["cnpjs_classes"].append(cnpj_classe)

    log.info("registro de fundos: %s fundos", len(registro))
    return registro


def carregar_universo(fetch=fetch_bytes) -> tuple[dict[str, dict], bool]:
    """Funde o Registro de Fundos (fonte atual) com o cad_fi (legado).

    O registro vence quando o CNPJ está nos dois; o cad_fi permanece para o
    histórico. Retorna (universo, registro_carregado) — sem o registro o
    levantamento ainda roda, mas enxerga só o passado, e o diagnóstico avisa.
    """
    universo = carregar_cadastro(fetch)
    for info in universo.values():
        info["fonte"] = "cad_fi"
    try:
        registro = carregar_registro(fetch)
    except Exception as exc:
        log.warning("registro de fundos indisponível (%s); seguindo só com cad_fi", exc)
        return universo, False
    for cnpj, info in registro.items():
        info["fonte"] = "registro"
        universo[cnpj] = info
    return universo, True

TERMOS_CONSULTORIA = ("consultoria especializada", "consultor especializado", "taxa de consultoria")
_TERMOS_MONO = ("monocedente", "mono cedente", "cedente unico", "unico cedente", "um unico cedente")
_TERMOS_MULTI = ("multicedente", "multi cedente", "multiplos cedentes", "diversos cedentes")

# Entrada de glossário: "Cedente": ... | "Cedentes" significa(m) ... | Cedente —
# (aspas retas, curvas e guillemets, por código Unicode para não depender do editor)
_ASPAS = "\"'“”‘’«»"
_DEFINICAO_CEDENTE_RE = re.compile(
    "[" + re.escape(_ASPAS) + "]?cedentes?[" + re.escape(_ASPAS) + "]?"
    r"\s*(?::|-|–|—|significam?\b)"
)
# Definição genérica (multicedente): pessoas que cedam/venham a ceder, cadastradas...
_DEF_GENERICA_RE = re.compile(
    r"pessoas?\s+(?:fisicas?\s+(?:ou|e)\s+)?juridicas?|quaisquer|que\s+venham?\s+a\s+ceder"
    r"|que\s+cedam|cadastrad|habilitad|aprovad|elegive"
)

_PCT_RE = re.compile(r"(?<![\d,.])(\d{1,2}(?:[.,]\d{1,4})?)\s*%")
# "0,35% (trinta e cinco centésimos por cento) ao ano": o extenso fica no meio
_ANUAL_RE = re.compile(r"^\s*(?:\([^)]{0,160}\))?\s*(?:a\s*\.?\s*a\b|ao\s+ano)")
_VALOR_RE = re.compile(r"r\$\s*([\d.]{1,14}(?:,\d{2})?)")

JANELA_ANTES = 250
JANELA_DEPOIS = 900
JANELA_DEFINICAO = 450


def _definicoes_cedente(texto: str, texto_norm: str) -> list[tuple[str, str]]:
    """Entradas de glossário para Cedente(s): [(trecho original, trecho norm)]."""
    achados = []
    for match in _DEFINICAO_CEDENTE_RE.finditer(texto_norm):
        fim = min(len(texto_norm), match.end() + JANELA_DEFINICAO)
        achados.append((texto[match.start():fim], texto_norm[match.start():fim]))
        if len(achados) >= 3:  # Cedente, Cedentes e olhe lá; o resto é ruído
            break
    return achados


def classificar_cedencia(texto: str, texto_norm: str) -> dict:
    """Classifica a estrutura de cedência pelo próprio regulamento.

    Dois sinais, nesta ordem de autoridade:
    1. o regulamento se declara ("fundo monocedente" / "multicedente");
    2. a DEFINIÇÃO de Cedente(s) no glossário: cedente nominado com CNPJ é
       estrutura monocedente; definição genérica ("as pessoas jurídicas que
       venham a ceder...", "cadastradas", "quaisquer") é multicedente.

    A definição encontrada sai no resultado para conferência humana — é ela
    que o analista leria para decidir.
    """
    menc_mono = sum(texto_norm.count(t) for t in _TERMOS_MONO)
    menc_multi = sum(texto_norm.count(t) for t in _TERMOS_MULTI)

    definicoes = _definicoes_cedente(texto, texto_norm)
    sinal_definicao = None
    for _, def_norm in definicoes:
        if _CNPJ_RE.search(def_norm):
            sinal_definicao = "monocedente"  # cedente nominado, com CNPJ
            break
        if _DEF_GENERICA_RE.search(def_norm):
            sinal_definicao = "multicedente"

    if menc_mono and not menc_multi:
        classificacao, fonte = "monocedente", "termo explicito no regulamento"
    elif menc_multi and not menc_mono:
        classificacao, fonte = "multicedente", "termo explicito no regulamento"
    elif sinal_definicao:
        classificacao, fonte = sinal_definicao, "definicao de Cedente(s)"
    elif menc_mono and menc_multi:
        classificacao, fonte = "ambiguo", "termos conflitantes"
    else:
        classificacao, fonte = "indeterminado", ""

    definicao_trecho = re.sub(r"\s+", " ", definicoes[0][0]).strip() if definicoes else ""
    return {
        "classificacao": classificacao,
        "fonte": fonte,
        "definicao": definicao_trecho,
        "mencoes_mono": menc_mono,
        "mencoes_multi": menc_multi,
    }


def extrair_consultoria(texto: str, texto_norm: str) -> dict:
    """Percentuais a.a. e mínimos mensais na vizinhança dos termos de consultoria."""
    pct_aa: list[str] = []
    pct_sem_periodo: list[str] = []
    minimos_mensais: list[str] = []
    outros_valores: list[str] = []
    presente = False

    for termo in TERMOS_CONSULTORIA:
        for ocorrencia in re.finditer(re.escape(termo), texto_norm):
            presente = True
            inicio = max(0, ocorrencia.start() - JANELA_ANTES)
            fim = min(len(texto_norm), ocorrencia.end() + JANELA_DEPOIS)
            janela = texto_norm[inicio:fim]

            for m in _PCT_RE.finditer(janela):
                pct = m.group(1)
                destino = pct_aa if _ANUAL_RE.match(janela[m.end():m.end() + 200]) else pct_sem_periodo
                if pct not in destino:
                    destino.append(pct)

            for m in _VALOR_RE.finditer(janela):
                contexto_valor = janela[max(0, m.start() - 110):m.end() + 110]
                eh_minimo = "minimo" in contexto_valor or "minima" in contexto_valor
                eh_mensal = re.search(r"\bmensal|\bmensais|\bpor mes\b|\bao mes\b", contexto_valor)
                valor = m.group(1)
                if eh_minimo and eh_mensal:
                    if valor not in minimos_mensais:
                        minimos_mensais.append(valor)
                elif valor not in outros_valores:
                    outros_valores.append(valor)

    trechos = (
        encontrar_trechos(texto, texto_norm, list(TERMOS_CONSULTORIA), max_trechos=2, contexto=420)
        if presente
        else []
    )
    return {
        "presente": presente,
        "pct_aa": pct_aa,
        "pct_sem_periodo": pct_sem_periodo,
        "minimos_mensais": minimos_mensais,
        "outros_valores": outros_valores,
        "trechos": [t.texto for t in trechos],
    }


def _diagnostico_zero(universo: dict, administrador: str, tipo: str, registro_ok: bool) -> None:
    """Quando o filtro zera, mostra ONDE a expectativa divergiu do dado real."""
    from collections import Counter

    alvo = fold(administrador)
    print("=" * 78)
    print("DIAGNÓSTICO — por que nenhum fundo casou")
    print("=" * 78)
    print(
        "\n0) Registro de Fundos (CVM 175): "
        + ("carregado e fundido ao cad_fi" if registro_ok
           else "INDISPONÍVEL — enxergando só o cad_fi legado (fundos vivos ausentes)")
    )

    por_admin = [
        (cnpj, info) for cnpj, info in universo.items()
        if alvo in fold(info.get("administrador") or "")
    ]
    print(f"\n1) fundos cujo ADMIN contém {administrador!r}: {len(por_admin)}")
    if por_admin:
        combos = Counter(
            (info.get("fonte"), info.get("tipo"), info.get("situacao"))
            for _, info in por_admin
        )
        print("   distribuição (fonte, TIPO, SIT):")
        for (fonte, tp, sit), n in combos.most_common(15):
            print(f"     {n:5}  [{fonte}] tipo={tp!r}  sit={sit!r}")
        print("   exemplos:")
        for cnpj, info in por_admin[:5]:
            print(f"     {cnpj}  {(info.get('denominacao') or '')[:52]!r}")
            print(f"        tipo={info.get('tipo')!r}  sit={info.get('situacao')!r}")
    else:
        aparicoes = [
            (cnpj, info, campo)
            for cnpj, info in universo.items()
            for campo in ("gestor", "denominacao")
            if alvo in fold(info.get(campo) or "")
        ]
        print(f"   ...mas o termo aparece em GESTOR/DENOMINAÇÃO de {len(aparicoes)} fundos:")
        for cnpj, info, campo in aparicoes[:5]:
            print(f"     {cnpj} [{campo}] {(info.get('denominacao') or '')[:46]!r}")
            print(f"        admin={info.get('administrador')!r} tipo={info.get('tipo')!r}")

    tipos = Counter(
        (info.get("fonte"), info.get("tipo")) for info in universo.values()
    )
    print("\n2) tipos mais comuns no universo (o filtro de tipo compara com isto):")
    for (fonte, tp), n in tipos.most_common(15):
        print(f"     {n:6}  [{fonte}] {tp!r}")


def cvm_base() -> str:
    from .cvm_bulk import base_url

    return base_url()


def _regulamento_vigente(client: FnetClient, cnpj: str, id_categoria: int | None, id_tipo: int | None) -> dict | None:
    payload = client.search_documents(
        cnpj=cnpj,
        id_categoria=id_categoria,
        tipo_fundo=id_tipo,
        ordem="desc",
        length=3,
    )
    dados = payload.get("data") or []
    return dados[0] if dados else None


def levantar_taxas(
    administrador: str,
    *,
    tipo: str = "FIDC",
    max_fundos: int = 400,
    apenas_ativos: bool = True,
    saida_csv: str | None = None,
    client: FnetClient | None = None,
    fetch=fetch_bytes,
) -> int:
    print(f"LEVANTAMENTO — consultoria especializada | administrador contém: {administrador!r}")
    print(f"tipo de fundo: {tipo} | somente ativos: {apenas_ativos}\n")

    universo, registro_ok = carregar_universo(fetch)
    alvo_admin = fold(administrador)
    selecionados = {
        cnpj: info
        for cnpj, info in universo.items()
        if alvo_admin in fold(info.get("administrador") or "")
        and _tipo_casa(tipo, info.get("tipo"))
        and (not apenas_ativos or esta_ativo(info.get("situacao")))
    }
    print(f"fundos selecionados (registro + cad_fi): {len(selecionados)}")
    if not selecionados:
        print("nenhum fundo casa com os filtros — diagnóstico abaixo\n")
        _diagnostico_zero(universo, administrador, tipo, registro_ok)
        return 1
    if len(selecionados) > max_fundos:
        print(f"AVISO: limitando aos primeiros {max_fundos} (de {len(selecionados)})")
        selecionados = dict(list(selecionados.items())[:max_fundos])

    client_proprio = client is None
    if client is None:
        client = FnetClient()
    linhas: list[dict] = []
    try:
        id_categoria = resolver_dominio(client, "categoria", "Regulamento")
        id_tipo = resolver_dominio(client, "tipoFundo", tipo)

        for n, (cnpj, info) in enumerate(selecionados.items(), 1):
            linha = {
                "cnpj": cnpj,
                "denominacao": info.get("denominacao"),
                "fonte_cadastro": info.get("fonte", ""),
                "situacao": info.get("situacao"),
                "publico_alvo": info.get("publico_alvo"),
                "gestor": info.get("gestor"),
                "cedencia": "",
                "cedencia_fonte": "",
                "definicao_cedente": "",
                "consultoria_presente": "",
                "taxa_consultoria_pct_aa": "",
                "pct_sem_periodo": "",
                "minimo_mensal_rs": "",
                "outros_valores_rs": "",
                "doc_id": "",
                "url_fnet": "",
                "paginas": "",
                "trecho": "",
                "obs": "",
            }
            linhas.append(linha)
            try:
                doc = _regulamento_vigente(
                    client, cnpj,
                    int(id_categoria) if id_categoria else None,
                    int(id_tipo) if id_tipo else None,
                )
                if doc is None:
                    # pós-CVM 175 as classes têm CNPJ próprio, e há fundos cujo
                    # regulamento está arquivado no FNET sob o CNPJ da classe
                    for cnpj_classe in (info.get("cnpjs_classes") or [])[:2]:
                        doc = _regulamento_vigente(
                            client, cnpj_classe,
                            int(id_categoria) if id_categoria else None,
                            int(id_tipo) if id_tipo else None,
                        )
                        if doc is not None:
                            linha["obs"] = f"regulamento sob o CNPJ da classe {cnpj_classe}"
                            break
                if doc is None:
                    linha["obs"] = "nenhum regulamento no FNET"
                    log.info("[%s/%s] %s — sem regulamento", n, len(selecionados), cnpj)
                    continue
                doc_id = int(doc["id"])
                linha["doc_id"] = doc_id
                linha["url_fnet"] = (
                    f"https://fnet.bmfbovespa.com.br/fnet/publico/downloadDocumento?id={doc_id}"
                )
                conteudo = client.download_document(doc_id)
                extracao = extrair(conteudo, detect_ext(conteudo))
                linha["paginas"] = extracao.num_paginas or ""
                if not extracao.texto:
                    linha["obs"] = "PDF sem camada de texto (precisa de OCR)"
                    log.info("[%s/%s] %s — escaneado", n, len(selecionados), cnpj)
                    continue

                texto_norm = fold(extracao.texto)
                cedencia = classificar_cedencia(extracao.texto, texto_norm)
                linha["cedencia"] = cedencia["classificacao"]
                linha["cedencia_fonte"] = cedencia["fonte"]
                linha["definicao_cedente"] = cedencia["definicao"][:400]
                consultoria = extrair_consultoria(extracao.texto, texto_norm)
                linha["consultoria_presente"] = "sim" if consultoria["presente"] else "nao"
                linha["taxa_consultoria_pct_aa"] = " | ".join(consultoria["pct_aa"])
                linha["pct_sem_periodo"] = " | ".join(consultoria["pct_sem_periodo"])
                linha["minimo_mensal_rs"] = " | ".join(consultoria["minimos_mensais"])
                linha["outros_valores_rs"] = " | ".join(consultoria["outros_valores"][:6])
                linha["trecho"] = " ||| ".join(consultoria["trechos"])
                log.info(
                    "[%s/%s] %s %s | %s | consultoria=%s pct_aa=%s min=%s",
                    n, len(selecionados), cnpj, (info.get("denominacao") or "")[:45],
                    cedencia["classificacao"], linha["consultoria_presente"],
                    linha["taxa_consultoria_pct_aa"] or "-",
                    linha["minimo_mensal_rs"] or "-",
                )
            except Exception as exc:
                linha["obs"] = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("[%s/%s] %s falhou: %s", n, len(selecionados), cnpj, exc)
    finally:
        if client_proprio:
            client.close()

    _escrever_csv(linhas, saida_csv)
    _resumir(linhas)
    return 0 if linhas else 1


def _escrever_csv(linhas: list[dict], caminho: str | None) -> None:
    if not caminho or not linhas:
        return
    with io.open(caminho, "w", encoding="utf-8-sig", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=list(linhas[0]), delimiter=";")
        escritor.writeheader()
        escritor.writerows(linhas)
    print(f"\nCSV completo: {caminho}")


def _resumir(linhas: list[dict]) -> None:
    com_consultoria = [l for l in linhas if l["consultoria_presente"] == "sim"]
    print(f"\n{'=' * 98}")
    print(f"RESUMO — {len(linhas)} fundos examinados | {len(com_consultoria)} com cláusula de consultoria")
    print(f"{'=' * 98}")
    print(f"{'CNPJ':16} {'cedência':14} {'% a.a.':14} {'mín. mensal R$':18} denominação")
    print("-" * 98)
    for linha in linhas:
        if linha["consultoria_presente"] != "sim":
            continue
        print(
            f"{linha['cnpj']:16} {linha['cedencia']:14} "
            f"{(linha['taxa_consultoria_pct_aa'] or '-'):14} "
            f"{(linha['minimo_mensal_rs'] or '-'):18} "
            f"{(linha['denominacao'] or '')[:44]}"
        )
    sem_texto = sum(1 for l in linhas if "OCR" in l["obs"])
    sem_reg = sum(1 for l in linhas if "nenhum regulamento" in l["obs"])
    erros = sum(1 for l in linhas if l["obs"] and "OCR" not in l["obs"] and "nenhum" not in l["obs"])
    print("-" * 98)
    print(f"sem camada de texto (OCR): {sem_texto} | sem regulamento no FNET: {sem_reg} | erros: {erros}")
    print(f"\nGerado em {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC. Os números são")
    print("extração automática: confira o trecho/documento antes de usar.")
