import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from conftest import FakeFetch, make_zip
from ingestor import cvm_bulk
from ingestor.metricas import recompute_metricas
from ingestor.models import Fundo, FundoMetricas, InformeMensal, SyncState
from ingestor.parsing import LayoutError

CNPJ_FII = "11111111000191"
CNPJ_FIAGRO = "22222222000172"
CNPJ_FIDC = "33333333000153"

CAD_FI = """TP_FUNDO;CNPJ_FUNDO;DENOM_SOCIAL;DT_REG;SIT;PUBLICO_ALVO;ADMIN;GESTOR;CLASSE
FII;11.111.111/0001-91;ALFA FUNDO DE INVESTIMENTO IMOBILIARIO;2015-03-10;EM FUNCIONAMENTO NORMAL;Investidores em Geral;ADMIN A;GESTOR A;
FII;11.111.111/0001-91;ALFA FUNDO DE INVESTIMENTO IMOBILIARIO;2010-01-01;CANCELADA;Investidores em Geral;ADMIN VELHO;GESTOR VELHO;
FII;22.222.222/0001-72;BETA FIAGRO IMOBILIARIO;2021-08-02;EM FUNCIONAMENTO NORMAL;Investidores Qualificados;ADMIN B;GESTOR B;
FIDC;33.333.333/0001-53;GAMA FIDC AGRO;2019-05-20;EM FUNCIONAMENTO NORMAL;Investidores Profissionais;ADMIN C;GESTOR C;FIDC
""".encode("latin-1")

FII_GERAL = """CNPJ_Fundo;Data_Referencia;Versao;Nome_Fundo;Publico_Alvo;Segmento_Atuacao
11.111.111/0001-91;2026-04;1;ALFA FII;Investidores em Geral;Lajes Corporativas
11.111.111/0001-91;2026-05;2;ALFA FII;Investidores em Geral;Lajes Corporativas
22.222.222/0001-72;2026-05;1;BETA FIAGRO;Investidores Qualificados;Agro
""".encode("latin-1")

FII_COMPLEMENTO = """CNPJ_Fundo;Data_Referencia;Total_Numero_Cotistas;Patrimonio_Liquido;Valor_Patrimonial_Cotas
11.111.111/0001-91;2026-04;1000;100000000.00;100.50
11.111.111/0001-91;2026-05;1100;110000000.00;101.00
22.222.222/0001-72;2026-05;50;20000000.00;95.00
""".encode("latin-1")

FII_ATIVO_PASSIVO = """CNPJ_Fundo;Data_Referencia;Total_Ativo;Patrimonio_Liquido
11.111.111/0001-91;2026-05;120000000.00;999.99
""".encode("latin-1")

FIDC_TAB_IV = """CNPJ_FUNDO;DENOM_SOCIAL;DT_COMPTC;VL_PATRIM_LIQ
33.333.333/0001-53;GAMA FIDC AGRO;2026-05-01;50000000.00
""".encode("latin-1")

FIDC_TAB_X = """CNPJ_FUNDO;DENOM_SOCIAL;DT_COMPTC;CLASSE_SERIE;QT_COTST
33.333.333/0001-53;GAMA FIDC AGRO;2026-05-01;Senior 1;8
33.333.333/0001-53;GAMA FIDC AGRO;2026-05-01;Subordinada;2
""".encode("latin-1")


def fetch_for(**overrides):
    responses = {
        "cad_fi.csv": CAD_FI,
        "inf_mensal_fii_2026.zip": make_zip(
            {
                "inf_mensal_fii_geral_2026.csv": FII_GERAL,
                "inf_mensal_fii_complemento_2026.csv": FII_COMPLEMENTO,
                "inf_mensal_fii_ativo_passivo_2026.csv": FII_ATIVO_PASSIVO,
            }
        ),
        "inf_mensal_fidc_202605.zip": make_zip(
            {
                "inf_mensal_fidc_tab_IV_202605.csv": FIDC_TAB_IV,
                "inf_mensal_fidc_tab_X_202605.csv": FIDC_TAB_X,
            }
        ),
    }
    responses.update(overrides)
    return FakeFetch(responses)


def test_sync_cadastro(session):
    stats = cvm_bulk.sync_cadastro(session, fetch_for())
    session.commit()
    assert stats["fundos"] == 3

    alfa = session.get(Fundo, CNPJ_FII)
    assert alfa.tipo_veiculo == "FII"
    assert alfa.situacao == "EM FUNCIONAMENTO NORMAL"
    assert alfa.administrador == "ADMIN A"  # ganhou o registro ativo, não o cancelado
    assert alfa.dt_registro == dt.date(2015, 3, 10)

    beta = session.get(Fundo, CNPJ_FIAGRO)
    assert beta.tipo_veiculo == "FIAGRO"  # detectado pela denominação
    assert beta.publico_alvo == "Investidores Qualificados"

    gama = session.get(Fundo, CNPJ_FIDC)
    assert gama.tipo_veiculo == "FIDC"


