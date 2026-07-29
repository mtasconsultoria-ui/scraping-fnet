"""Fase 3 de ponta a ponta: PDF -> extração -> busca com filtros -> trechos."""
import datetime as dt
from decimal import Decimal

from pdf_fixture import make_pdf
from sqlalchemy import select

from ingestor import text_extract
from ingestor.busca import Filtros, buscar
from ingestor.models import Documento, DocumentoTexto, Fundo, FundoMetricas
from ingestor.storage import LocalStorage
from ingestor.text_extract import extrair, extrair_pdf, extrair_textos

PREAMBULO = (
    "REGULAMENTO DO FUNDO DE INVESTIMENTO. "
    "Capitulo I - Do Fundo, sua denominacao e prazo de duracao. "
    "O Fundo e constituido sob a forma de condominio fechado, regido por este "
    "regulamento e pelas disposicoes legais aplicaveis. "
) * 3

# Fundo A: permite CPR-F expressamente (quebra de linha no meio do termo longo)
PDF_COM_CPRF = make_pdf(
    [
        PREAMBULO[:90],
        PREAMBULO[90:180],
        "Art. 5o - A carteira podera ser composta por CPR-F (Cedula do",
        "Produto Rural Financeira), CRA e outros ativos do agronegocio,",
        "observados os limites de concentracao previstos neste regulamento.",
        PREAMBULO[180:270],
    ]
)

# Fundo B: menciona CPR (sem o -F) — o prefiltro LIKE pega, o regex descarta
PDF_SEM_CPRF = make_pdf(
    [
        PREAMBULO[:90],
        PREAMBULO[90:180],
        "Art. 5o - A carteira podera ser composta por CPR fisica e por",
        "duplicatas do agronegocio, vedada a aquisicao de derivativos.",
        PREAMBULO[180:270],
    ]
)

CNPJ_A = "11111111000191"
CNPJ_B = "22222222000172"


def _monta_universo(session, tmp_path):
    """Dois fundos com regulamento baixado no storage local."""
    storage = LocalStorage(tmp_path / "docs")
    dados = [
        (CNPJ_A, "ALFA FIAGRO", "FIAGRO", "Investidores Qualificados", 501, PDF_COM_CPRF,
         Decimal("250000000.00"), 120),
        (CNPJ_B, "BETA FIDC AGRO", "FIDC", "Investidores Profissionais", 502, PDF_SEM_CPRF,
         Decimal("80000000.00"), 15),
    ]
    for cnpj, nome, tipo, publico, doc_id, pdf, pl, cotistas in dados:
        session.add(
            Fundo(
                cnpj=cnpj,
                denominacao=nome,
                tipo_veiculo=tipo,
                publico_alvo=publico,
                situacao="EM FUNCIONAMENTO NORMAL",
                administrador=f"ADMIN {nome[0]}",
            )
        )
        session.add(
            FundoMetricas(
                cnpj=cnpj,
                competencia_ultima=dt.date(2026, 5, 1),
                pl_atual=pl,
                pl_medio_12m=pl,
                cotistas_atual=cotistas,
            )
        )
        session.add(
            Documento(
                id_fnet=doc_id,
                cnpj=cnpj,
                denominacao_fundo=nome,
                categoria="Regulamento",
                tipo="Regulamento",
                data_referencia="10/06/2026",
                data_entrega=dt.datetime(2026, 6, 11, 10, 0),
                situacao="AC",
                versao=1,
                status_download="baixado",
                formato="pdf",
                url_storage=storage.put(f"fnet/{doc_id}.pdf", pdf),
            )
        )
    session.commit()
    return storage


def test_extrair_pdf_direto():
    resultado = extrair_pdf(PDF_COM_CPRF)
    assert resultado.origem == "pdf"
    assert resultado.num_paginas == 1
    assert "CPR-F" in resultado.texto


def test_extrair_pdf_escaneado_vai_para_fila_ocr():
    """PDF sem camada de texto útil não vira busca silenciosamente errada."""
    resultado = extrair_pdf(make_pdf(["capa"]))
    assert resultado.origem == "vazio"


def test_extrair_formato_desconhecido():
    assert extrair(b"...", "zip").origem == "vazio"
    assert extrair(b"<?xml version='1.0'?><a>ola</a>", "xml").origem == "xml"


def test_extrair_textos_pipeline(session, tmp_path):
    storage = _monta_universo(session, tmp_path)
    stats = extrair_textos(session, storage, categorias=["Regulamento"], limite=10)
    session.commit()
    assert stats == {"extraidos": 2, "vazios": 0, "erros": 0}

    texto = session.get(DocumentoTexto, 501)
    assert texto.origem == "pdf"
    assert texto.num_caracteres > 200
    assert len(texto.texto_norm) == len(texto.texto)  # offsets alinhados
    assert "cpr-f" in texto.texto_norm

    # segunda passada não reprocessa o que já tem texto
    stats = extrair_textos(session, storage, limite=10)
    session.commit()
    assert stats["extraidos"] == 0
    assert len(session.execute(select(DocumentoTexto)).scalars().all()) == 2


