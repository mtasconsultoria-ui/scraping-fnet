"""Levantamento de taxas de consultoria, de ponta a ponta contra mocks.

Os regulamentos simulados reproduzem as duas formas reais da definição de
Cedentes: nominada com CNPJ (estrutura monocedente) e genérica (multicedente).
"""
import httpx
import pytest

from conftest import FakeFetch, make_zip
from ingestor.fnet_client import FnetClient
from ingestor.levantamento import classificar_cedencia, extrair_consultoria, levantar_taxas
from ingestor.textos import fold, normalize_nfc

# CNPJs com DV válido
CNPJ_MONO = "11222333000181"
CNPJ_MULTI = "11444777000161"

# Realidade constatada em produção: o cad_fi legado só tem os fundos MORTOS do
# administrador; os vivos estão no Registro de Fundos (CVM 175).
CAD_FI = """TP_FUNDO;CNPJ_FUNDO;DENOM_SOCIAL;DT_REG;SIT;PUBLICO_ALVO;ADMIN;GESTOR;CLASSE
FIDC;55.555.555/0001-15;ENCERRADO FIDC;2015-03-30;CANCELADA;Investidores Qualificados;LIMINE TRUST DTVM LTDA;GESTORA C;FIDC
FII;33.333.333/0001-53;LIMINE SHOPPING FII;2018-05-05;EM FUNCIONAMENTO NORMAL;Investidores em Geral;LIMINE TRUST DTVM LTDA;GESTORA E;
""".encode("latin-1")

REGISTRO_FUNDO = """ID_Registro_Fundo;CNPJ_Fundo;Tipo_Fundo;Denominacao_Social;Situacao;Administrador;Gestor
10;11.222.333/0001-81;FIDC;AGRO FORTE FIDC;EM FUNCIONAMENTO NORMAL;LIMINE TRUST DTVM LTDA;GESTORA A
20;11.444.777/0001-61;FIDC;VAREJO AMPLO FIDC;EM FUNCIONAMENTO NORMAL;LIMINE TRUST DTVM LTDA;GESTORA B
30;22.222.222/0001-72;FIDC;OUTRO ADMIN FIDC;EM FUNCIONAMENTO NORMAL;BANCO QUALQUER S.A.;GESTORA D
40;44.444.444/0001-34;FII;LIMINE PREDIOS FII;EM FUNCIONAMENTO NORMAL;LIMINE TRUST DTVM LTDA;GESTORA E
""".encode("latin-1")

REGISTRO_CLASSE = """ID_Registro_Fundo;ID_Registro_Classe;CNPJ_Classe;Denominacao_Social;Publico_Alvo
10;101;11.999.888/0001-55;AGRO FORTE CLASSE UNICA;Investidores Profissionais
20;201;;VAREJO AMPLO SENIOR;Investidores Qualificados
20;202;;VAREJO AMPLO SUB;Investidores Profissionais
""".encode("latin-1")

REGISTRO_ZIP = make_zip(
    {"registro_fundo.csv": REGISTRO_FUNDO, "registro_classe.csv": REGISTRO_CLASSE}
)

FILTER_PAGE = """
<select id="tipoFundo">
  <option value="">Tipo de Fundo</option>
  <option value="2">FIDC</option>
</select>
<select name="idCategoriaDocumento">
  <option value="0">Todos</option>
  <option value="5">Regulamento</option>
</select>
"""

REG_MONO = normalize_nfc(
    "<?xml version='1.0'?><reg>REGULAMENTO DO AGRO FORTE FIDC. CAPITULO II - DEFINICOES. "
    '"Cedente": AGRO FORTE COMERCIO DE INSUMOS S.A., sociedade anonima inscrita no '
    "CNPJ/ME sob o no 44.555.666/0001-77, unica cedente dos Direitos Creditorios. "
    "CAPITULO X - ENCARGOS. A consultoria especializada fara jus a remuneracao de "
    "0,35% (trinta e cinco centesimos por cento) ao ano sobre o patrimonio liquido do "
    "Fundo, observado o valor minimo mensal de R$ 15.000,00 (quinze mil reais), alem "
    "da taxa de administracao de 0,20% a.a. com minimo mensal de R$ 8.000,00.</reg>"
).encode()

REG_MULTI = normalize_nfc(
    "<?xml version='1.0'?><reg>REGULAMENTO DO VAREJO AMPLO FIDC. DEFINICOES. "
    '"Cedentes": as pessoas juridicas que venham a ceder Direitos Creditorios ao '
    "Fundo, devidamente cadastradas e aprovadas pela Gestora. ENCARGOS. O Fundo nao "
    "contara com consultoria especializada.</reg>"
).encode()


