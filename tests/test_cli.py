"""Testes do CLI, com foco no que o agendamento depende: o código de saída."""
import datetime as dt
import json

import pytest
from sqlalchemy import select

from ingestor.cli import main
from ingestor.models import Fundo, InformeMensal, SyncRun


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite:///{tmp_path}/cli.db"


def _popula_saudavel(url):
    from sqlalchemy.orm import Session

    from ingestor.db import get_engine, init_db
    from ingestor.monitoramento import registrar_execucao

    engine = get_engine(url)
    init_db(engine)
    with Session(engine) as session:
        session.add(Fundo(cnpj="11111111000191", denominacao="ALFA"))
        session.add(
            InformeMensal(
                cnpj="11111111000191",
                competencia=dt.date.today().replace(day=1),
                fonte="fii_inf_mensal",
            )
        )
        session.commit()
        with registrar_execucao(session, "cvm") as stats:
            stats["fundos"] = 1


def test_init_db_cria_tabelas(db_url):
    assert main(["--database-url", db_url, "init-db"]) == 0


def test_status_check_falha_em_banco_vazio(db_url, capsys):
    """Sem ingestão registrada o agendamento precisa falhar, não passar calado."""
    assert main(["--database-url", db_url, "status", "--check"]) == 1
    assert "ERRO" in capsys.readouterr().out


def test_status_check_passa_quando_saudavel(db_url):
    _popula_saudavel(db_url)
    assert main(["--database-url", db_url, "status", "--check"]) == 0


def test_status_sem_check_nunca_falha(db_url):
    """Sem --check o comando é só diagnóstico; não deve quebrar um pipeline."""
    assert main(["--database-url", db_url, "status"]) == 0


def test_status_json(db_url, capsys):
    _popula_saudavel(db_url)
    assert main(["--database-url", db_url, "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["contagens"]["fundos"] == 1
    assert payload["ultimasExecucoes"][0]["fonte"] == "cvm"


def test_status_cria_tabelas_faltantes(db_url):
    """Banco de uma versão anterior, sem sync_runs, não pode quebrar o status."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from ingestor.db import get_engine, init_db

    engine = get_engine(db_url)
    init_db(engine)
    with Session(engine) as session:
        session.execute(text("DROP TABLE sync_runs"))
        session.commit()

    assert main(["--database-url", db_url, "status"]) == 0


def test_metricas_roda_sem_dados(db_url):
    main(["--database-url", db_url, "init-db"])
    assert main(["--database-url", db_url, "metricas"]) == 0


def test_busca_sem_resultados(db_url, capsys):
    main(["--database-url", db_url, "init-db"])
    assert main(["--database-url", db_url, "buscar", "--termos", "CPR-F"]) == 0
    assert "0 fundo(s)" in capsys.readouterr().out


def test_extrair_textos_registra_execucao(db_url, tmp_path):
    from sqlalchemy.orm import Session

    from ingestor.db import get_engine

    main(["--database-url", db_url, "init-db"])
    # sem documentos baixados, a execução é vazia mas precisa ficar registrada
    assert main(["--database-url", db_url, "extrair-textos"]) == 0

    with Session(get_engine(db_url)) as session:
        run = session.execute(select(SyncRun)).scalar_one()
        assert run.fonte == "fnet_textos"
        assert run.status == "ok"
        assert json.loads(run.detalhe) == {"extraidos": 0, "vazios": 0, "erros": 0}