def test_extrair_textos_erro_de_leitura(session, tmp_path):
    _monta_universo(session, tmp_path)
    doc = session.get(Documento, 501)
    doc.url_storage = str(tmp_path / "nao-existe.pdf")
    session.commit()

    stats = extrair_textos(session, LocalStorage(tmp_path / "docs"), limite=10)
    session.commit()
    assert stats["erros"] == 1
    assert stats["extraidos"] == 1  # o outro documento seguiu normalmente


def test_busca_cprf_encontra_apenas_o_fundo_certo(session, tmp_path):
    """O caso motivador: CPR-F expressamente permitido, com trecho do regulamento."""
    storage = _monta_universo(session, tmp_path)
    extrair_textos(session, storage, limite=10)
    session.commit()

    resultado = buscar(
        session,
        Filtros(termos=["CPR-F", "Cédula do Produto Rural Financeira"]),
    )
    assert [f.cnpj for f in resultado.fundos] == [CNPJ_A]  # BETA (só "CPR") ficou de fora

    alfa = resultado.fundos[0]
    assert alfa.denominacao == "ALFA FIAGRO"
    assert alfa.tipo_veiculo == "FIAGRO"
    assert alfa.pl_medio_12m == Decimal("250000000.00")
    assert alfa.cotistas_atual == 120

    doc = alfa.documentos[0]
    assert doc.id_fnet == 501
    assert doc.categoria == "Regulamento"
    assert doc.url_storage  # link para o PDF na UI
    assert doc.trechos
    # o trecho mostra o contexto ao redor do termo, com o match localizado
    trecho = doc.trechos[0]
    assert trecho.texto[trecho.inicio : trecho.fim].lower().startswith("cpr")
    assert "carteira" in trecho.texto or "Cedula" in trecho.texto


def test_busca_termo_com_quebra_de_linha_no_pdf(session, tmp_path):
    """'Cedula do\\nProduto Rural Financeira' precisa casar apesar da quebra."""
    storage = _monta_universo(session, tmp_path)
    extrair_textos(session, storage, limite=10)
    session.commit()

    resultado = buscar(session, Filtros(termos=["Cedula do Produto Rural Financeira"]))
    assert [f.cnpj for f in resultado.fundos] == [CNPJ_A]


def test_busca_combina_filtros_estruturados(session, tmp_path):
    storage = _monta_universo(session, tmp_path)
    extrair_textos(session, storage, limite=10)
    session.commit()

    # o fundo casa o texto mas é filtrado pelo PL
    assert buscar(session, Filtros(termos=["CPR-F"], pl_min=Decimal("300000000"))).fundos == []
    # e volta quando o filtro permite
    assert len(buscar(session, Filtros(termos=["CPR-F"], pl_min=Decimal("100000000"))).fundos) == 1
    # filtro por tipo de veículo
    assert buscar(session, Filtros(termos=["CPR-F"], tipos_veiculo=["FIDC"])).fundos == []
    assert len(buscar(session, Filtros(termos=["CPR-F"], tipos_veiculo=["FIAGRO"])).fundos) == 1
    # público-alvo casa por substring
    assert len(buscar(session, Filtros(termos=["CPR-F"], publico_alvo=["qualificado"])).fundos) == 1
    assert buscar(session, Filtros(termos=["CPR-F"], publico_alvo=["profissional"])).fundos == []


def test_busca_exigir_todos_os_termos(session, tmp_path):
    storage = _monta_universo(session, tmp_path)
    extrair_textos(session, storage, limite=10)
    session.commit()

    # CRA está no regulamento do ALFA; "debênture" não
    assert len(buscar(session, Filtros(termos=["CPR-F", "CRA"], exigir_todos_termos=True)).fundos) == 1
    assert buscar(
        session, Filtros(termos=["CPR-F", "debênture"], exigir_todos_termos=True)
    ).fundos == []
    # com "qualquer termo", CPR-F sozinho já traz o fundo
    assert len(buscar(session, Filtros(termos=["CPR-F", "debênture"])).fundos) == 1


def test_busca_sem_termos_lista_por_filtros(session, tmp_path):
    _monta_universo(session, tmp_path)
    resultado = buscar(session, Filtros(cotistas_min=100))
    assert [f.cnpj for f in resultado.fundos] == [CNPJ_A]
    assert resultado.total_aproximado == 1

    todos = buscar(session, Filtros())
    assert len(todos.fundos) == 2
    # ordenado por PL médio desc
    assert [f.cnpj for f in todos.fundos] == [CNPJ_A, CNPJ_B]


