# Arquitetura — Scraper FundosNET / CVM

Rotina back-end para coletar, estruturar e disponibilizar dados e documentos de fundos
(FIDC, FII, FIAGRO, FIP, FIF etc.) a partir do **FundosNET** (B3/CVM) e do
**Portal de Dados Abertos da CVM**, com filtros por características do fundo
(público-alvo, tipo de veículo, PL médio, nº de cotistas) e por **conteúdo dos
documentos** (ex.: regulamentos que permitem expressamente a aquisição de CPR-F),
para posterior publicação de uma interface gráfica no Vercel.

---

## 1. Insight central da arquitetura

O problema tem **duas naturezas de dados diferentes**, e a arquitetura trata cada uma
pela fonte mais eficiente:

| Necessidade | Fonte recomendada | Por quê |
|---|---|---|
| Filtros quantitativos/cadastrais (público-alvo, tipo, PL, nº de cotistas, segmento) | **Dados Abertos CVM** (`dados.cvm.gov.br`) — CSVs em massa | O mesmo Informe Mensal Estruturado que o FNET exibe fundo a fundo é publicado pela CVM em **CSV consolidado de todos os fundos**, atualizado semanalmente. Um download de ~poucos MB substitui milhares de downloads de XML individuais no FNET. |
| Documentos da estrutura (regulamento, atas de assembleia, fatos relevantes) e seus metadados | **API JSON não documentada do FNET** | É a fonte primária dos PDFs/XMLs. A tela de busca do FNET é alimentada por um endpoint JSON paginado, fácil de consumir programaticamente. |
| Filtros qualitativos por texto ("permite CPR-F") | **Extração de texto dos regulamentos + índice full-text no nosso banco** | Nenhuma fonte estruturada tem isso. Baixamos o regulamento vigente de cada fundo do universo filtrado, extraímos o texto e indexamos. |

Ou seja: **o FNET vira a fonte de documentos; a CVM aberta vira a fonte de atributos;
o nosso banco vira o produto** — é nele que os filtros combinados rodam rápido, sem
depender da disponibilidade/latência dos sites públicos na hora em que o usuário clica.

---

## 2. Visão geral

```
┌─────────────────────────── INGESTÃO (rotina agendada, Python) ───────────────────────────┐
│                                                                                          │
│  [A] Dados Abertos CVM ──► download CSVs (informes mensais + cadastro) ──► normaliza ─┐  │
│                                                                                       │  │
│  [B] FNET API JSON ──► lista documentos por fundo/categoria ──► metadados ────────────┤  │
│        └──► downloadDocumento?id= ──► PDF/XML ──► Object Storage (Blob/S3)            │  │
│                                                                                       │  │
│  [C] Extração de texto (PDF ──► texto) ──► índice full-text ──────────────────────────┤  │
│                                                                                       ▼  │
└───────────────────────────────────────────────────────────────► Postgres (Neon/Supabase) │
                                                                          │
                        ┌─────────────────────────────────────────────────┘
                        ▼
              API de consulta (Next.js API routes / FastAPI)
                        │
                        ▼
              Front-end Next.js no Vercel (tela de filtros + resultados + viewer)
```

---

## 3. Fontes de dados em detalhe

### 3.1 FNET — API JSON não documentada

A tela pública `https://fnet.bmfbovespa.com.br/fnet/publico/abrirGerenciadorDocumentosCVM`
é alimentada por um endpoint estilo DataTables:

```
GET https://fnet.bmfbovespa.com.br/fnet/publico/pesquisarGerenciadorDocumentosDados
```

Parâmetros principais (query string):

| Parâmetro | Significado |
|---|---|
| `s` / `l` | offset / page size (paginação) |
| `o[0][dataEntrega]=desc` | ordenação |
| `tipoFundo` | tipo do veículo (ex.: 1 = FII; demais IDs mapeados do HTML dos filtros) |
| `cnpjFundo` | CNPJ do fundo (só dígitos) |
| `idCategoriaDocumento` | categoria (Regulamento, Assembleia, Fato Relevante, Informe Mensal Estruturado...) |
| `idTipoDocumento` / `idEspecieDocumento` | subclassificações |
| `dataInicial` / `dataFinal` | janela de entrega |

Resposta: JSON `{recordsTotal, data: [{id, descricaoFundo, categoriaDocumento,
tipoDocumento, dataReferencia, dataEntrega, situacaoDocumento, versao, ...}]}`.

Download do arquivo:

```
GET https://fnet.bmfbovespa.com.br/fnet/publico/downloadDocumento?id={id}
```

