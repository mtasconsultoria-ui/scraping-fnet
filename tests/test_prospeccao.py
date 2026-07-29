"""Prospecção de ponta a ponta contra um FNET simulado com o comportamento real:
tipoFundo vazio não percorre o acervo, metadados muitas vezes sem CNPJ, e o
filtro de categoria aplicado pelo id resolvido da página."""
import httpx
import pytest

from conftest import FakeFetch
from ingestor.fnet_client import FnetClient
from ingestor.prospeccao import prospectar

# CNPJs com dígitos verificadores VÁLIDOS: a extração de CNPJ do texto valida DV
CNPJ_GAMA = "11222333000181"
CNPJ_ADMIN = "11444777000161"  # válido, mas de administrador: não está no cad_fi

CAD_FI = """TP_FUNDO;CNPJ_FUNDO;DENOM_SOCIAL;DT_REG;SIT;PUBLICO_ALVO;ADMIN;GESTOR;CLASSE
FIDC;11.222.333/0001-81;GAMA FIDC AGRO;2019-05-20;EM FUNCIONAMENTO NORMAL;Investidores Profissionais;ADMIN C;GESTOR C;FIDC
FIDC;55.555.555/0001-15;DELTA FIDC ENCERRADO;2010-01-01;CANCELADA;Investidores Qualificados;ADMIN D;GESTOR D;FIDC
""".encode("latin-1")

FILTER_PAGE = """
<select id="tipoFundo">
  <option value="">Tipo de Fundo</option>
  <option value="1">Fundo Imobiliário</option>
  <option value="2">FIDC</option>
</select>
<select name="idCategoriaDocumento">
  <option value="0">Todos</option>
  <option value="5">Regulamento</option>
</select>
"""

# 201: sem CNPJ nos metadados -> extraído do texto do documento
# 202: com CNPJ de fundo CANCELADO -> barrado ANTES do download
# 203: categoria diferente -> filtrado pelo id da categoria
DOCS = [
    {"id": 201, "descricaoFundo": "GAMA FIDC AGRO", "categoriaDocumento": "Regulamento",
     "dataReferencia": "10/06/2026", "dataEntrega": "10/06/2026 10:00", "versao": 1,
     "tipoFundo": 2},
    {"id": 202, "descricaoFundo": "DELTA FIDC ENCERRADO", "categoriaDocumento": "Regulamento",
     "dataReferencia": "09/06/2026", "dataEntrega": "09/06/2026 10:00", "versao": 1,
     "cnpjFundo": "55555555000115", "tipoFundo": 2},
    {"id": 203, "descricaoFundo": "GAMA FIDC AGRO", "categoriaDocumento": "Fato Relevante",
     "dataReferencia": "08/06/2026", "dataEntrega": "08/06/2026 10:00", "versao": 1,
     "tipoFundo": 2},
]

# O preâmbulo reproduz as armadilhas reais: número de protocolo com cara de
# CNPJ no miolo, e o CNPJ do ADMINISTRADOR citado antes do CNPJ do fundo.
REGULAMENTO_GAMA = (
    "<?xml version='1.0'?><regulamento>Protocolo FNET 202607290001234567. "
    "Administrado por BANCO ADMIN S.A., CNPJ 11.444.777/0001-61. "
    "GAMA FIDC AGRO, CNPJ 11.222.333/0001-81. "
    "Art. 5 - O fundo podera adquirir CDCA (Certificado de Deposito de Creditos "
    "do Agronegocio) e duplicatas.</regulamento>"
).encode()

CATEGORIA_IDS = {"5": "Regulamento"}


class ProspeccaoFake:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def client(self) -> FnetClient:
        return FnetClient(
            base_url="https://fnet.test/fnet/publico",
            min_interval=0,
            retries=0,
            transport=httpx.MockTransport(self.handler),
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, request.url.params
        if path.endswith("pesquisarGerenciadorDocumentosDados"):
            tf = params.get("tipoFundo", "")
            docs = [d for d in DOCS if str(d["tipoFundo"]) == tf] if tf else []
            cat = params.get("idCategoriaDocumento", "0")
            if cat != "0":
                docs = [d for d in docs if d["categoriaDocumento"] == CATEGORIA_IDS.get(cat)]
            docs = sorted(docs, key=lambda d: d["dataEntrega"], reverse=True)
            return httpx.Response(
                200, json={"recordsTotal": len(docs), "recordsFiltered": len(docs), "data": docs}
            )
        if path.endswith("downloadDocumento"):
            if params.get("id") == "201":
                return httpx.Response(200, content=REGULAMENTO_GAMA)
            return httpx.Response(200, content=b"<?xml version='1.0'?><outro/>")
        if path.endswith("abrirGerenciadorDocumentosCVM"):
            return httpx.Response(200, text=FILTER_PAGE)
        return httpx.Response(500)


@pytest.fixture()
def fake():
    return ProspeccaoFake()


def test_prospeccao_ponta_a_ponta(fake, capsys):
    codigo = prospectar(
        ["CDCA", "Certificado de Depósito de Créditos do Agronegócio"],
        alvo=5,
        client=fake.client(),
        fetch=FakeFetch({"cad_fi.csv": CAD_FI}),
    )
    saida = capsys.readouterr().out
    assert codigo == 0
    assert "1 de 5 fundo(s) encontrados" in saida
    # CNPJ veio do texto do documento (metadados não traziam) e as defesas
    # funcionaram: nem o protocolo, nem o CNPJ do administrador foram usados
    assert "GAMA FIDC AGRO" in saida
    assert CNPJ_GAMA in saida
    assert CNPJ_ADMIN not in saida
    assert "EM FUNCIONAMENTO NORMAL" in saida
    assert "«CDCA»" in saida
    assert "downloadDocumento?id=201" in saida

    # o fundo cancelado foi barrado ANTES do download (nenhuma requisição p/ 202)
    baixados = [r.url.params.get("id") for r in fake.requests if "download" in r.url.path]
    assert "202" not in baixados
    # a categoria foi filtrada pelo id (203 nunca apareceu na busca nem baixou)
    assert "203" not in baixados


def test_prospeccao_tipo_inexistente(fake, capsys):
    codigo = prospectar(
        ["CDCA"],
        tipo_fundo="FIAGRO",  # não existe no mock
        client=fake.client(),
        fetch=FakeFetch({"cad_fi.csv": CAD_FI}),
    )
    assert codigo == 1
    saida = capsys.readouterr().out
    assert "não existe no FNET" in saida
    assert "FIDC" in saida  # lista as opções disponíveis


def test_prospeccao_termo_ausente(fake, capsys):
    codigo = prospectar(
        ["debênture perpétua"],
        client=fake.client(),
        fetch=FakeFetch({"cad_fi.csv": CAD_FI}),
    )
    assert codigo == 1
    assert "0 de 5 fundo(s) encontrados" in capsys.readouterr().out
