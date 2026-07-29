# scraping-fnet

Coleta e estruturação de dados de fundos (FII, FIDC, FIAGRO, FIP, FIF...) a partir do
Portal de Dados Abertos da CVM e do FundosNET, para filtros por características do
fundo e por conteúdo de documentos. Arquitetura completa em [`ARQUITETURA.md`](ARQUITETURA.md).

**Status: completo (Fases 1 a 5).** Ingestão dos Dados Abertos da CVM e dos
documentos do FundosNET, extração de texto dos regulamentos, busca combinando
filtros estruturados e conteúdo, API e interface Next.js para a Vercel
(ver [`web/`](web/README.md)), agendamento com alertas e export CSV.

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

### Busca por conteúdo dos regulamentos (Fase 3)

```bash
# 4. extrai o texto dos PDFs baixados
python -m ingestor extrair-textos --categorias Regulamento --limite 500

# 5. o caso motivador: fundos que admitem CPR-F, com o trecho do regulamento
python -m ingestor buscar \
  --termos "CPR-F" "Cédula do Produto Rural Financeira" \
  --excluir-vedacoes \
  --tipo FIDC FIAGRO --publico-alvo qualificado --pl-min 100000000
```

Como a busca funciona (`ingestor/busca.py`, a mesma camada que a API da Fase 4
vai expor):

- **Dois passos**: um `LIKE` no SQL reduz o universo (com índice trigram no
  Postgres, criado automaticamente quando a extensão `pg_trgm` está disponível),
  e um regex em Python confirma e recorta os trechos. É isso que separa um
  "CPR-F" de um "CPR" solto no meio do regulamento.
- **Tolerante à redação jurídica**: absorve plural e troca de conectores, então
  o termo `"Cédula do Produto Rural Financeira"` também casa
  *"cédulas de produto rural financeiras"*, inclusive quebrado por fim de linha
  no PDF. `--literal` desliga essa flexibilidade.
