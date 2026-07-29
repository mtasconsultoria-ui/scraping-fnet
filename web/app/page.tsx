import { buscar, type FundoResultado } from "@/lib/busca";
import { query } from "@/lib/db";
import { filtrosDaQuery } from "@/lib/filtros";
import type { Trecho } from "@/lib/textos";

export const dynamic = "force-dynamic";

type SearchParams = Record<string, string | string[] | undefined>;

function paramsFrom(searchParams: SearchParams): URLSearchParams {
  const params = new URLSearchParams();
  for (const [chave, valor] of Object.entries(searchParams)) {
    if (Array.isArray(valor)) valor.forEach((v) => params.append(chave, v));
    else if (valor != null) params.append(chave, valor);
  }
  return params;
}

const moeda = new Intl.NumberFormat("pt-BR", { notation: "compact", maximumFractionDigits: 1 });
const inteiro = new Intl.NumberFormat("pt-BR");

function formataCnpj(cnpj: string): string {
  return cnpj.replace(/^(\d{2})(\d{3})(\d{3})(\d{4})(\d{2})$/, "$1.$2.$3/$4-$5");
}

async function opcoes() {
  try {
    const [tipos, publicos, categorias] = await Promise.all([
      query<{ valor: string }>(
        `SELECT DISTINCT tipo_veiculo AS valor FROM fundos WHERE tipo_veiculo IS NOT NULL ORDER BY 1`,
      ),
      query<{ valor: string }>(
        `SELECT DISTINCT publico_alvo AS valor FROM fundos WHERE publico_alvo IS NOT NULL ORDER BY 1`,
      ),
      query<{ valor: string }>(
        `SELECT DISTINCT categoria AS valor FROM documentos WHERE categoria IS NOT NULL ORDER BY 1`,
      ),
    ]);
    return {
      tipos: tipos.map((t) => t.valor),
      publicos: publicos.map((p) => p.valor),
      categorias: categorias.map((c) => c.valor),
    };
  } catch {
    return { tipos: [], publicos: [], categorias: [] };
  }
}

function TrechoView({ trecho }: { trecho: Trecho }) {
  return (
    <div className={trecho.possivelVedacao ? "trecho vedacao" : "trecho"}>
      …{trecho.texto.slice(0, trecho.inicio)}
      <mark>{trecho.texto.slice(trecho.inicio, trecho.fim)}</mark>
      {trecho.texto.slice(trecho.fim)}…
      {trecho.possivelVedacao && (
        <span className="aviso-vedacao">⚠ possível vedação — confira o regulamento</span>
      )}
    </div>
  );
}