Observações operacionais (validar na primeira execução real — este ambiente de
desenvolvimento não tem acesso de rede ao domínio da B3):

- O conteúdo costuma vir **codificado em base64** no corpo; decodificar para obter o PDF/XML.
- O servidor rejeita user-agents "de robô" — enviar headers de navegador
  (`User-Agent`, `Accept`, `Referer`) e aplicar **rate limiting + backoff exponencial**.
- Os IDs de `tipoFundo`/`idCategoriaDocumento`/`idTipoDocumento` devem ser raspados
  uma vez dos `<select>` da página de filtros e versionados como tabela de domínio no
  banco (eles mudam raramente, mas mudam — ex.: novas categorias pós-CVM 175).

### 3.2 Dados Abertos CVM — CSVs consolidados

Datasets relevantes (todos com CSVs anuais/mensais, atualização semanal, histórico ~5 anos):

- **FII — Informe Mensal Estruturado**: `dados.cvm.gov.br/dataset/fii-doc-inf_mensal`
  (arquivos `geral`, `complemento`, `ativo_passivo` — trazem público-alvo, segmento,
  PL, nº de cotistas, mandato etc.). FIAGRO-FII aparece aqui também.
- **FIDC — Informe Mensal**: `dados.cvm.gov.br/dataset/fidc-doc-inf_mensal`
  (tabelas I a X do informe — PL, cotistas por classe, carteira por tipo de recebível).
- **Cadastro de fundos** (`fi-cad` / registro de fundos CVM 175): situação, classificação,
  tipo, administrador, gestor — chave para montar a dimensão `fundos`.
- Informes de **FIP** e informes trimestrais/anuais de FII para enriquecimento futuro.

A partir desses CSVs calculamos derivados como **PL médio** (janela móvel de N meses)
e **nº de cotistas mais recente** — métricas que seriam caras de montar via FNET.

### 3.3 O que só o documento responde (caso CPR-F)

"Permite expressamente a aquisição de CPR-F" não existe em campo estruturado.
Fluxo:

1. Filtrar o universo de interesse via banco (ex.: FIDCs e FIAGROs ativos).
2. Para cada fundo, localizar via FNET o **regulamento vigente** (categoria Regulamento,
   maior `dataReferencia`/`versao`, situação "Ativo").
3. Baixar o PDF, extrair texto (`pdfplumber`; fallback OCR com `pytesseract` para
   regulamentos antigos escaneados).
4. Gravar o texto em `documento_textos` com índice **full-text (tsvector, config
   `portuguese`)** + busca por expressões literais (`CPR-F`, "Cédula do Produto Rural").
5. A UI mostra os fundos com match e os **trechos destacados** do regulamento onde o
   termo aparece, com link para o PDF.

---

## 4. Componentes

### 4.1 Ingestão (`/ingestor`, Python)

Rotina batch idempotente, executada por agendador (não roda no Vercel — ver §7):

- `cvm_bulk.py` — baixa/atualiza CSVs da CVM, faz upsert em `fundos` e `informes_mensais`.
- `fnet_client.py` — cliente HTTP do FNET (sessão com headers de navegador, retry,
  rate limit ~1 req/s, cache local por `id` de documento).
- `fnet_sync.py` — varre documentos novos por categoria/tipo desde o último sync
  (cursor por `dataEntrega`), grava metadados em `documentos`, baixa arquivos
  priorizados (regulamentos primeiro) para o object storage.
- `text_extract.py` — PDF → texto → `documento_textos` (+ tsvector).
- Tudo **incremental**: estado de sincronização em tabela `sync_state`; reprocessos
  não duplicam nada (chave natural = id do documento FNET / CNPJ+competência).

### 4.2 Banco de dados — Postgres gerenciado (Neon ou Supabase)

Integração nativa com Vercel, tier gratuito suficiente para começar, e full-text search
embutido (dispensa Elasticsearch nesta escala). Modelo mínimo:

```
fundos            (cnpj PK, denominacao, tipo_veiculo, publico_alvo, situacao,
                   administrador, gestor, segmento, dt_registro, ...)
informes_mensais  (cnpj FK, competencia, pl, num_cotistas, valor_cota, ...,
                   UNIQUE(cnpj, competencia))
fundos_metricas   (visão materializada: pl_medio_12m, cotistas_atual, ...)
documentos        (id_fnet PK, cnpj FK, categoria, tipo, especie, data_referencia,
                   data_entrega, versao, situacao, url_storage, status_download)
documento_textos  (id_fnet FK, texto, texto_tsv tsvector, num_paginas)
dominios          (grupo, id_fnet, rotulo)          -- tipos de fundo, categorias etc.
sync_state        (fonte, cursor, atualizado_em)
```

