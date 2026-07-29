"""Registro de execuções e diagnóstico de saúde da ingestão.

O objetivo é que uma falha silenciosa não passe despercebida: além de erros
explícitos, o diagnóstico detecta o caso mais traiçoeiro — o pipeline "passa"
sem erro, mas os dados param de ser atualizados (portal mudou de layout, cursor
travado, credencial expirada). Por isso as verificações olham para o *frescor*
dos dados, não só para o resultado da última execução.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Documento, DocumentoTexto, Fundo, InformeMensal, SyncRun

log = logging.getLogger(__name__)

OK = "ok"
AVISO = "aviso"
ERRO = "erro"

# Limiares (sobrescrevíveis por env). A ingestão da CVM é semanal e a do FNET
# diária; os informes da CVM têm defasagem natural de um a dois meses.
LIMITES_DIAS_SEM_SYNC = {"cvm": 9, "fnet": 3}
MAX_MESES_ATRASO_INFORME = 4
MAX_ERROS_DOWNLOAD = 50


def _agora() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _limite_dias(grupo: str) -> int:
    env = os.environ.get(f"MAX_DIAS_SEM_SYNC_{grupo.upper()}")
    if env and env.isdigit():
        return int(env)
    return LIMITES_DIAS_SEM_SYNC[grupo]


@contextmanager
def registrar_execucao(session: Session, fonte: str):
    """Grava início/fim/erro de uma execução; o bloco preenche as estatísticas.

    O registro é commitado por conta própria, inclusive quando o corpo levanta
    exceção — é justamente aí que ele precisa sobreviver.
    """
    run = SyncRun(fonte=fonte, iniciado_em=_agora(), status="executando")
    session.add(run)
    session.commit()
    run_id = run.id
    stats: dict = {}
    try:
        yield stats
    except Exception as exc:
        session.rollback()  # a exceção pode ter sujado a transação
        falho = session.get(SyncRun, run_id)
        if falho is not None:
            falho.status = ERRO
            falho.erro = f"{type(exc).__name__}: {exc}"[:4000]
            falho.terminado_em = _agora()
            session.commit()
        raise
    else:
        concluido = session.get(SyncRun, run_id)
        if concluido is not None:
            concluido.status = OK
            concluido.detalhe = json.dumps(stats, ensure_ascii=False, default=str)
            concluido.terminado_em = _agora()
            session.commit()


@dataclass
class Verificacao:
    nome: str
    status: str
    mensagem: str

    @property
    def ok(self) -> bool:
        return self.status == OK


@dataclass
class Diagnostico:
    verificacoes: list[Verificacao] = field(default_factory=list)
    contagens: dict = field(default_factory=dict)
    ultimas_execucoes: list[dict] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(v.status == ERRO for v in self.verificacoes):
            return ERRO
        if any(v.status == AVISO for v in self.verificacoes):
            return AVISO
        return OK

    @property
    def saudavel(self) -> bool:
        return self.status != ERRO

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "verificacoes": [
                {"nome": v.nome, "status": v.status, "mensagem": v.mensagem}
                for v in self.verificacoes
            ],
            "contagens": self.contagens,
            "ultimasExecucoes": self.ultimas_execucoes,
        }


def _grupo_da_fonte(fonte: str) -> str:
    return "fnet" if fonte.startswith("fnet") else "cvm"


def diagnosticar(session: Session, agora: dt.datetime | None = None) -> Diagnostico:
    agora = agora or _agora()
    diag = Diagnostico()

    diag.contagens = {
        "fundos": session.scalar(select(func.count()).select_from(Fundo)) or 0,
        "informes": session.scalar(select(func.count()).select_from(InformeMensal)) or 0,
        "documentos": session.scalar(select(func.count()).select_from(Documento)) or 0,
        "textos": session.scalar(select(func.count()).select_from(DocumentoTexto)) or 0,
        "downloads_pendentes": session.scalar(
            select(func.count()).select_from(Documento).where(Documento.status_download == "pendente")
        ) or 0,
        "downloads_com_erro": session.scalar(
            select(func.count()).select_from(Documento).where(Documento.status_download == "erro")
        ) or 0,
        "fila_ocr": session.scalar(
            select(func.count()).select_from(DocumentoTexto).where(DocumentoTexto.origem == "vazio")
        ) or 0,
    }

    # Última execução de cada fonte
    ultimo_id_por_fonte = select(func.max(SyncRun.id)).group_by(SyncRun.fonte)
    ultimas = (
        session.execute(
            select(SyncRun).where(SyncRun.id.in_(ultimo_id_por_fonte)).order_by(SyncRun.fonte)
        )
        .scalars()
        .all()
    )

    diag.ultimas_execucoes = [
        {
            "fonte": run.fonte,
            "status": run.status,
            "iniciadoEm": run.iniciado_em.isoformat() if run.iniciado_em else None,
            "terminadoEm": run.terminado_em.isoformat() if run.terminado_em else None,
            "detalhe": json.loads(run.detalhe) if run.detalhe else None,
            "erro": run.erro,
        }
        for run in ultimas
    ]

    if not ultimas:
        diag.verificacoes.append(
            Verificacao("execucoes", ERRO, "nenhuma execução de ingestão registrada")
        )
    for run in ultimas:
        if run.status == ERRO:
            diag.verificacoes.append(
                Verificacao(f"execucao:{run.fonte}", ERRO, f"última execução falhou: {run.erro}")
            )
            continue
        if run.status == "executando":
            # execução que nunca terminou indica processo morto no meio
            idade = (agora - run.iniciado_em).total_seconds() / 3600
            if idade > 6:
                diag.verificacoes.append(
                    Verificacao(
                        f"execucao:{run.fonte}", ERRO,
                        f"execução iniciada há {idade:.0f}h sem conclusão",
                    )
                )
            continue
        dias = (agora - (run.terminado_em or run.iniciado_em)).days
        limite = _limite_dias(_grupo_da_fonte(run.fonte))
        if dias > limite:
            diag.verificacoes.append(
                Verificacao(
                    f"frescor:{run.fonte}", ERRO,
                    f"sem execução bem-sucedida há {dias} dias (limite {limite})",
                )
            )
        else:
            diag.verificacoes.append(
                Verificacao(f"frescor:{run.fonte}", OK, f"última execução há {dias} dia(s)")
            )

    # Frescor dos dados em si: o pipeline pode "passar" sem trazer nada novo
    competencia = session.scalar(select(func.max(InformeMensal.competencia)))
    if competencia is None:
        diag.verificacoes.append(
            Verificacao("informes", ERRO, "nenhum informe mensal carregado")
        )
    else:
        meses = (agora.year - competencia.year) * 12 + (agora.month - competencia.month)
        if meses > MAX_MESES_ATRASO_INFORME:
            diag.verificacoes.append(
                Verificacao(
                    "informes", ERRO,
                    f"informe mais recente é de {competencia:%Y-%m} ({meses} meses atrás)",
                )
            )
        else:
            diag.verificacoes.append(
                Verificacao("informes", OK, f"competência mais recente: {competencia:%Y-%m}")
            )

    if diag.contagens["downloads_com_erro"] > MAX_ERROS_DOWNLOAD:
        diag.verificacoes.append(
            Verificacao(
                "downloads", AVISO,
                f"{diag.contagens['downloads_com_erro']} documentos com erro de download",
            )
        )

    if diag.contagens["fila_ocr"] > 0:
        diag.verificacoes.append(
            Verificacao(
                "ocr", AVISO,
                f"{diag.contagens['fila_ocr']} documentos sem camada de texto (fila de OCR)",
            )
        )

    return diag