function FundoCard({ fundo }: { fundo: FundoResultado }) {
  return (
    <article className="fundo">
      <h2>{fundo.denominacao ?? "(sem denominação)"}</h2>
      <div className="cnpj">{formataCnpj(fundo.cnpj)}</div>
      <div className="tags">
        {fundo.tipoVeiculo && <span className="tag">{fundo.tipoVeiculo}</span>}
        {fundo.publicoAlvo && <span className="tag neutro">{fundo.publicoAlvo}</span>}
        {fundo.segmento && <span className="tag neutro">{fundo.segmento}</span>}
      </div>
      <div className="metricas">
        <div>
          PL médio 12m
          <b>{fundo.plMedio12m != null ? `R$ ${moeda.format(fundo.plMedio12m)}` : "—"}</b>
        </div>
        <div>
          Cotistas
          <b>{fundo.cotistasAtual != null ? inteiro.format(fundo.cotistasAtual) : "—"}</b>
        </div>
        <div>
          Administrador
          <b>{fundo.administrador ?? "—"}</b>
        </div>
        <div>
          Gestor
          <b>{fundo.gestor ?? "—"}</b>
        </div>
      </div>
      {fundo.documentos.map((doc) => (
        <div className="doc" key={doc.idFnet}>
          <div className="doc-cab">
            <span>
              <strong>{doc.categoria}</strong>
              {doc.dataReferencia ? ` · ${doc.dataReferencia}` : ""}
            </span>
            <a
              href={`https://fnet.bmfbovespa.com.br/fnet/publico/downloadDocumento?id=${doc.idFnet}`}
              target="_blank"
              rel="noreferrer"
            >
              abrir no FNET ↗
            </a>
          </div>
          {doc.trechos.map((trecho, i) => (
            <TrechoView key={i} trecho={trecho} />
          ))}
        </div>
      ))}
    </article>
  );
}

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const resolvidos = await searchParams;
  const params = paramsFrom(resolvidos);
  const filtros = filtrosDaQuery(params);
  const { tipos, publicos, categorias } = await opcoes();

  let resultado: Awaited<ReturnType<typeof buscar>> | null = null;
  let erro: string | null = null;
  const consultou = params.toString().length > 0;
  if (consultou) {
    try {
      resultado = await buscar(filtros);
    } catch (e) {
      erro = (e as Error).message;
    }
  }

  const proximaPagina = new URLSearchParams(params);
  proximaPagina.set("offset", String(filtros.offset + filtros.limite));
  const paginaAnterior = new URLSearchParams(params);
  paginaAnterior.set("offset", String(Math.max(0, filtros.offset - filtros.limite)));

  return (
    <main className="wrap">
      <header>
        <h1>Consulta de fundos</h1>
        <p>
          Filtros por características do fundo e por conteúdo dos documentos.
          Dados do Informe Mensal Estruturado (Dados Abertos CVM) e documentos do FundosNET.
        </p>
      </header>

      <form className="painel" method="get">
        <div className="grid">
          <div>
            <label htmlFor="termo">Termo no documento</label>
            <input
              id="termo"
              name="termo"
              type="text"
              placeholder="CPR-F"
              defaultValue={filtros.termos[0] ?? ""}
            />
          </div>
          <div>
            <label htmlFor="termo2">Sinônimo (opcional)</label>
            <input
              id="termo2"
              name="termo"
              type="text"
              placeholder="Cédula do Produto Rural Financeira"
              defaultValue={filtros.termos[1] ?? ""}
            />
          </div>
          <div>
            <label htmlFor="tipo">Tipo de veículo</label>
            <select id="tipo" name="tipo" multiple defaultValue={filtros.tiposVeiculo}>
              {tipos.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="publicoAlvo">Público-alvo</label>
            <select id="publicoAlvo" name="publicoAlvo" multiple defaultValue={filtros.publicoAlvo}>
              {publicos.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="categoria">Categoria do documento</label>
            <select id="categoria" name="categoria" multiple defaultValue={filtros.categorias}>
              {(categorias.length ? categorias : ["Regulamento"]).map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="plMin">PL médio 12m mínimo (R$)</label>
            <input id="plMin" name="plMin" type="number" step="1000000" defaultValue={filtros.plMin} />
          </div>
          <div>
            <label htmlFor="plMax">PL médio 12m máximo (R$)</label>
            <input id="plMax" name="plMax" type="number" step="1000000" defaultValue={filtros.plMax} />
          </div>
          <div>
            <label htmlFor="cotistasMin">Cotistas (mínimo)</label>
            <input id="cotistasMin" name="cotistasMin" type="number" defaultValue={filtros.cotistasMin} />
          </div>
        </div>

        <div className="checks">
          <label>
            <input type="checkbox" name="excluirVedacoes" value="1" defaultChecked={filtros.excluirVedacoes} />
            Só quem permite (descarta vedações)
          </label>
          <label>
            <input type="checkbox" name="todosTermos" value="1" defaultChecked={filtros.exigirTodosTermos} />
            Exigir todos os termos
          </label>
          <label>
            <input type="checkbox" name="literal" value="1" defaultChecked={filtros.literal} />
            Busca literal
          </label>
          <label>
            <input type="checkbox" name="todasVersoes" value="1" defaultChecked={!filtros.apenasVigente} />
            Incluir versões antigas
          </label>
        </div>

        <div className="acoes">
          <button type="submit">Buscar</button>
          <a className="limpar" href="/">
            limpar filtros
          </a>
        </div>
      </form>

      {erro && (
        <div className="erro">
          <strong>Falha na consulta:</strong> {erro}
          <br />
          Verifique se <code>DATABASE_URL</code> aponta para o banco carregado pelo ingestor.
        </div>
      )}

      {!erro && resultado && (
        <>
          <div className="resumo">
            <span>
              <strong>{resultado.exibidos}</strong> fundo(s)
              {filtros.offset > 0 && <> nesta página</>}
              {filtros.termos.length > 0 && <> para {filtros.termos.map((t) => `“${t}”`).join(" ou ")}</>}
            </span>
            {/* o COUNT do SQL antecede a confirmação por regex e o descarte de
                vedações, então é apresentado como aproximação, nunca como exato */}
            {resultado.totalAproximado > resultado.exibidos && (
              <span>
                de até {resultado.totalAproximado} candidato(s)
                {filtros.offset > 0 && <> · a partir do {filtros.offset + 1}º</>}
              </span>
            )}
          </div>

          {resultado.fundos.length === 0 ? (
            <div className="vazio">
              Nenhum fundo atende a esses filtros. Tente remover restrições ou adicionar
              sinônimos do termo.
            </div>
          ) : (
            resultado.fundos.map((fundo) => <FundoCard key={fundo.cnpj} fundo={fundo} />)
          )}

          <div className="paginacao">
            {filtros.offset > 0 && <a href={`/?${paginaAnterior}`}>← anteriores</a>}
            {filtros.offset + filtros.limite < resultado.totalAproximado && (
              <a href={`/?${proximaPagina}`}>próximos →</a>
            )}
          </div>
        </>
      )}

      {!consultou && (
        <div className="vazio">
          Informe um termo (ex.: <strong>CPR-F</strong>) e/ou filtros e clique em Buscar.
        </div>
      )}

      <footer>
        Fontes: Portal de Dados Abertos da CVM e FundosNET (B3). Os trechos são extraídos
        automaticamente dos documentos — confira sempre o inteiro teor antes de decidir.
      </footer>
    </main>
  );
}
