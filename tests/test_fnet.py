import base64
import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from ingestor import fnet_sync
from ingestor.fnet_client import FnetClient, FnetShapeError, detect_ext, maybe_base64
from ingestor.models import Documento, Dominio, Fundo, SyncState
from ingestor.storage import LocalStorage

PDF_BYTES = b"%PDF-1.4 conteudo de regulamento"
XML_BYTES = b"<?xml version='1.0'?><informeMensal/>"

DOCS = [
    {
        "id": 101,
        "descricaoFundo": "ALFA FII - 11.111.111/0001-91",
        "categoriaDocumento": "Regulamento",
        "tipoDocumento": "Regulamento",
        "especieDocumento": "",
        "dataReferencia": "10/06/2026",
        "dataEntrega": "11/06/2026 10:00",
        "situacaoDocumento": "AC",
        "versao": 1,
        "cnpjFundo": "11111111000191",
        "tipoFundo": 1,
    },
    {
        # sem campo cnpjFundo: o CNPJ sai da descrição
        "id": 102,
        "descricaoFundo": "GAMA FIDC AGRO - CNPJ 33.333.333/0001-53",
        "categoriaDocumento": "Informe Mensal Estruturado",
        "tipoDocumento": "Informe Mensal",
        "dataReferencia": "05/2026",
        "dataEntrega": "05/06/2026 09:30",
        "situacaoDocumento": "AC",
        "versao": 2,
        "tipoFundo": 2,
    },
    {
        "id": 103,
        "descricaoFundo": "ALFA FII - 11.111.111/0001-91",
        "categoriaDocumento": "Fato Relevante",
        "tipoDocumento": "Fato Relevante",
        "dataReferencia": "12/06/2026",
        "dataEntrega": "12/06/2026 15:45",
        "situacaoDocumento": "AC",
        "versao": 1,
        "cnpjFundo": "11111111000191",
        "tipoFundo": 1,
    },
]

# Espelha a página real: o placeholder de "todos" em tipoFundo é value=""
# (não 0), e Regulamento é a categoria 5.
FILTER_PAGE = """
<html><body>
<select id="tipoFundo">
  <option value="">Tipo de Fundo</option>
  <option value="1">Fundo Imobiliário</option>
  <option value="2">FIDC</option>
</select>
<select name="idCategoriaDocumento">
  <option value="0">Todos</option>
  <option value="5">Regulamento</option>
</select>
</body></html>
"""


class FnetFake:
    """Reproduz o comportamento observável do FNET num httpx.MockTransport."""

    def __init__(self):
        self.docs = list(DOCS)
        self.requests: list[httpx.Request] = []
        self.fail_tipos: set[str] = set()  # tipos cuja busca responde HTTP 500

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = request.url.params
        if path.endswith("pesquisarGerenciadorDocumentosDados"):
            docs = sorted(self.docs, key=lambda d: d["dataEntrega"])
            # como no FNET real: tipo específico filtra o acervo; vazio devolve
            # só os "documentos do dia" (nenhum, neste mock)
            tf = params.get("tipoFundo", "")
            if tf in self.fail_tipos:
                return httpx.Response(500, text="instabilidade simulada")
            docs = [d for d in docs if str(d.get("tipoFundo")) == tf] if tf else []
            data_inicial = params.get("dataInicial")
            if data_inicial:
                corte = dt.datetime.strptime(data_inicial, "%d/%m/%Y").date()
                docs = [
                    d
                    for d in docs
                    if dt.datetime.strptime(d["dataEntrega"], "%d/%m/%Y %H:%M").date() >= corte
                ]
            start, length = int(params.get("s", 0)), int(params.get("l", 10))
            return httpx.Response(
                200,
                json={
                    "recordsTotal": len(docs),
                    "recordsFiltered": len(docs),
                    "data": docs[start : start + length],
                },
            )
        if path.endswith("downloadDocumento"):
            doc_id = params.get("id")
            if doc_id == "101":  # corpo base64, como o FNET costuma responder
                return httpx.Response(200, text=base64.b64encode(PDF_BYTES).decode())
            if doc_id == "102":  # corpo cru
                return httpx.Response(200, content=XML_BYTES)
            return httpx.Response(404, text="Not Found")
        if path.endswith("abrirGerenciadorDocumentosCVM"):
            return httpx.Response(200, text=FILTER_PAGE)
        return httpx.Response(500, text="unexpected")


@pytest.fixture()
def fnet():
    return FnetFake()


@pytest.fixture()
def client(fnet):
    return FnetClient(base_url="https://fnet.test/fnet/publico", min_interval=0, retries=0,
                      transport=fnet.transport())