### 4.3 Object storage

Vercel Blob (mais simples no ecossistema) ou S3/Supabase Storage. Guarda os PDFs/XMLs
originais; o banco guarda só a URL. A UI serve o documento a partir daí — não
re-baixa do FNET a cada visualização.

### 4.4 API de consulta

Endpoints (Next.js API routes no mesmo projeto Vercel, padrão dos demais projetos):

```
GET /api/fundos?tipo=FIDC&publico_alvo=profissional&pl_min=100000000
               &cotistas_min=10&texto=CPR-F&page=1
GET /api/fundos/{cnpj}                      → detalhe + série de PL/cotistas
GET /api/fundos/{cnpj}/documentos?categoria=regulamento
GET /api/documentos/{id}                    → metadados + URL do arquivo + trechos com match
GET /api/dominios                           → opções dos filtros (para popular os selects)
```

Como a API só lê o Postgres, ela cabe perfeitamente em serverless do Vercel
(respostas em ms, sem scraping em request path).

### 4.5 Front-end (Vercel, Next.js)

- Painel de filtros: tipo de veículo, público-alvo, faixas de PL médio e nº de cotistas,
  categoria de documento disponível, busca textual em regulamento.
- Tabela de resultados com ordenação e export CSV.
- Drawer/página de detalhe do fundo: dados cadastrais, gráfico de PL/cotistas,
  lista de documentos com viewer de PDF embutido e trechos destacados do termo buscado.

---

## 5. Fluxo do exemplo motivador (CPR-F)

1. Usuário filtra: veículo = FIDC + FIAGRO, público-alvo = qualquer,
   texto no regulamento = `"CPR-F" OU "Cédula do Produto Rural Financeira"`.
2. API consulta `fundos ⋈ fundos_metricas ⋈ documento_textos` (uma query SQL).
3. UI lista os fundos, cada um com PL médio, cotistas, administrador e os trechos do
   regulamento onde o termo aparece; clique abre o PDF do regulamento no viewer.

---

## 6. Estrutura de repositório proposta

```
scraping-fnet/
├── ingestor/                  # Python 3.12, uv/poetry
│   ├── fnet_client.py
│   ├── fnet_sync.py
│   ├── cvm_bulk.py
│   ├── text_extract.py
│   ├── db.py                  # SQLAlchemy + migrações (alembic)
│   └── cli.py                 # `python -m ingestor sync-cvm | sync-fnet | extract-text`
├── web/                       # Next.js (App Router) — API routes + UI → deploy Vercel
│   ├── app/
│   │   ├── api/...
│   │   └── (ui)/...
│   └── lib/db.ts
├── db/migrations/
├── .github/workflows/ingest.yml   # cron do agendamento
└── ARQUITETURA.md
```

Alternativa: separar `ingestor` em repo próprio se o padrão dos outros projetos for
um repo por app Vercel. Manter junto simplifica versionamento do schema.

## 7. Agendamento e deploy

- **Ingestão**: GitHub Actions com `schedule` (ex.: diário 07:00 BRT para FNET;
  semanal para CSVs da CVM, que atualizam semanalmente). Actions comporta jobs longos
  (até 6h), diferente das functions do Vercel — por isso o scraping **não** roda no Vercel.
- **Web**: deploy contínuo no Vercel apontando para `web/`, com `DATABASE_URL` e
  token do Blob como env vars.
- **Backfill inicial**: rodado manualmente (workflow_dispatch) — carga histórica dos
  CSVs e download dos regulamentos vigentes do universo ativo.

## 8. Riscos e cuidados

- **API não documentada**: o FNET pode mudar parâmetros/formato sem aviso. Mitigação:
  camada `fnet_client` isolada + testes de contrato que rodam no início de cada sync e
  falham alto (notificação) se o shape da resposta mudar.
- **Bloqueio/ratelimit da B3**: headers de navegador, 1 req/s, backoff, retomada por
  cursor. Nunca baixar em massa o que a CVM já dá em CSV.
- **PDFs escaneados**: parte dos regulamentos antigos exige OCR — tratar como fila
  separada (mais lenta) e marcar `documento_textos.origem = 'ocr'`.
- **CVM 175**: a migração FI → classes/subclasses está renomeando datasets e colunas
  no portal da CVM; fixar versões de layout por ano e validar schema no load.
- **Uso responsável**: dados públicos, mas respeitar os termos dos portais; identificar
  o client via User-Agent honesto se os portais aceitarem, e cachear agressivamente.

## 9. Roadmap sugerido