def test_busca_sinaliza_vedacao(session, tmp_path):
    """Menção não é permissão: regulamento que VEDA CPR-F aparece marcado."""
    storage = _monta_universo(session, tmp_path)
    pdf_veda = make_pdf(
        [
            PREAMBULO[:90],
            PREAMBULO[90:180],
            "Art. 5o - E vedada ao Fundo a aquisicao de CPR-F e de quaisquer",
            "outros ativos financeiros do agronegocio.",
            PREAMBULO[180:270],
        ]
    )
    session.add(
        Fundo(cnpj="33333333000153", denominacao="DELTA FIDC", tipo_veiculo="FIDC",
              situacao="EM FUNCIONAMENTO NORMAL")
    )
    session.add(
        Documento(
            id_fnet=503, cnpj="33333333000153", categoria="Regulamento",
            data_referencia="10/06/2026", data_entrega=dt.datetime(2026, 6, 11, 10, 0),
            status_download="baixado", formato="pdf",
            url_storage=storage.put("fnet/503.pdf", pdf_veda),
        )
    )
    session.commit()
    extrair_textos(session, storage, limite=10)
    session.commit()

    resultado = buscar(session, Filtros(termos=["CPR-F"]))
    por_cnpj = {f.cnpj: f for f in resultado.fundos}
    assert "33333333000153" in por_cnpj  # aparece, pois menciona o termo
    assert por_cnpj["33333333000153"].documentos[0].trechos[0].possivel_vedacao is True
    assert por_cnpj[CNPJ_A].documentos[0].trechos[0].possivel_vedacao is False

    # e some quando o analista pede só permissões
    filtrado = buscar(session, Filtros(termos=["CPR-F"], excluir_vedacoes=True))
    assert "33333333000153" not in {f.cnpj for f in filtrado.fundos}
    assert CNPJ_A in {f.cnpj for f in filtrado.fundos}


def test_busca_flexivel_vs_literal(session, tmp_path):
    """'cédulas de produto rural' no texto casa o termo no singular com 'do'."""
    storage = _monta_universo(session, tmp_path)
    session.add(
        Fundo(cnpj="44444444000134", denominacao="EPSILON FIAGRO", tipo_veiculo="FIAGRO",
              situacao="EM FUNCIONAMENTO NORMAL")
    )
    session.add(
        Documento(
            id_fnet=504, cnpj="44444444000134", categoria="Regulamento",
            data_referencia="10/06/2026", data_entrega=dt.datetime(2026, 6, 11, 10, 0),
            status_download="baixado", formato="pdf",
            url_storage=storage.put(
                "fnet/504.pdf",
                make_pdf([PREAMBULO[:90], PREAMBULO[90:180],
                          "Art. 5o - O Fundo podera adquirir cedulas de produto rural",
                          "financeiras emitidas por produtores.", PREAMBULO[180:270]]),
            ),
        )
    )
    session.commit()
    extrair_textos(session, storage, limite=10)
    session.commit()

    termo = ["Cédula do Produto Rural Financeira"]
    assert "44444444000134" in {f.cnpj for f in buscar(session, Filtros(termos=termo)).fundos}
    # no modo literal, a variação de plural/conector deixa de casar
    literais = buscar(session, Filtros(termos=termo, literal=True))
    assert "44444444000134" not in {f.cnpj for f in literais.fundos}


def test_busca_apenas_vigente(session, tmp_path):
    """Com várias versões do regulamento, só a mais recente é considerada."""
    storage = _monta_universo(session, tmp_path)
    antigo = make_pdf([PREAMBULO[:90], "Art. 5o - vedada a aquisicao de CPR-F.", PREAMBULO[90:180]])
    session.add(
        Documento(
            id_fnet=400,
            cnpj=CNPJ_A,
            categoria="Regulamento",
            data_referencia="10/01/2020",
            data_entrega=dt.datetime(2020, 1, 11, 10, 0),
            status_download="baixado",
            formato="pdf",
            url_storage=storage.put("fnet/400.pdf", antigo),
        )
    )
    session.commit()
    extrair_textos(session, storage, limite=10)
    session.commit()

    vigente = buscar(session, Filtros(termos=["CPR-F"]))
    assert [d.id_fnet for f in vigente.fundos for d in f.documentos] == [501]

    todas = buscar(session, Filtros(termos=["CPR-F"], apenas_vigente=False))
    assert sorted(d.id_fnet for f in todas.fundos for d in f.documentos) == [400, 501]
