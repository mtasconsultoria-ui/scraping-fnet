import datetime as dt
import json

import pytest
from sqlalchemy import select

from ingestor.models import Documento, DocumentoTexto, Fundo, InformeMensal, SyncRun
from ingestor.monitoramento import AVISO, ERRO, OK, diagnosticar, registrar_execucao

AGORA = dt.datetime(2026, 7, 29, 12, 0)


def _fundo_com_informe(session, competencia=dt.date(2026, 6, 1)):
    session.add(Fundo(cnpj="11111111000191", denominacao="ALFA"))
    session.add(
        InformeMensal(cnpj="11111111000191", competencia=competencia, fonte="fii_inf_mensal")
    )
    session.commit()


def test_registrar_execucao_sucesso(session):
    with registrar_execucao(session, "cvm") as stats:
        stats["fundos"] = 1234

    run = session.execute(select(SyncRun)).scalar_one()
    assert run.fonte == "cvm"
    assert run.status == OK
    assert run.terminado_em is not None
    assert json.loads(run.detalhe) == {"fundos": 1234}
    assert run.erro is None


def test_registrar_execucao_grava_erro_e_propaga(session):
    with pytest.raises(RuntimeError, match="layout mudou"):
        with registrar_execucao(session, "cvm"):
            raise RuntimeError("layout mudou")

    run = session.execute(select(SyncRun)).scalar_one()
    assert run.status == ERRO
    assert "RuntimeError: layout mudou" in run.erro
    assert run.terminado_em is not None


def test_registrar_execucao_sobrevive_a_erro_de_banco(session):
    """Erro que suja a transação não pode impedir o registro da falha."""
    with pytest.raises(Exception):
        with registrar_execucao(session, "fnet_documentos"):
            # FK inexistente: quebra no commit e deixa a sessão suja
            session.add(Documento(id_fnet=1, cnpj="00000000000000", status_download="pendente"))
            session.commit()

    run = session.execute(select(SyncRun)).scalar_one()
    assert run.status == ERRO
    assert run.erro


def test_diagnostico_sem_execucoes(session):
    diag = diagnosticar(session, agora=AGORA)
    assert diag.status == ERRO
    assert not diag.saudavel
    nomes = {v.nome for v in diag.verificacoes}
    assert "execucoes" in nomes
    assert "informes" in nomes  # sem informes carregados


def test_diagnostico_saudavel(session):
    _fundo_com_informe(session)
    with registrar_execucao(session, "cvm") as stats:
        stats["fundos"] = 1
    diag = diagnosticar(session, agora=AGORA)

    assert diag.status == OK
    assert diag.saudavel
    assert diag.contagens["fundos"] == 1
    assert diag.contagens["informes"] == 1
    assert diag.ultimas_execucoes[0]["fonte"] == "cvm"
    assert diag.ultimas_execucoes[0]["detalhe"] == {"fundos": 1}


def test_diagnostico_detecta_execucao_com_erro(session):
    _fundo_com_informe(session)
    with pytest.raises(RuntimeError):
        with registrar_execucao(session, "cvm"):
            raise RuntimeError("falhou")

    diag = diagnosticar(session, agora=AGORA)
    assert diag.status == ERRO
    assert any(v.nome == "execucao:cvm" and v.status == ERRO for v in diag.verificacoes)


def test_diagnostico_detecta_sync_antigo(session):
    """Execução antiga demais: o agendamento parou de rodar."""
    _fundo_com_informe(session)
    session.add(
        SyncRun(
            fonte="cvm",
            iniciado_em=AGORA - dt.timedelta(days=30),
            terminado_em=AGORA - dt.timedelta(days=30),
            status=OK,
        )
    )
    session.commit()

    diag = diagnosticar(session, agora=AGORA)
    assert diag.status == ERRO
    frescor = next(v for v in diag.verificacoes if v.nome == "frescor:cvm")
    assert frescor.status == ERRO
    assert "30 dias" in frescor.mensagem