| Fase | Entrega | Status |
|---|---|---|
| 1 | `cvm_bulk` + schema Postgres + carga de fundos/informes (filtros quantitativos já funcionam) | ✅ concluída |
| 2 | `fnet_client` + `fnet_sync` de metadados de documentos + download de regulamentos vigentes | ✅ concluída |
| 3 | Extração de texto + busca por conteúdo (caso CPR-F de ponta a ponta) | ✅ concluída |
| 4 | API de consulta + UI Next.js no Vercel | ✅ concluída |
| 5 | Agendamento, monitoramento (alertas de falha de sync) e export CSV na UI | ✅ concluída |

### Ajuste de rumo na Fase 3

O desenho original previa `tsvector` com dicionário `portuguese`. A implementação
usa **prefiltro `LIKE` no SQL + confirmação por regex em Python** porque o caso de
uso é busca por *expressão jurídica exata* ("CPR-F", "Cédula do Produto Rural
Financeira"), não por linguagem natural: o stemmer do Postgres quebraria "CPR-F"
em tokens e confundiria com "CPR" solto, que é justamente a distinção que importa.
A abordagem adotada dá controle explícito sobre plural, conectores e fronteiras de
palavra, funciona igual em SQLite (dev) e Postgres (produção), e continua rápida —
o `LIKE` usa índice trigram (`pg_trgm`) quando disponível. Se no futuro entrar
busca por linguagem natural sobre os documentos, `tsvector` volta como camada
complementar, sem substituir esta.

Duas descobertas da implementação viraram funcionalidade: regulamentos escrevem o
mesmo ativo em plural e com conectores trocados (daí o matching flexível), e
mencionar um ativo não é permiti-lo — "é vedada a aquisição de CPR-F" casaria a
busca ingenuamente, então trechos precedidos de vedação são sinalizados e podem
ser filtrados.

### Ajuste de rumo na Fase 4

A lógica de busca ficou **duplicada em Python e TypeScript** (`ingestor/textos.py`
e `web/lib/textos.ts`). É uma duplicação deliberada: manter a UI na Vercel — o
padrão dos demais projetos — significa TypeScript no servidor da aplicação, e a
alternativa (um serviço Python separado só para a busca) acrescentaria um
componente para operar e pagar. As duas implementações são cobertas pelos mesmos
casos de teste, inclusive nos detalhes que poderiam divergir silenciosamente
(desempate do prefiltro, tratamento de plural). Se a lógica textual crescer muito,
vale reavaliar em favor de um serviço único.

No PostgreSQL a confirmação também roda no banco (operador `~`), para que só os
documentos relevantes trafeguem até a função serverless. Como o regex POSIX não
tem lookarounds, a versão SQL usa `\y` e é deliberadamente **mais permissiva** que
a de JS: o banco peneira grosso, o JS confirma. Consequência visível: o `COUNT` do
SQL é anterior ao descarte de vedações, então a API expõe `totalAproximado`
(limite superior) e `exibidos` (o que de fato passou), em vez de um "total" que
não corresponderia à lista.

A Fase 4 também revelou um defeito das fases anteriores: sem `relationship()`
declarado, o SQLAlchemy desconhecia a dependência entre entidades e emitia
`INSERT` em `documentos` antes de `fundos`. O SQLite aceitava (ignora foreign keys
por padrão), o PostgreSQL de produção rejeitaria. Corrigido com os relacionamentos,
com `PRAGMA foreign_keys=ON` no SQLite e rodando a suíte também contra PostgreSQL.

### Ajuste de rumo na Fase 5

O plano falava em "alertas de falha de sync". Na prática, a falha que mais
importa **não levanta exceção**: o job roda, termina com sucesso e nada de novo
entra no banco — porque o layout do portal mudou de um jeito tolerado, o cursor
travou ou a credencial expirou. Um alerta preso a exceções não veria nada disso.

Por isso o monitoramento é baseado em *frescor*, não só em erro: `sync_runs`
guarda o histórico de execuções, e `status --check` falha quando a última
execução de uma fonte envelheceu além do limite ou quando a competência dos
informes parou de avançar. É esse comando que fecha cada workflow agendado, de
modo que dados parados derrubam o job e abrem uma issue — o mesmo caminho de um
erro explícito. `GET /api/health` expõe o diagnóstico para monitoração externa.

Fica registrada uma limitação: `create_all` cria tabelas novas mas não altera as
existentes, então colunas novas em produção exigem `ALTER TABLE` manual. Adotar
Alembic faz sentido quando o schema estabilizar; antes disso, migrações versionadas
seriam custo sem retorno.