def test_detect_ext_and_base64():
    assert detect_ext(PDF_BYTES) == "pdf"
    assert detect_ext(XML_BYTES) == "xml"
    assert detect_ext(b"PK\x03\x04zipzip") == "zip"
    assert detect_ext(b"qualquer coisa") == "bin"
    assert maybe_base64(base64.b64encode(PDF_BYTES)) == PDF_BYTES
    assert maybe_base64(PDF_BYTES) == PDF_BYTES  # já decodificado, não mexe
    assert maybe_base64(b"texto que nao e base64!") == b"texto que nao e base64!"


def test_iter_documents_pagina(client):
    docs = list(client.iter_documents(page_size=1, tipo_fundo=1))
    assert [d["id"] for d in docs] == [101, 103]  # ordenado por dataEntrega asc


def test_iter_documents_sem_tipo_nao_percorre_o_acervo(client):
    """Comportamento real do FNET: tipoFundo vazio só traz os docs recentes."""
    assert list(client.iter_documents(page_size=2)) == []


def test_listar_tipos_fundo_ignora_placeholder(client):
    assert client.listar_tipos_fundo() == [(1, "Fundo Imobiliário"), (2, "FIDC")]


def test_search_shape_error():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<html>bloqueado"))
    bad = FnetClient(base_url="https://fnet.test/x", min_interval=0, retries=0, transport=transport)
    with pytest.raises(FnetShapeError):
        bad.search_documents()


def test_sync_documentos(session, client):
    # sem tipo informado, varre um tipo por vez (vazio não percorre o acervo)
    stats = fnet_sync.sync_documentos(session, client, page_size=2)
    session.commit()
    assert stats["documentos"] == 3

    reg = session.get(Documento, 101)
    assert reg.categoria == "Regulamento"
    assert reg.cnpj == "11111111000191"
    assert reg.data_entrega == dt.datetime(2026, 6, 11, 10, 0)
    assert reg.competencia == dt.date(2026, 6, 1)
    assert reg.status_download == "pendente"

    informe = session.get(Documento, 102)
    assert informe.cnpj == "33333333000153"  # extraído da descrição
    assert informe.competencia == dt.date(2026, 5, 1)

    # fundos mínimos criados para a FK
    assert session.get(Fundo, "11111111000191") is not None

    # cursor próprio por tipo de fundo
    assert session.get(SyncState, "fnet_documentos:tf=1:cat=all").cursor == "2026-06-12T15:45:00"
    assert session.get(SyncState, "fnet_documentos:tf=2:cat=all").cursor == "2026-06-05T09:30:00"


def test_sync_incremental_preserva_download(session, client, fnet):
    fnet_sync.sync_documentos(session, client)
    session.commit()

    # simula um download já feito
    doc = session.get(Documento, 101)
    doc.status_download = "baixado"
    doc.url_storage = "/algum/lugar/101.pdf"
    session.commit()

    # novo documento chega no FNET
    fnet.docs.append(
        {
            "id": 104,
            "descricaoFundo": "ALFA FII - 11.111.111/0001-91",
            "categoriaDocumento": "Assembleia",
            "dataReferencia": "20/06/2026",
            "dataEntrega": "20/06/2026 12:00",
            "versao": 1,
            "cnpjFundo": "11111111000191",
            "tipoFundo": 1,
        }
    )
    fnet_sync.sync_documentos(session, client)
    session.commit()

    todos = session.execute(select(Documento)).scalars().all()
    assert {d.id_fnet for d in todos} == {101, 102, 103, 104}
    # o cursor do tipo 1 limitou a busca: dataInicial = cursor (12/06) - 1 dia
    buscas_tf1 = [
        r
        for r in fnet.requests
        if "pesquisar" in r.url.path
        and r.url.params.get("tipoFundo") == "1"
        and "dataInicial" in r.url.params
    ]
    assert buscas_tf1[-1].url.params["dataInicial"] == "11/06/2026"
    # e o re-sync do 101 não desfez o download
    doc = session.get(Documento, 101)
    assert doc.status_download == "baixado"
    assert doc.url_storage == "/algum/lugar/101.pdf"


def test_download_documentos(session, client, tmp_path):
    fnet_sync.sync_documentos(session, client)
    session.commit()
    storage = LocalStorage(tmp_path / "docs")

    stats = fnet_sync.download_documentos(
        session, client, storage, categorias=["Regulamento"], limite=10
    )
    session.commit()
    assert stats == {"baixados": 1, "erros": 0}

    reg = session.get(Documento, 101)
    assert reg.status_download == "baixado"
    assert reg.formato == "pdf"
    with open(reg.url_storage, "rb") as fh:
        assert fh.read() == PDF_BYTES  # base64 foi decodificado

    # os demais continuam pendentes (filtro por categoria respeitado)
    assert session.get(Documento, 102).status_download == "pendente"

    # sem filtro: 102 baixa (xml cru), 103 dá 404 -> erro
    stats = fnet_sync.download_documentos(session, client, storage, limite=10)
    session.commit()
    assert stats == {"baixados": 1, "erros": 1}
    assert session.get(Documento, 102).formato == "xml"
    assert session.get(Documento, 103).status_download == "erro"