- **Menção não é permissão**: trechos precedidos de vedação (*"é vedada a
  aquisição de CPR-F"*) são marcados com `possivel_vedacao`; `--excluir-vedacoes`
  descarta os documentos em que o termo só aparece proibido.
- **Regulamento vigente** por padrão (só a versão mais recente de cada fundo);
  `--todas-versoes` inclui o histórico.
- Os trechos saem recortados do texto **original**, com acentuação e caixa
  preservadas para exibição na UI.

PDFs escaneados não têm camada de texto: são registrados com `origem='vazio'` e
formam a fila de OCR, processada por `extrair-textos --ocr` (requer
`pip install -e .[ocr]` mais `tesseract` e `poppler` no sistema).

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
- `documento_textos` — texto extraído (`texto`) e sua forma normalizada para busca
  (`texto_norm`: minúsculo, sem acento, **mesmo comprimento**, o que mantém os
  offsets alinhados e permite recortar o trecho do texto original)
- `dominios` — opções dos filtros do FNET (tipos de fundo, categorias de documento)
- `sync_state` — cursores de sincronização

## Interface e API (Fase 4)

```bash
cd web && npm install
export DATABASE_URL="postgresql://usuario:senha@host:5432/fnet"
npm run dev    # http://localhost:3000
```

Tela de filtros (tipo de veículo, público-alvo, PL médio, cotistas, termo no
documento) com os fundos encontrados, seus trechos de regulamento destacados e o
aviso de possível vedação. As mesmas consultas estão disponíveis em JSON para uso
programático. Detalhes de endpoints, testes e deploy: [`web/README.md`](web/README.md).

## Banco de dados: SQLite e PostgreSQL

O ingestor roda nos dois; **produção é PostgreSQL** e é ele que valida foreign
keys e resolve `ON CONFLICT`. O SQLite ignora foreign keys por padrão, então
`get_engine` liga `PRAGMA foreign_keys=ON` para que o desenvolvimento local
recuse os mesmos erros que a produção recusaria. A suíte roda contra os dois:

```bash
pytest                                                    # SQLite
TEST_DATABASE_URL=postgresql+psycopg://... pytest          # PostgreSQL
```

## Validando os portais antes do backfill

Dois comandos que **não usam banco, storage nem secrets** — servem para conferir,
com rede de verdade, se a API não documentada do FNET e os layouts da CVM
continuam válidos:

```bash
# 1. Uma requisição a cada portal; imprime colunas, chaves do JSON e bytes crus
python -m ingestor smoke

# 2. Pipeline completo em escala reduzida: acha fundos cujo regulamento cita um termo
python -m ingestor prospectar \
  --termos "CDCA" "Certificado de Depósito de Créditos do Agronegócio" \
  --alvo 5 --max-documentos 120
```

`prospectar` vai ao FNET, baixa os regulamentos mais recentes, extrai o texto,
procura os termos e cruza o CNPJ com o `cad_fi.csv` da CVM para informar a
situação cadastral — parando assim que junta o número de fundos pedido. É uma
amostra dos documentos mais recentes, não o universo completo: para isso existe a
ingestão, que cobre tudo e guarda o resultado.

Constatação da validação real que moldou os dois comandos: a busca do FNET com
`tipoFundo` vazio **não percorre o acervo** — devolve só os documentos do dia.
Por isso `sync-fnet` e `prospectar` varrem tipo a tipo (ids raspados da própria
página de filtros) quando nenhum tipo é informado, e quando os metadados vêm sem
CNPJ (comum), a prospecção o extrai do texto do próprio documento.

Ambos rodam também pelo workflow **Teste de fumaça** (`workflow_dispatch`), útil
quando a máquina local não alcança os portais.

## Agendamento e monitoramento (Fase 5)

A ingestão roda no GitHub Actions (não na Vercel: funções serverless têm limite de
tempo incompatível com scraping em lote):

| Workflow | Agendamento | O que faz |
|---|---|---|
| **Ingestão CVM** | segundas, 07:00 BRT | CSVs da CVM (atualizados semanalmente) + métricas |
| **Ingestão FNET** | diário, 06:00 BRT | domínios, metadados, download de regulamentos e extração de texto |

Ambos aceitam disparo manual (`workflow_dispatch`) e, ao falhar, **abrem uma issue**
com rótulo `ingestao` (comentando na existente, em vez de acumular duplicatas).

### O diagnóstico

```bash
python -m ingestor status            # relatório legível
python -m ingestor status --json     # mesmo conteúdo em JSON
python -m ingestor status --check    # sai com código 1 se houver problema
```

`--check` é o último passo de cada workflow agendado. Ele existe por causa da
falha mais traiçoeira: o job **passa sem erro nenhum, mas os dados param de
avançar** — layout do portal mudou, cursor travou, credencial expirou. Por isso o
diagnóstico não olha só o resultado da última execução; ele verifica o *frescor*:

- última execução de cada fonte falhou, ficou velha demais (9 dias para a CVM,
  3 para o FNET) ou nunca terminou;
- a competência mais recente dos informes está atrasada mais de 4 meses;
- avisos (não derrubam o status): documentos com erro de download, fila de OCR.

Os limiares vêm de `MAX_DIAS_SEM_SYNC_CVM` e `MAX_DIAS_SEM_SYNC_FNET`.

Cada execução fica registrada em `sync_runs` com status, estatísticas e erro — o
registro sobrevive inclusive quando a exceção suja a transação. A aplicação web
expõe o mesmo diagnóstico em `GET /api/health` (HTTP 503 quando há erro), pronto
para um monitor de uptime externo.

## Execução no GitHub Actions

O ambiente de desenvolvimento pode não ter acesso de rede aos portais da CVM/B3;
a carga real roda pelos workflows acima, que precisam do secret `DATABASE_URL`
(e `STORAGE_URL` para persistir os PDFs). O CI roda a cada push: as duas suítes,
contra SQLite e PostgreSQL.

## Limitação conhecida: migrações de schema

`init_db` usa `create_all`, que **cria tabelas novas mas não altera as existentes**.
Colunas adicionadas a uma tabela que já existe em produção exigem `ALTER TABLE`
manual. Quando o schema estabilizar, vale adotar Alembic; até lá, mudanças de
coluna precisam de um passo manual antes do deploy.
