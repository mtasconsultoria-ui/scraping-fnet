# scraping-fnet

Coleta e estruturação de dados de fundos (FII, FIDC, FIAGRO, FIP, FIF...) a partir do
Portal de Dados Abertos da CVM e do FundosNET, para filtros por características do
fundo e por conteúdo de documentos. Arquitetura completa em [`ARQUITETURA.md`](ARQUITETURA.md).

**Status: Fase 2** — Fase 1 (Dados Abertos da CVM: cadastro + informes mensais +
métricas) e Fase 2 (documentos do FNET: metadados incrementais, download para
storage, tabelas de domínio). Fases seguintes: full-text search de regulamentos,
API e UI no Vercel.

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

### Documentos do FNET (Fase 2)

```bash
# 1. IDs dos filtros do FNET (tipos de fundo, categorias) -> tabela dominios
python -m ingestor dominios-fnet

# 2. metadados de documentos (incremental por cursor de dataEntrega)
python -m ingestor sync-fnet --tipo-fundo 1 --categoria 9   # ids da tabela dominios
python -m ingestor sync-fnet --desde 2026-01-01             # sem filtros: tudo desde a data

# 3. download dos arquivos pendentes para o storage (regulamentos primeiro)
python -m ingestor download-docs --categorias Regulamento --limite 200
```

Configuração por env: `FNET_MIN_INTERVAL` (rate limit, default 1 req/s) e
`STORAGE_URL` — diretório local (default `./storage`) ou `s3://bucket/prefixo`
(S3/R2/Supabase Storage; `pip install -e .[s3]` e `S3_ENDPOINT_URL` para não-AWS).

O corpo do `downloadDocumento` do FNET costuma vir em base64: o cliente detecta
e decodifica pelos magic bytes, e registra o formato (`pdf`, `xml`, `zip`...).
Re-sincronizar metadados nunca desfaz o estado de download de um documento.

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
- `documentos` — metadados dos documentos do FNET (categoria, competência, situação,
  status de download, URL no storage, JSON original para diagnóstico)
- `dominios` — opções dos filtros do FNET (tipos de fundo, categorias de documento)
- `sync_state` — cursores de sincronização

## Execução no GitHub Actions

O ambiente de desenvolvimento pode não ter acesso de rede aos portais da CVM/B3;
a carga real roda pelo workflow **Ingestão CVM** (`workflow_dispatch`), que precisa
do secret `DATABASE_URL`. O CI (`pytest`) roda em todo push.