def test_sync_dominios(session, client):
    stats = fnet_sync.sync_dominios(session, client)
    session.commit()
    assert stats == {"grupos": 2, "opcoes": 5}
    fii = session.get(Dominio, ("tipoFundo", "1"))
    assert fii.rotulo == "Fundo Imobiliário"
    assert session.get(Dominio, ("idCategoriaDocumento", "5")).rotulo == "Regulamento"
    # idempotente
    fnet_sync.sync_dominios(session, client)
    session.commit()
    assert len(session.execute(select(Dominio)).scalars().all()) == 5


# --- achados do teste de fumaça contra o FNET real (2026-07-29) ----------------


def test_tipo_fundo_todos_vai_vazio_nao_zero(client, fnet):
    """No FNET, 'todos' em tipoFundo é string vazia; 0 filtra por tipo inexistente.

    Foi assim que a busca voltou recordsTotal=0 na primeira execução real.
    """
    client.search_documents(length=1)
    params = fnet.requests[-1].url.params
    assert params["tipoFundo"] == ""
    # os demais filtros, ao contrário, usam 0 como "todos" (conforme os <option>)
    assert params["idCategoriaDocumento"] == "0"
    assert params["idTipoDocumento"] == "0"
    assert params["idEspecieDocumento"] == "0"


def test_tipo_fundo_especifico_e_enviado(client, fnet):
    client.search_documents(length=1, tipo_fundo=11)  # FIAGRO
    assert fnet.requests[-1].url.params["tipoFundo"] == "11"


def test_sync_persiste_progresso_quando_um_tipo_falha(session, client, fnet):
    """Achado da revisão adversarial: sem commit por tipo, uma falha no tipo k
    descartava documentos E cursores de todos os tipos já varridos."""
    fnet.fail_tipos = {"2"}
    with pytest.raises(RuntimeError, match="tipoFundo=2"):
        fnet_sync.sync_documentos(session, client)

    # o tipo 1, varrido antes da falha, ficou persistido (commit por tipo)
    ids = {d.id_fnet for d in session.execute(select(Documento)).scalars()}
    assert ids == {101, 103}
    assert session.get(SyncState, "fnet_documentos:tf=1:cat=all").cursor == "2026-06-12T15:45:00"
    assert session.get(SyncState, "fnet_documentos:tf=2:cat=all") is None

    # na execução seguinte, com o tipo 2 recuperado, só falta o que faltava
    fnet.fail_tipos = set()
    fnet_sync.sync_documentos(session, client)
    session.commit()
    ids = {d.id_fnet for d in session.execute(select(Documento)).scalars()}
    assert ids == {101, 102, 103}


def test_cursor_legado_tf_all_e_aproveitado(session, client, fnet):
    """Bancos da era pré-varredura-por-tipo não podem regredir a backfill do zero."""
    session.add(SyncState(fonte="fnet_documentos:tf=all:cat=all", cursor="2026-06-12T15:45:00"))
    session.commit()

    fnet_sync.sync_documentos(session, client)
    buscas = [r for r in fnet.requests if "pesquisar" in r.url.path]
    assert buscas, "nenhuma busca executada"
    # todos os tipos herdaram o cursor legado (12/06 - 1 dia de sobreposição)
    assert all(r.url.params.get("dataInicial") == "11/06/2026" for r in buscas)
    # e os cursores novos por tipo passaram a existir
    assert session.get(SyncState, "fnet_documentos:tf=1:cat=all") is not None


def test_competencia_aceita_data_referencia_com_hora(session, client, fnet):
    """Formato real observado em atas: dataReferencia '29/07/2026 13:00'."""
    fnet.docs.append(
        {
            "id": 105,
            "descricaoFundo": "ALFA FII - 11.111.111/0001-91",
            "categoriaDocumento": "Assembleia",
            "dataReferencia": "29/07/2026 13:00",
            "dataEntrega": "29/07/2026 17:20",
            "versao": 1,
            "cnpjFundo": "11111111000191",
            "tipoFundo": 1,
        }
    )
    fnet_sync.sync_documentos(session, client, tipo_fundo=1)
    session.commit()
    doc = session.get(Documento, 105)
    assert doc.competencia == dt.date(2026, 7, 1)
    assert doc.data_referencia == "29/07/2026 13:00"  # string original preservada
