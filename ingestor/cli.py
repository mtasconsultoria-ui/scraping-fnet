from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from sqlalchemy.orm import Session

from . import cvm_bulk, fnet_sync
from .db import get_engine, init_db
from .fnet_client import FnetClient
from .http_client import fetch_bytes
from .metricas import recompute_metricas
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
    p_fnet.add_argument("--max-paginas", type=int, default=200)

    p_down = sub.add_parser("download-docs", help="baixa documentos pendentes para o storage")
    p_down.add_argument("--categorias", nargs="+", help='ex.: --categorias Regulamento "Fato Relevante"')
    p_down.add_argument("--limite", type=int, default=100)

    sub.add_parser("dominios-fnet", help="raspa as tabelas de domínio dos filtros do FNET")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    engine = get_engine(args.database_url)
    if args.comando == "init-db":
        init_db(engine)
        log.info("tabelas criadas")
        return 0

    with Session(engine) as session:
        if args.comando == "sync-cvm":
            init_db(engine)  # idempotente; garante schema em bancos novos
            if "cadastro" in args.datasets:
                cvm_bulk.sync_cadastro(session, fetch_bytes)
                session.commit()
            if "fii" in args.datasets:
                cvm_bulk.sync_fii_informes(session, fetch_bytes, args.anos)
                session.commit()
            if "fidc" in args.datasets:
                competencias = args.competencias_fidc or _competencias_fidc(args.anos)
                cvm_bulk.sync_fidc_informes(session, fetch_bytes, competencias)
                session.commit()
            total = recompute_metricas(session)
            session.commit()
            log.info("métricas recalculadas para %s fundos", total)
        elif args.comando == "metricas":
            total = recompute_metricas(session)
            session.commit()
            log.info("métricas recalculadas para %s fundos", total)
        elif args.comando == "sync-fnet":
            init_db(engine)
            client = FnetClient()
            try:
                fnet_sync.sync_documentos(
                    session,
                    client,
                    tipo_fundo=args.tipo_fundo,
                    id_categoria=args.categoria,
                    desde=dt.date.fromisoformat(args.desde) if args.desde else None,
                    max_pages=args.max_paginas,
                )
                session.commit()
            finally:
                client.close()
        elif args.comando == "download-docs":
            client = FnetClient()
            try:
                fnet_sync.download_documentos(
                    session,
                    client,
                    storage_from_env(),
                    categorias=args.categorias,
                    limite=args.limite,
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