def test_sync_fii_informes(session):
    cvm_bulk.sync_cadastro(session, fetch_for())
    stats = cvm_bulk.sync_fii_informes(session, fetch_for(), anos=[2026])
    session.commit()
    assert stats["informes"] == 3

    informes = session.execute(
        select(InformeMensal).where(InformeMensal.cnpj == CNPJ_FII).order_by(InformeMensal.competencia)
    ).scalars().all()
    assert [i.competencia for i in informes] == [dt.date(2026, 4, 1), dt.date(2026, 5, 1)]
    maio = informes[-1]
    assert maio.pl == Decimal("110000000.00")  # complemento tem precedência sobre ativo_passivo
    assert maio.num_cotistas == 1100
    assert maio.valor_cota == Decimal("101.00")
    assert maio.versao == 2
    assert maio.fonte == "fii_inf_mensal"

    # enriquecimento do cadastro a partir do informe
    alfa = session.get(Fundo, CNPJ_FII)
    assert alfa.segmento == "Lajes Corporativas"

    state = session.get(SyncState, "cvm_fii_inf_mensal")
    assert state.cursor == "2026-05-01"


def test_sync_fii_cria_fundo_desconhecido(session):
    # sem carregar o cadastro antes: informes criam o fundo mínimo
    cvm_bulk.sync_fii_informes(session, fetch_for(), anos=[2026])
    session.commit()
    beta = session.get(Fundo, CNPJ_FIAGRO)
    assert beta is not None
    assert beta.tipo_veiculo == "FIAGRO"
    assert beta.publico_alvo == "Investidores Qualificados"


def test_sync_fii_idempotente(session):
    cvm_bulk.sync_fii_informes(session, fetch_for(), anos=[2026])
    session.commit()
    cvm_bulk.sync_fii_informes(session, fetch_for(), anos=[2026])
    session.commit()
    total = session.execute(select(InformeMensal)).scalars().all()
    assert len(total) == 3


def test_sync_fii_layout_quebrado(session):
    quebrado = make_zip(
        {"inf_mensal_fii_geral_2026.csv": b"COLUNA_NOVA;OUTRA\n1;2\n"}
    )
    with pytest.raises(LayoutError) as err:
        cvm_bulk.sync_fii_informes(
            session, fetch_for(**{"inf_mensal_fii_2026.zip": quebrado}), anos=[2026]
        )
    assert "CNPJ_FUNDO" in str(err.value)


def test_sync_fidc(session):
    stats = cvm_bulk.sync_fidc_informes(session, fetch_for(), competencias=["202605"])
    session.commit()
    assert stats["informes"] == 1

    informe = session.execute(
        select(InformeMensal).where(InformeMensal.cnpj == CNPJ_FIDC)
    ).scalar_one()
    assert informe.pl == Decimal("50000000.00")  # tab_IV, sem soma dupla
    assert informe.num_cotistas == 10  # soma das classes na tab_X
    gama = session.get(Fundo, CNPJ_FIDC)
    assert gama.tipo_veiculo == "FIDC"
    assert gama.denominacao == "GAMA FIDC AGRO"


def test_sync_fidc_zip_sem_colunas_conhecidas(session):
    vazio = make_zip({"inf_mensal_fidc_tab_I_202605.csv": b"CNPJ_FUNDO;DT_COMPTC\n1;2026-05-01\n"})
    with pytest.raises(LayoutError):
        cvm_bulk.sync_fidc_informes(
            session,
            fetch_for(**{"inf_mensal_fidc_202605.zip": vazio}),
            competencias=["202605"],
        )


def test_recompute_metricas(session):
    cvm_bulk.sync_cadastro(session, fetch_for())
    cvm_bulk.sync_fii_informes(session, fetch_for(), anos=[2026])
    cvm_bulk.sync_fidc_informes(session, fetch_for(), competencias=["202605"])
    total = recompute_metricas(session)
    session.commit()
    assert total == 3

    alfa = session.get(FundoMetricas, CNPJ_FII)
    assert alfa.competencia_ultima == dt.date(2026, 5, 1)
    assert alfa.pl_atual == Decimal("110000000.00")
    assert alfa.pl_medio_12m == Decimal("105000000.00")  # média de abr e mai
    assert alfa.cotistas_atual == 1100

    # recálculo é substitutivo, não acumulativo
    total = recompute_metricas(session)
    session.commit()
    assert total == 3


def test_map_tipo_veiculo():
    assert cvm_bulk.map_tipo_veiculo("FII", "ALFA FII") == "FII"
    assert cvm_bulk.map_tipo_veiculo("FII", "BETA FIAGRO IMOBILIARIO") == "FIAGRO"
    assert cvm_bulk.map_tipo_veiculo("FIDC-NP", "GAMA") == "FIDC"
    assert cvm_bulk.map_tipo_veiculo("FIP MULT", "DELTA") == "FIP"
    assert cvm_bulk.map_tipo_veiculo("FI", "EPSILON RF") == "FIF"
    assert cvm_bulk.map_tipo_veiculo(None, None) is None