def test_diagnostico_detecta_dados_parados(session):
    """O caso traiçoeiro: sync 'passa' sem erro, mas os dados não avançam."""
    _fundo_com_informe(session, competencia=dt.date(2025, 1, 1))
    with registrar_execucao(session, "cvm"):
        pass

    diag = diagnosticar(session, agora=AGORA)
    assert diag.status == ERRO
    informes = next(v for v in diag.verificacoes if v.nome == "informes")
    assert informes.status == ERRO
    assert "2025-01" in informes.mensagem


def test_diagnostico_execucao_travada(session):
    _fundo_com_informe(session)
    session.add(
        SyncRun(fonte="fnet_documentos", iniciado_em=AGORA - dt.timedelta(hours=10),
                status="executando")
    )
    session.commit()

    diag = diagnosticar(session, agora=AGORA)
    assert any(
        v.nome == "execucao:fnet_documentos" and v.status == ERRO for v in diag.verificacoes
    )


def test_diagnostico_avisa_fila_de_ocr(session):
    _fundo_com_informe(session)
    with registrar_execucao(session, "cvm"):
        pass
    session.add(
        Documento(id_fnet=1, cnpj="11111111000191", categoria="Regulamento",
                  status_download="baixado")
    )
    session.commit()
    session.add(
        DocumentoTexto(id_fnet=1, texto="", texto_norm="", num_caracteres=0, origem="vazio")
    )
    session.commit()

    diag = diagnosticar(session, agora=AGORA)
    ocr = next(v for v in diag.verificacoes if v.nome == "ocr")
    assert ocr.status == AVISO
    assert diag.saudavel  # aviso não derruba a saúde geral
    assert diag.status == AVISO


def test_diagnostico_avisa_documentos_defasados(session):
    """Sync 'ok' com acervo recente ausente (backfill no passado) gera aviso."""
    _fundo_com_informe(session)
    with registrar_execucao(session, "fnet_documentos"):
        pass  # a execução em si foi bem-sucedida...
    session.add(
        Documento(id_fnet=9, cnpj="11111111000191", categoria="Regulamento",
                  status_download="pendente",
                  data_entrega=AGORA - dt.timedelta(days=400))
    )
    session.commit()

    diag = diagnosticar(session, agora=AGORA)
    docs = next(v for v in diag.verificacoes if v.nome == "documentos")
    assert docs.status == AVISO
    assert "400 dias" in docs.mensagem
    assert diag.status == AVISO  # visível, sem derrubar o agendamento


def test_diagnostico_documentos_recentes_ok(session):
    _fundo_com_informe(session)
    with registrar_execucao(session, "fnet_documentos"):
        pass
    session.add(
        Documento(id_fnet=9, cnpj="11111111000191", categoria="Regulamento",
                  status_download="pendente",
                  data_entrega=AGORA - dt.timedelta(days=1))
    )
    session.commit()

    diag = diagnosticar(session, agora=AGORA)
    docs = next(v for v in diag.verificacoes if v.nome == "documentos")
    assert docs.status == OK


def test_diagnostico_serializa_para_json(session):
    _fundo_com_informe(session)
    with registrar_execucao(session, "cvm") as stats:
        stats["fundos"] = 1
    payload = diagnosticar(session, agora=AGORA).to_dict()
    assert json.loads(json.dumps(payload, default=str))["status"] == OK
    assert payload["contagens"]["fundos"] == 1


def test_status_por_fonte_usa_execucao_mais_recente(session):
    _fundo_com_informe(session)
    with pytest.raises(RuntimeError):
        with registrar_execucao(session, "cvm"):
            raise RuntimeError("primeira falhou")
    with registrar_execucao(session, "cvm"):
        pass  # a segunda deu certo

    diag = diagnosticar(session, agora=AGORA)
    assert diag.status == OK
    assert len(diag.ultimas_execucoes) == 1
    assert diag.ultimas_execucoes[0]["status"] == OK
