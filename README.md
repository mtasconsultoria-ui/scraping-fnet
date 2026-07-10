# scraping-fnet

Coleta e estruturação de dados de fundos (FII, FIDC, FIAGRO, FIP, FIF...) a partir do
Portal de Dados Abertos da CVM e do FundosNET, para filtros por características do
fundo e por conteúdo de documentos. Arquitetura completa em [`ARQUITETURA.md`](ARQUITETURA.md).

**Status: Fase 1** — ingestão dos Dados Abertos da CVM (cadastro + informes mensais)
com schema de banco e métricas derivadas. Fases seguintes: documentos do FNET,
full-text search de regulamentos, API e UI no Vercel.

## Ingestor (Python 3.11+)

```bash
pip install -e .[dev]
pytest                      # testes com fixtures dos layouts da CVM

# carga local (SQLite ./fnet.db por padrão)
python -m ingestor sync-cvm --datasets cadastro fii --anos 2025 2026

# Postgres (produção)
export DATABASE_URL="postgresql+psycopg://user:senha@host/db?sslmode=require"
pip install -e .[postgres]
python -m ingestor sync-cvm --datasets cadastro fii fidc --anos 2024 2025 2026
```

Comandos: `init-db` (cria tabelas), `sync-cvm` (carrega e recalcula métricas),
`metricas` (só recalcula derivados).

### Datasets

| Dataset | Fonte | Alimenta |
|---|---|---|
| `cadastro` | `FI/CAD/DADOS/cad_fi.csv` | `fundos` (todos os veículos: tipo, público-alvo, situação, admin, gestor) |
| `fii` | `FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_{ano}.zip` | `informes_mensais` (PL, cotistas, valor de cota) + enriquece `fundos`; inclui FIAGRO-FII |
| `fidc` | `FIDC/DOC/INF_MENSAL/DADOS/inf_mensal_fidc_{aaaamm}.zip` | `informes_mensais` — **experimental**: layout das tabelas precisa ser validado na 1ª execução real |

Os loaders são idempotentes (upsert por chave natural) e tolerantes a variações de
layout (encoding, caixa, renomeações da CVM 175); colunas obrigatórias ausentes
disparam `LayoutError` com diagnóstico das colunas encontradas.

### Tabelas

- `fundos` — dimensão por CNPJ (tipo de veículo, público-alvo, situação, admin/gestor, segmento)
- `informes_mensais` — série mensal por fundo (PL, nº de cotistas, valor de cota), única por (cnpj, competência)
- `fundos_metricas` — derivados para os filtros: PL médio 12m, PL e cotistas atuais
- `sync_state` — cursores de sincronização

## Execução no GitHub Actions

O ambiente de desenvolvimento pode não ter acesso de rede aos portais da CVM/B3;
a carga real roda pelo workflow **Ingestão CVM** (`workflow_dispatch`), que precisa
do secret `DATABASE_URL`. O CI (`pytest`) roda em todo push.
