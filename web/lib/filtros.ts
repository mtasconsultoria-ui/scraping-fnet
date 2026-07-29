import { FILTROS_PADRAO, type Filtros } from "./busca";

const LIMITE_MAXIMO = 200;
/** Teto de linhas do export CSV — alto o bastante para o filtro inteiro. */
export const LIMITE_EXPORT = 2000;

function num(value: string | null | undefined): number | undefined {
  if (!value) return undefined;
  const n = Number(value.replace(/[^\d.-]/g, ""));
  return Number.isFinite(n) ? n : undefined;
}

function lista(params: URLSearchParams, chave: string): string[] {
  return params
    .getAll(chave)
    .flatMap((v) => v.split(","))
    .map((v) => v.trim())
    .filter(Boolean);
}

/** Traduz a query string (da UI ou de um cliente externo) em `Filtros`. */
export function filtrosDaQuery(
  params: URLSearchParams,
  opcoes: { export?: boolean } = {},
): Filtros {
  const categorias = lista(params, "categoria");
  const tetoLimite = opcoes.export ? LIMITE_EXPORT : LIMITE_MAXIMO;
  const limite = num(params.get("limite")) ?? (opcoes.export ? LIMITE_EXPORT : FILTROS_PADRAO.limite);
  return {
    ...FILTROS_PADRAO,
    termos: lista(params, "termo"),
    exigirTodosTermos: params.get("todosTermos") === "1",
    excluirVedacoes: params.get("excluirVedacoes") === "1",
    literal: params.get("literal") === "1",
    tiposVeiculo: lista(params, "tipo"),
    publicoAlvo: lista(params, "publicoAlvo"),
    situacao: params.get("situacao") ?? undefined,
    plMin: num(params.get("plMin")),
    plMax: num(params.get("plMax")),
    cotistasMin: num(params.get("cotistasMin")),
    cotistasMax: num(params.get("cotistasMax")),
    categorias: categorias.length ? categorias : FILTROS_PADRAO.categorias,
    apenasVigente: params.get("todasVersoes") !== "1",
    limite: Math.min(Math.max(limite, 1), tetoLimite),
    // o export ignora a paginação da tela: leva o filtro inteiro
    offset: opcoes.export ? 0 : num(params.get("offset")) ?? 0,
  };
}
