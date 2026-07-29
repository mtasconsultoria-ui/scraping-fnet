from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

import json
from decimal import Decimal

from sqlalchemy.orm import Session

from . import busca as busca_mod
from . import cvm_bulk, fnet_sync, prospeccao, smoke, text_extract
from .db import get_engine, init_db
from .fnet_client import FnetClient
from .http_client import fetch_bytes
from .metricas import recompute_metricas
from .monitoramento import AVISO, ERRO, diagnosticar, registrar_execucao
from .storage import storage_from_env

log = logging.getLogger("ingestor")


def _competencias_fidc(anos: list[int]) -> list[str]:
    """AAAAMM de cada mês já encerrado dos anos pedidos."""
    hoje = dt.date.today()
    competencias = []
    for ano in anos:
        for mes in range(1, 13):
            if dt.date(ano, mes, 1) < hoje.replace(day=1):
                competencias.append(f"{ano}{mes:02d}")
    return competencias


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingestor", description="Ingestão CVM/FNET")
    parser.add_argument("--database-url", help="sobrepõe DATABASE_URL")
    sub = parser.add_subparsers(dest="comando", required=True)

    sub.add_parser("init-db", help="cria as tabelas")

    p_sync = sub.add_parser("sync-cvm", help="carrega dados abertos da CVM")
    p_sync.add_argument(
        "--datasets",
        nargs="+",
        choices=["cadastro", "fii", "fidc"],
        default=["cadastro", "fii"],
    )
    p_sync.add_argument(
        "--anos",
        nargs="+",
        type=int,
        default=[dt.date.today().year - 1, dt.date.today().year],
    )
    p_sync.add_argument(
        "--competencias-fidc",
        nargs="+",
        help="meses AAAAMM para o dataset fidc (default: meses encerrados de --anos)",
    )

    sub.add_parser("metricas", help="recalcula fundos_metricas")

    p_fnet = sub.add_parser("sync-fnet", help="sincroniza metadados de documentos do FNET")
    p_fnet.add_argument("--tipo-fundo", type=int, help="id do tipo de fundo no FNET (ver dominios-fnet)")
    p_fnet.add_argument("--categoria", type=int, help="idCategoriaDocumento no FNET")
    p_fnet.add_argument("--desde", help="data inicial AAAA-MM-DD (default: cursor do último sync)")
    p_fnet.add_argument("--max-paginas", type=int, default=2000)

    p_down = sub.add_parser("download-docs", help="baixa documentos pendentes para o storage")
    p_down.add_argument("--categorias", nargs="+", help='ex.: --categorias Regulamento "Fato Relevante"')
    p_down.add_argument("--limite", type=int, default=100)

    sub.add_parser("dominios-fnet", help="raspa as tabelas de domínio dos filtros do FNET")

    p_txt = sub.add_parser("extrair-textos", help="extrai texto dos documentos baixados")
    p_txt.add_argument("--categorias", nargs="+", help='ex.: --categorias Regulamento')
    p_txt.add_argument("--limite", type=int, default=100)
    p_txt.add_argument("--reprocessar", action="store_true", help="refaz os já extraídos")
    p_txt.add_argument("--ocr", action="store_true", help="processa a fila de escaneados")

    p_busca = sub.add_parser("buscar", help="consulta fundos por filtros e conteúdo")
    p_busca.add_argument("--termos", nargs="+", default=[], help='ex.: --termos "CPR-F" "Cédula do Produto Rural"')
    p_busca.add_argument("--todos-termos", action="store_true", help="exige todos os termos")
    p_busca.add_argument("--tipo", nargs="+", default=[], help="FII FIDC FIAGRO FIP FIF")
    p_busca.add_argument("--publico-alvo", nargs="+", default=[], help="ex.: qualificado profissional")
    p_busca.add_argument("--situacao", help='ex.: "EM FUNCIONAMENTO NORMAL"')
    p_busca.add_argument("--pl-min", type=Decimal)
    p_busca.add_argument("--pl-max", type=Decimal)
    p_busca.add_argument("--cotistas-min", type=int)
    p_busca.add_argument("--cotistas-max", type=int)
    p_busca.add_argument("--categorias", nargs="+", default=["Regulamento"])
    p_busca.add_argument("--todas-versoes", action="store_true", help="não limita ao vigente")
    p_busca.add_argument(
        "--excluir-vedacoes",
        action="store_true",
        help="descarta documentos em que o termo só aparece como vedação",
    )
    p_busca.add_argument("--literal", action="store_true", help="não tolera plural/conectores")
    p_busca.add_argument("--limite", type=int, default=20)

    p_smoke = sub.add_parser(
        "smoke", help="teste de fumaça: uma requisição a cada portal (sem banco)"
    )
    p_smoke.add_argument("--ano-fii", type=int, default=dt.date.today().year)

    p_prosp = sub.add_parser(
        "prospectar", help="acha fundos cujo regulamento cita um termo (sem banco)"
    )
    p_prosp.add_argument("--termos", nargs="+", required=True)
    p_prosp.add_argument("--alvo", type=int, default=5, help="quantos fundos trazer")
    p_prosp.add_argument("--categoria", default="Regulamento")
    p_prosp.add_argument("--tipo-fundo", help="rótulo do filtro no FNET (ex.: FIDC)")
    p_prosp.add_argument("--max-documentos", type=int, default=120)
    p_prosp.add_argument(
        "--incluir-inativos", action="store_true", help="não exige situação ativa no cad_fi"
    )

    p_status = sub.add_parser("status", help="diagnóstico de saúde da ingestão")
    p_status.add_argument("--json", action="store_true", help="saída em JSON")
    p_status.add_argument(
        "--check",
        action="store_true",
        help="encerra com código 1 se houver problema (usar no agendamento)",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # Estes dois não tocam o banco: servem para validar os portais sem infra
    if args.comando == "smoke":
        return smoke.executar(ano_fii=args.ano_fii)
    if args.comando == "prospectar":
        return prospeccao.prospectar(
            args.termos,
            alvo=args.alvo,
            categoria=args.categoria,
            tipo_fundo=args.tipo_fundo,
            max_documentos=args.max_documentos,
            apenas_ativos=not args.incluir_inativos,
        )

    engine = get_engine(args.database_url)
    if args.comando == "init-db":
        init_db(engine)
        log.info("tabelas criadas")
        return 0

    with Session(engine) as session:
        if args.comando == "sync-cvm":
            init_db(engine)  # idempotente; garante schema em bancos novos
            with registrar_execucao(session, "cvm") as stats:
                if "cadastro" in args.datasets:
                    stats["cadastro"] = cvm_bulk.sync_cadastro(session, fetch_bytes)
                    session.commit()
                if "fii" in args.datasets:
                    stats["fii"] = cvm_bulk.sync_fii_informes(session, fetch_bytes, args.anos)
                    session.commit()
                if "fidc" in args.datasets:
                    competencias = args.competencias_fidc or _competencias_fidc(args.anos)
                    stats["fidc"] = cvm_bulk.sync_fidc_informes(session, fetch_bytes, competencias)
                    session.commit()
                stats["metricas"] = recompute_metricas(session)
                session.commit()
                log.info("métricas recalculadas para %s fundos", stats["metricas"])
        elif args.comando == "metricas":
            total = recompute_metricas(session)
            session.commit()
            log.info("métricas recalculadas para %s fundos", total)
        elif args.comando == "sync-fnet":
            init_db(engine)
            client = FnetClient()
            try:
                with registrar_execucao(session, "fnet_documentos") as stats:
                    stats.update(
                        fnet_sync.sync_documentos(
                            session,
                            client,
                            tipo_fundo=args.tipo_fundo,
                            id_categoria=args.categoria,
                            desde=dt.date.fromisoformat(args.desde) if args.desde else None,
                            max_pages=args.max_paginas,
                        )
                    )
                    session.commit()
            finally:
                client.close()
        elif args.comando == "download-docs":
            client = FnetClient()
            try:
                with registrar_execucao(session, "fnet_download") as stats:
                    stats.update(
                        fnet_sync.download_documentos(
                            session,
                            client,
                            storage_from_env(),
                            categorias=args.categorias,
                            limite=args.limite,
                        )
                    )
                    session.commit()
            finally:
                client.close()
        elif args.comando == "dominios-fnet":
            init_db(engine)
            client = FnetClient()
            try:
                fnet_sync.sync_dominios(session, client)
                session.commit()
            finally:
                client.close()
        elif args.comando == "extrair-textos":
            init_db(engine)
            with registrar_execucao(session, "fnet_textos") as stats:
                stats.update(
                    text_extract.extrair_textos(
                        session,
                        storage_from_env(),
                        categorias=args.categorias,
                        limite=args.limite,
                        reprocessar=args.reprocessar,
                        ocr=args.ocr,
                    )
                )
                session.commit()
        elif args.comando == "status":
            # bancos criados por versões anteriores podem não ter todas as
            # tabelas; create_all é idempotente e adiciona as que faltarem
            init_db(engine)
            diag = diagnosticar(session)
            if args.json:
                print(json.dumps(diag.to_dict(), ensure_ascii=False, indent=2, default=str))
            else:
                _imprime_diagnostico(diag)
            if args.check and not diag.saudavel:
                return 1
        elif args.comando == "buscar":
            resultado = busca_mod.buscar(
                session,
                busca_mod.Filtros(
                    tipos_veiculo=args.tipo,
                    publico_alvo=args.publico_alvo,
                    situacao=args.situacao,
                    pl_min=args.pl_min,
                    pl_max=args.pl_max,
                    cotistas_min=args.cotistas_min,
                    cotistas_max=args.cotistas_max,
                    termos=args.termos,
                    exigir_todos_termos=args.todos_termos,
                    categorias_documento=args.categorias,
                    apenas_vigente=not args.todas_versoes,
                    excluir_vedacoes=args.excluir_vedacoes,
                    literal=args.literal,
                    limite=args.limite,
                ),
            )
            _imprime_resultado(resultado)
    return 0


_SIMBOLO = {"ok": "OK  ", AVISO: "AVISO", ERRO: "ERRO"}


def _imprime_diagnostico(diag) -> None:
    print(f"\nstatus geral: {diag.status.upper()}\n")
    print("verificações:")
    for verificacao in diag.verificacoes:
        print(f"  [{_SIMBOLO[verificacao.status]}] {verificacao.nome}: {verificacao.mensagem}")
    print("\ncontagens:")
    for chave, valor in diag.contagens.items():
        print(f"  {chave:22} {valor:>10,}".replace(",", "."))
    if diag.ultimas_execucoes:
        print("\núltimas execuções:")
        for run in diag.ultimas_execucoes:
            quando = (run["terminadoEm"] or run["iniciadoEm"] or "")[:19].replace("T", " ")
            print(f"  {run['fonte']:20} {run['status']:10} {quando}")
            if run["detalhe"]:
                print(f"    {json.dumps(run['detalhe'], ensure_ascii=False)}")
            if run["erro"]:
                print(f"    erro: {run['erro'][:200]}")
    print()


def _fmt_milhoes(valor) -> str:
    return f"R$ {valor / 1_000_000:,.1f}mi".replace(",", "_").replace(".", ",").replace("_", ".")


def _imprime_resultado(resultado) -> None:
    print(f"\n{len(resultado.fundos)} fundo(s) de {resultado.total_aproximado} encontrado(s)")
    if resultado.truncado:
        print("AVISO: universo de candidatos truncado; refine os filtros")
    for fundo in resultado.fundos:
        pl = _fmt_milhoes(fundo.pl_medio_12m) if fundo.pl_medio_12m else "PL n/d"
        cotistas = f"{fundo.cotistas_atual} cotistas" if fundo.cotistas_atual else "cotistas n/d"
        print(f"\n{'=' * 78}")
        print(f"{fundo.denominacao or '(sem denominação)'}  [{fundo.cnpj}]")
        print(f"  {fundo.tipo_veiculo or '?'} | {fundo.publico_alvo or 'público-alvo n/d'}")
        print(f"  PL médio 12m: {pl} | {cotistas} | admin: {fundo.administrador or 'n/d'}")
        for doc in fundo.documentos:
            print(f"  -- {doc.categoria} ({doc.data_referencia or 's/ data'}) doc {doc.id_fnet}")
            for trecho in doc.trechos:
                marca = " [POSSÍVEL VEDAÇÃO]" if trecho.possivel_vedacao else ""
                print(f"     ...{trecho.destacado()}...{marca}")
    print()


if __name__ == "__main__":
    sys.exit(main())
