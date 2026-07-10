"""Recálculo dos derivados usados nos filtros (fundos_metricas)."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .db import bulk_upsert
from .models import FundoMetricas, InformeMensal

JANELA_PL_MEDIO = 12  # meses


def recompute_metricas(session: Session) -> int:
    series: dict[str, list] = defaultdict(list)
    stmt = select(
        InformeMensal.cnpj,
        InformeMensal.competencia,
        InformeMensal.pl,
        InformeMensal.num_cotistas,
    ).order_by(InformeMensal.cnpj, InformeMensal.competencia)
    for cnpj, competencia, pl, num_cotistas in session.execute(stmt):
        series[cnpj].append((competencia, pl, num_cotistas))

    rows = []
    for cnpj, informes in series.items():
        pls_janela = [pl for _, pl, _ in informes[-JANELA_PL_MEDIO:] if pl is not None]
        pl_medio = (
            (sum(pls_janela) / Decimal(len(pls_janela))).quantize(Decimal("0.01"))
            if pls_janela
            else None
        )
        pl_atual = next((pl for _, pl, _ in reversed(informes) if pl is not None), None)
        cotistas_atual = next(
            (cot for _, _, cot in reversed(informes) if cot is not None), None
        )
        rows.append(
            {
                "cnpj": cnpj,
                "competencia_ultima": informes[-1][0],
                "pl_atual": pl_atual,
                "pl_medio_12m": pl_medio,
                "cotistas_atual": cotistas_atual,
            }
        )

    session.execute(delete(FundoMetricas))
    bulk_upsert(
        session,
        FundoMetricas,
        rows,
        key_cols=["cnpj"],
        update_cols=["competencia_ultima", "pl_atual", "pl_medio_12m", "cotistas_atual"],
    )
    return len(rows)