class LevantamentoFake:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def client(self) -> FnetClient:
        return FnetClient(
            base_url="https://fnet.test/fnet/publico", min_interval=0, retries=0,
            transport=httpx.MockTransport(self.handler),
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, request.url.params
        if path.endswith("pesquisarGerenciadorDocumentosDados"):
            cnpj = params.get("cnpjFundo", "")
            docs = {
                CNPJ_MONO: [{"id": 301, "categoriaDocumento": "Regulamento",
                             "dataReferencia": "01/06/2026", "dataEntrega": "02/06/2026 10:00"}],
                CNPJ_MULTI: [{"id": 302, "categoriaDocumento": "Regulamento",
                              "dataReferencia": "01/05/2026", "dataEntrega": "02/05/2026 10:00"}],
            }.get(cnpj, [])
            return httpx.Response(
                200, json={"recordsTotal": len(docs), "recordsFiltered": len(docs), "data": docs}
            )
        if path.endswith("downloadDocumento"):
            corpo = {"301": REG_MONO, "302": REG_MULTI}.get(params.get("id", ""))
            return httpx.Response(200, content=corpo or b"nada")
        if path.endswith("abrirGerenciadorDocumentosCVM"):
            return httpx.Response(200, text=FILTER_PAGE)
        return httpx.Response(500)


def test_classifica_pela_definicao_de_cedente():
    texto = normalize_nfc(
        'DEFINICOES: "Cedente": AGRO FORTE S.A., inscrita no CNPJ sob o no '
        "44.555.666/0001-77, na qualidade de cedente dos Direitos Creditorios."
    )
    resultado = classificar_cedencia(texto, fold(texto))
    assert resultado["classificacao"] == "monocedente"
    assert resultado["fonte"] == "definicao de Cedente(s)"
    assert "44.555.666/0001-77" in resultado["definicao"]

    texto = normalize_nfc(
        'DEFINICOES: "Cedentes": as pessoas juridicas que venham a ceder Direitos '
        "Creditorios ao Fundo, devidamente cadastradas junto ao Custodiante."
    )
    resultado = classificar_cedencia(texto, fold(texto))
    assert resultado["classificacao"] == "multicedente"
    assert resultado["fonte"] == "definicao de Cedente(s)"


def test_classifica_com_aspas_curvas():
    """Regulamentos reais usam aspas tipográficas na entrada do glossário."""
    texto = normalize_nfc(
        "DEFINICOES: “Cedente”: AGRO FORTE S.A., inscrita no CNPJ sob o no "
        "44.555.666/0001-77."
    )
    assert classificar_cedencia(texto, fold(texto))["classificacao"] == "monocedente"


def test_termo_explicito_tem_precedencia():
    texto = normalize_nfc(
        'Fundo multicedente e multissacado. "Cedentes": as pessoas juridicas '
        "cadastradas que venham a ceder Direitos Creditorios."
    )
    resultado = classificar_cedencia(texto, fold(texto))
    assert resultado["classificacao"] == "multicedente"
    assert resultado["fonte"] == "termo explicito no regulamento"


def test_sem_sinais_fica_indeterminado():
    texto = "Regulamento sem glossario e sem mencao a estrutura de cedencia."
    assert classificar_cedencia(texto, fold(texto))["classificacao"] == "indeterminado"


def test_extrai_pct_aa_e_minimo_mensal():
    texto = normalize_nfc(
        "A consultoria especializada fara jus a remuneracao de 0,35% (trinta e cinco "
        "centesimos por cento) ao ano, observado o valor minimo mensal de R$ 15.000,00 "
        "(quinze mil reais), pago com recursos do Fundo."
    )
    resultado = extrair_consultoria(texto, fold(texto))
    assert resultado["presente"] is True
    assert resultado["pct_aa"] == ["0,35"]
    assert resultado["minimos_mensais"] == ["15.000,00"]
    assert resultado["trechos"]


def test_sem_clausula_de_consultoria():
    texto = "O Fundo tera taxa de administracao de 0,20% ao ano."
    resultado = extrair_consultoria(texto, fold(texto))
    assert resultado["presente"] is False
    assert resultado["pct_aa"] == []


def test_levantamento_ponta_a_ponta(fake_levantamento, capsys, tmp_path):
    csv_path = tmp_path / "saida.csv"
    codigo = levantar_taxas(
        "LIMINE",
        saida_csv=str(csv_path),
        client=fake_levantamento.client(),
        fetch=FakeFetch({
            "cad_fi.csv": CAD_FI,
            "registro_fundo_classe.zip": REGISTRO_ZIP,
        }),
    )
    assert codigo == 0
    saida = capsys.readouterr().out

    # só os 2 FIDCs ATIVOS da LIMINE, vindos do REGISTRO: o cancelado do
    # cad_fi, o FII e o FIDC de outro administrador ficam fora
    assert "fundos selecionados (registro + cad_fi): 2" in saida
    buscas = [r.url.params.get("cnpjFundo") for r in fake_levantamento.requests
              if "pesquisar" in r.url.path]
    assert "55555555000115" not in buscas  # cancelado nunca foi consultado

    import csv as csv_mod

    with open(csv_path, encoding="utf-8-sig") as fh:
        linhas = {l["cnpj"]: l for l in csv_mod.DictReader(fh, delimiter=";")}
    assert set(linhas) == {CNPJ_MONO, CNPJ_MULTI}

    mono = linhas[CNPJ_MONO]
    assert mono["fonte_cadastro"] == "registro"
    assert mono["publico_alvo"] == "Investidores Profissionais"  # veio da classe
    assert mono["cedencia"] == "monocedente"
    assert mono["cedencia_fonte"] == "definicao de Cedente(s)"
    assert "44.555.666/0001-77" in mono["definicao_cedente"]
    assert mono["consultoria_presente"] == "sim"
    assert "0,35" in mono["taxa_consultoria_pct_aa"]
    assert "15.000,00" in mono["minimo_mensal_rs"]
    assert mono["url_fnet"].endswith("id=301")

    multi = linhas[CNPJ_MULTI]
    assert multi["cedencia"] == "multicedente"
    assert "Qualificados | Investidores Profissionais" in multi["publico_alvo"]
    # menciona o termo, mas para dizer que NÃO contará com consultoria;
    # o trecho no CSV é o que permite ao analista ver isso
    assert multi["consultoria_presente"] == "sim"
    assert multi["taxa_consultoria_pct_aa"] == ""
    assert "nao contara com consultoria" in fold(multi["trecho"])


def test_levantamento_usa_url_de_contingencia(fake_levantamento, capsys):
    """Se o zip principal der 404, a variante com underscore é tentada."""
    codigo = levantar_taxas(
        "LIMINE",
        client=fake_levantamento.client(),
        fetch=FakeFetch({
            "cad_fi.csv": CAD_FI,
            "registro_fundo_classe.zip": RuntimeError("404"),
            "registro_fundo_classe_.zip": REGISTRO_ZIP,
        }),
    )
    assert codigo == 0
    assert "fundos selecionados (registro + cad_fi): 2" in capsys.readouterr().out


def test_levantamento_sem_registro_cai_no_diagnostico(fake_levantamento, capsys):
    """Sem o registro, só o legado existe — e o diagnóstico avisa isso."""
    codigo = levantar_taxas(
        "LIMINE",
        client=fake_levantamento.client(),
        fetch=FakeFetch({
            "cad_fi.csv": CAD_FI,
            "registro_fundo_classe.zip": RuntimeError("fora do ar"),
            "registro_fundo_classe_.zip": RuntimeError("fora do ar"),
        }),
    )
    saida = capsys.readouterr().out
    assert codigo == 1  # só há LIMINE cancelada/FII no legado
    assert "INDISPONÍVEL" in saida
    assert "[cad_fi] tipo='FIDC'  sit='CANCELADA'" in saida


def test_tipo_casa_sigla_e_por_extenso():
    from ingestor.levantamento import _tipo_casa

    assert _tipo_casa("FIDC", "FIDC")
    assert _tipo_casa("FIDC", "FIDC-NP")
    # o cad_fi já alternou entre sigla e nome por extenso entre safras
    assert _tipo_casa("FIDC", "FUNDO DE INVESTIMENTO EM DIREITOS CREDITÓRIOS")
    assert not _tipo_casa("FIDC", "FII")
    assert not _tipo_casa("FIDC", None)


def test_esta_ativo_tolerante():
    from ingestor.prospeccao import esta_ativo

    assert esta_ativo("EM FUNCIONAMENTO NORMAL")
    assert esta_ativo("Em Funcionamento Normal")
    assert esta_ativo("FUNCIONAMENTO NORMAL")
    assert not esta_ativo("CANCELADA")
    assert not esta_ativo("EM LIQUIDACAO")
    assert not esta_ativo(None)


def test_diagnostico_quando_zero(capsys):
    """Com 0 selecionados, o comando explica o porquê em vez de só falhar."""
    # LIMINE existe no registro, mas em situação que não é de funcionamento
    registro = make_zip({
        "registro_fundo.csv": (
            "ID_Registro_Fundo;CNPJ_Fundo;Tipo_Fundo;Denominacao_Social;Situacao;Administrador;Gestor\n"
            "10;11.222.333/0001-81;FIDC;ALGUM FIDC;PRE-OPERACIONAL;LIMINE TRUST DTVM;G\n"
        ).encode("latin-1"),
    })
    cad = """TP_FUNDO;CNPJ_FUNDO;DENOM_SOCIAL;DT_REG;SIT;PUBLICO_ALVO;ADMIN;GESTOR;CLASSE
FIDC;55.555.555/0001-15;VELHO FIDC;2015-01-10;CANCELADA;Prof;LIMINE TRUST DTVM;G;
""".encode("latin-1")
    codigo = levantar_taxas(
        "LIMINE",
        client=None,  # nunca chega ao FNET
        fetch=FakeFetch({"cad_fi.csv": cad, "registro_fundo_classe.zip": registro}),
    )
    saida = capsys.readouterr().out
    assert codigo == 1
    assert "DIAGNÓSTICO" in saida
    assert "carregado e fundido" in saida
    assert "fundos cujo ADMIN contém 'LIMINE': 2" in saida
    assert "[registro] tipo='FIDC'  sit='PRE-OPERACIONAL'" in saida
    assert "[cad_fi] tipo='FIDC'  sit='CANCELADA'" in saida


@pytest.fixture()
def fake_levantamento():
    return LevantamentoFake()
