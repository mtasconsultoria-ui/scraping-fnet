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
    },
]

FILTER_PAGE = """
<html><body>
<select id="tipoFundo">
  <option value="0">Todos</option>
  <option value="1">FII</option>
  <option value="2">FIDC</option>
</select>
<select name="idCategoriaDocumento">
  <option value="0">Todas</option>
  <option value="9">Regulamento</option>
</select>
</body></html>
"""


class FnetFake:
    """Reproduz o comportamento observável do FNET num httpx.MockTransport."""

    def __init__(self):
        self.docs = list(DOCS)
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = request.url.params
        if path.endswith("pesquisarGerenciadorDocumentosDados"):
            docs = sorted(self.docs, key=lambda d: d["dataEntrega"])
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
    docs = list(client.iter_documents(page_size=2))
    assert [d["id"] for d in docs] == [102, 101, 103]  # ordenado por dataEntrega asc


def test_search_shape_error():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<html>bloqueado"))
    bad = FnetClient(base_url="https://fnet.test/x", min_interval=0, retries=0, transport=transport)
    with pytest.raises(FnetShapeError):
        bad.search_documents()


def test_sync_documentos(session, client):
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

    state = session.get(SyncState, "fnet_documentos:tf=all:cat=all")
    assert state.cursor == "2026-06-12T15:45:00"


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
        }
    )
    fnet_sync.sync_documentos(session, client)
    session.commit()

    todos = session.execute(select(Documento)).scalars().all()
    assert {d.id_fnet for d in todos} == {101, 102, 103, 104}
    # o cursor limitou a busca: só docs de 11/06 em diante voltaram na 2ª chamada
    ultima_busca = [r for r in fnet.requests if "pesquisar" in r.url.path][-1]
    assert ultima_busca.url.params["dataInicial"] == "11/06/2026"
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
    assert fii.rotulo == "FII"
    # idempotente
    fnet_sync.sync_dominios(session, client)
    session.commit()
    assert len(session.execute(select(Dominio)).scalars().all()) == 5
