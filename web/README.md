# web — API de consulta e interface (Vercel)

Next.js (App Router) que lê o PostgreSQL carregado pelo ingestor. Nenhum scraping
acontece aqui: as páginas e a API só consultam o banco, então cabem no modelo
serverless da Vercel.

## Rodando local

```bash
npm install
export DATABASE_URL="postgresql://usuario:senha@host:5432/fnet"
export PGSSL=disable          # só para Postgres local sem TLS
npm run dev                   # http://localhost:3000
```

Aceita também a URL no formato do SQLAlchemy (`postgresql+psycopg://...`), a mesma
usada pelo ingestor — o prefixo é normalizado.

## Testes

```bash
npm test                      # unitários (lógica de texto)
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/fnet_test npm test
                              # + integração: SQL real contra PostgreSQL
npm run typecheck
```

Os testes de integração criam as tabelas que usam e são **pulados** quando
`TEST_DATABASE_URL` não está definida.

## Endpoints

| Rota | O que faz |
|---|---|
| `GET /api/fundos` | busca por filtros e conteúdo; devolve fundos, documentos e trechos |
| `GET /api/fundos/{cnpj}` | cadastro, série mensal de PL/cotistas e documentos do fundo (CNPJ só com dígitos) |
| `GET /api/documentos/{id}?termo=` | metadados do documento e trechos do termo |
| `GET /api/dominios` | valores disponíveis para popular os filtros |

Parâmetros de `/api/fundos` (repetíveis ou separados por vírgula): `termo`, `tipo`,
`publicoAlvo`, `categoria`. Escalares: `situacao`, `plMin`, `plMax`, `cotistasMin`,
`cotistasMax`, `limite`, `offset`. Flags (`=1`): `todosTermos`, `excluirVedacoes`,
`literal`, `todasVersoes`.

```bash
curl -G localhost:3000/api/fundos \
  --data-urlencode "termo=CPR-F" \
  --data-urlencode "termo=Cédula do Produto Rural Financeira" \
  -d excluirVedacoes=1 -d tipo=FIDC -d tipo=FIAGRO -d plMin=100000000
```

### `totalAproximado` vs `exibidos`

`totalAproximado` é o `COUNT` do SQL, calculado **antes** da confirmação por regex
e do descarte de vedações — ambos feitos em JS. É um limite superior, não o número
exato; `exibidos` é quantos fundos de fato voltaram. A interface mostra os dois
para não afirmar um total que não verificou.

## Relação com o `ingestor` Python

A lógica de normalização e recorte de trechos existe nas duas linguagens
(`ingestor/textos.py` e `lib/textos.ts`) porque o Python indexa e o TypeScript
exibe. Os mesmos casos são cobertos nas duas suítes — inclusive o desempate do
prefiltro — e qualquer mudança em uma precisa ser refletida na outra.

No SQL, a confirmação por regex usa o operador `~` do PostgreSQL, que é POSIX e
**não** tem lookarounds; por isso `termoRegexSql` usa `\y` (fronteira de palavra),
uma aproximação deliberadamente mais permissiva. O filtro do banco nunca pode
descartar o que a confirmação em JS aceitaria — o JS é sempre a peneira fina.

## Deploy na Vercel

1. Novo projeto apontando para este repositório, com **Root Directory = `web`**.
2. Variável de ambiente `DATABASE_URL` com a string de conexão do Postgres
   (Neon/Supabase). Use a connection string **pooled** (PgBouncer): funções
   serverless escalam e esgotariam o limite de conexões diretas.
3. O build roda `next build`; não há passo de scraping no deploy.
