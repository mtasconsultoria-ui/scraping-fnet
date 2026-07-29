/**
 * Consulta de fundos por filtros estruturados e conteúdo de documentos.
 *
 * Espelha `ingestor/busca.py`, com uma diferença de execução: aqui a
 * confirmação por regex também acontece no PostgreSQL (operador `~`, acelerado
 * pelo índice trigram), de modo que só os documentos realmente relevantes
 * trafegam até a função serverless. O JS refaz a confirmação ao extrair os
 * trechos — o SQL é sempre a peneira mais grossa, nunca a mais fina.
 */
import { query } from "./db";
import {
  encontrarTrechos,
  fold,
  termoRegex,
  termoRegexSql,
  tokenPrefiltro,
  type Trecho,
} from "./textos";

export interface Filtros {
  termos: string[];
  exigirTodosTermos: boolean;
  excluirVedacoes: boolean;
  literal: boolean;
  tiposVeiculo: string[];
  publicoAlvo: string[];
  situacao?: string;
  plMin?: number;
  plMax?: number;
  cotistasMin?: number;
  cotistasMax?: number;
  categorias: string[];
  apenasVigente: boolean;
  maxTrechos: number;
  limite: number;
  offset: number;
}

export const FILTROS_PADRAO: Filtros = {
  termos: [],
  exigirTodosTermos: false,
  excluirVedacoes: false,
  literal: false,
  tiposVeiculo: [],
  publicoAlvo: [],
  categorias: ["Regulamento"],
  apenasVigente: true,
  maxTrechos: 3,
  limite: 25,
  offset: 0,
};

export interface DocumentoMatch {
  idFnet: number;
  categoria: string | null;
  tipo: string | null;
  dataReferencia: string | null;
  dataEntrega: string | null;
  formato: string | null;
  urlStorage: string | null;
  trechos: Trecho[];
}

export interface FundoResultado {
  cnpj: string;
  denominacao: string | null;
  tipoVeiculo: string | null;
  publicoAlvo: string | null;
  situacao: string | null;
  administrador: string | null;
  gestor: string | null;
  segmento: string | null;
  plAtual: number | null;
  plMedio12m: number | null;
  cotistasAtual: number | null;
  competenciaUltima: string | null;
  documentos: DocumentoMatch[];
}

export interface Resultado {
  fundos: FundoResultado[];
  /**
   * Limite SUPERIOR de fundos que atendem aos filtros.
   *
   * Quando há busca textual, o COUNT do SQL é calculado antes da confirmação
   * por regex e da exclusão de vedações — ambas feitas aqui, em JS. O número
   * exato só seria conhecido processando todas as páginas, então a UI mostra
   * quantos foram de fato exibidos e trata este valor como aproximação.
   */
  totalAproximado: number;
  /** Fundos efetivamente retornados nesta página (após todas as confirmações). */
  exibidos: number;
}

/** Acumula fragmentos de SQL com seus parâmetros posicionais ($1, $2, ...). */
class Params {
  readonly values: unknown[] = [];
  add(value: unknown): string {
    this.values.push(value);
    return `$${this.values.length}`;
  }
}

function condicoesFundo(filtros: Filtros, p: Params): string[] {
  const cond: string[] = [];
  if (filtros.tiposVeiculo.length) cond.push(`f.tipo_veiculo = ANY(${p.add(filtros.tiposVeiculo)})`);
  if (filtros.situacao) cond.push(`lower(f.situacao) = lower(${p.add(filtros.situacao)})`);
  if (filtros.publicoAlvo.length) {
    const alvos = filtros.publicoAlvo.map((t) => `lower(f.publico_alvo) LIKE ${p.add(`%${t.toLowerCase()}%`)}`);
    cond.push(`(${alvos.join(" OR ")})`);
  }
  if (filtros.plMin != null) cond.push(`m.pl_medio_12m >= ${p.add(filtros.plMin)}`);
  if (filtros.plMax != null) cond.push(`m.pl_medio_12m <= ${p.add(filtros.plMax)}`);
  if (filtros.cotistasMin != null) cond.push(`m.cotistas_atual >= ${p.add(filtros.cotistasMin)}`);
  if (filtros.cotistasMax != null) cond.push(`m.cotistas_atual <= ${p.add(filtros.cotistasMax)}`);
  return cond;
}

function condicoesDocumento(filtros: Filtros, p: Params): string[] {
  const cond: string[] = [];
  if (filtros.categorias.length) cond.push(`d.categoria = ANY(${p.add(filtros.categorias)})`);
  if (filtros.apenasVigente) {
    cond.push(`d.id_fnet = (
      SELECT d2.id_fnet FROM documentos d2
      WHERE d2.cnpj = f.cnpj AND d2.categoria = d.categoria
      ORDER BY d2.data_entrega DESC NULLS LAST, d2.id_fnet DESC LIMIT 1)`);
  }
  return cond;
}

function condicoesTexto(filtros: Filtros, p: Params): string {
  // por termo: prefiltro LIKE (índice trigram) + regex com fronteira de palavra
  const porTermo = filtros.termos.map((termo) => {
    const like = `t.texto_norm LIKE ${p.add(`%${tokenPrefiltro(termo)}%`)}`;
    const regex = `t.texto_norm ~ ${p.add(termoRegexSql(termo, !filtros.literal))}`;
    return `(${like} AND ${regex})`;
  });
  return `(${porTermo.join(filtros.exigirTodosTermos ? " AND " : " OR ")})`;
}

const COLUNAS_FUNDO = `
  f.cnpj, f.denominacao, f.tipo_veiculo, f.publico_alvo, f.situacao,
  f.administrador, f.gestor, f.segmento,
  m.pl_atual, m.pl_medio_12m, m.cotistas_atual, m.competencia_ultima`;

const ORDENACAO = "ORDER BY m.pl_medio_12m DESC NULLS LAST, f.cnpj";

function toNumber(value: unknown): number | null {
  if (value == null) return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Datas do `pg` viram Date; serializa como ISO para não vazar formato local. */
export function toIso(value: unknown, apenasData = false): string | null {
  if (value == null) return null;
  const data = value instanceof Date ? value : new Date(String(value));
  if (Number.isNaN(data.getTime())) return String(value);
  const iso = data.toISOString();
  return apenasData ? iso.slice(0, 10) : iso;
}

function montaFundo(row: Record<string, unknown>): FundoResultado {
  return {
    cnpj: String(row.cnpj),
    denominacao: (row.denominacao as string) ?? null,
    tipoVeiculo: (row.tipo_veiculo as string) ?? null,
    publicoAlvo: (row.publico_alvo as string) ?? null,
    situacao: (row.situacao as string) ?? null,
    administrador: (row.administrador as string) ?? null,
    gestor: (row.gestor as string) ?? null,
    segmento: (row.segmento as string) ?? null,
    plAtual: toNumber(row.pl_atual),
    plMedio12m: toNumber(row.pl_medio_12m),
    cotistasAtual: toNumber(row.cotistas_atual),
    competenciaUltima: toIso(row.competencia_ultima, true),
    documentos: [],
  };
}

export async function buscar(filtros: Filtros): Promise<Resultado> {
  return filtros.termos.length ? buscarComTexto(filtros) : buscarSemTexto(filtros);
}

async function buscarSemTexto(filtros: Filtros): Promise<Resultado> {
  const p = new Params();
  const cond = condicoesFundo(filtros, p);
  const where = cond.length ? `WHERE ${cond.join(" AND ")}` : "";
  const base = `FROM fundos f LEFT JOIN fundos_metricas m ON m.cnpj = f.cnpj ${where}`;

  const totalRows = await query<{ total: string }>(`SELECT COUNT(*) AS total ${base}`, p.values);
  const rows = await query<Record<string, unknown>>(
    `SELECT ${COLUNAS_FUNDO} ${base} ${ORDENACAO} LIMIT ${p.add(filtros.limite)} OFFSET ${p.add(filtros.offset)}`,
    p.values,
  );
  const fundos = rows.map(montaFundo);
  return {
    fundos,
    totalAproximado: Number(totalRows[0]?.total ?? 0),
    exibidos: fundos.length,
  };
}

async function buscarComTexto(filtros: Filtros): Promise<Resultado> {
  // Etapa 1: quais fundos casam (paginação sobre fundos, não sobre documentos)
  const p1 = new Params();
  const cond = [
    ...condicoesFundo(filtros, p1),
    ...condicoesDocumento(filtros, p1),
    condicoesTexto(filtros, p1),
  ];
  const base = `
    FROM fundos f
    JOIN documentos d ON d.cnpj = f.cnpj
    JOIN documento_textos t ON t.id_fnet = d.id_fnet
    LEFT JOIN fundos_metricas m ON m.cnpj = f.cnpj
    WHERE ${cond.join(" AND ")}`;

  const totalRows = await query<{ total: string }>(
    `SELECT COUNT(DISTINCT f.cnpj) AS total ${base}`,
    p1.values,
  );
  const fundoRows = await query<Record<string, unknown>>(
    `SELECT DISTINCT ${COLUNAS_FUNDO} ${base} ${ORDENACAO}
     LIMIT ${p1.add(filtros.limite)} OFFSET ${p1.add(filtros.offset)}`,
    p1.values,
  );
  if (fundoRows.length === 0) {
    return { fundos: [], totalAproximado: Number(totalRows[0]?.total ?? 0), exibidos: 0 };
  }

  // Etapa 2: documentos e textos apenas dos fundos desta página
  const cnpjs = fundoRows.map((r) => String(r.cnpj));
  const p2 = new Params();
  const cond2 = [
    `f.cnpj = ANY(${p2.add(cnpjs)})`,
    ...condicoesDocumento(filtros, p2),
    condicoesTexto(filtros, p2),
  ];
  const docRows = await query<Record<string, unknown>>(
    `SELECT f.cnpj, d.id_fnet, d.categoria, d.tipo, d.data_referencia, d.data_entrega,
            d.formato, d.url_storage, t.texto
     FROM fundos f
     JOIN documentos d ON d.cnpj = f.cnpj
     JOIN documento_textos t ON t.id_fnet = d.id_fnet
     WHERE ${cond2.join(" AND ")}
     ORDER BY d.data_entrega DESC NULLS LAST, d.id_fnet DESC`,
    p2.values,
  );

  const fundos = new Map(fundoRows.map((r) => [String(r.cnpj), montaFundo(r)]));

  for (const row of docRows) {
    const texto = String(row.texto ?? "");
    const textoNorm = fold(texto);
    const termosCasados = filtros.termos.filter((termo) => {
      const rx = termoRegex(termo, !filtros.literal);
      rx.lastIndex = 0;
      return rx.test(textoNorm);
    });
    if (termosCasados.length === 0) continue;
    if (filtros.exigirTodosTermos && termosCasados.length !== filtros.termos.length) continue;

    const trechos = encontrarTrechos(texto, termosCasados, {
      maxTrechos: filtros.maxTrechos,
      flexivel: !filtros.literal,
      textoNorm,
    });
    if (filtros.excluirVedacoes && trechos.length > 0 && trechos.every((t) => t.possivelVedacao)) {
      continue;
    }
    fundos.get(String(row.cnpj))?.documentos.push({
      idFnet: Number(row.id_fnet),
      categoria: (row.categoria as string) ?? null,
      tipo: (row.tipo as string) ?? null,
      dataReferencia: (row.data_referencia as string) ?? null,
      dataEntrega: toIso(row.data_entrega),
      formato: (row.formato as string) ?? null,
      urlStorage: (row.url_storage as string) ?? null,
      trechos,
    });
  }

  // fundos cujos documentos foram todos descartados (ex.: só vedações) saem
  const lista = [...fundos.values()].filter((f) => f.documentos.length > 0);
  return {
    fundos: lista,
    totalAproximado: Number(totalRows[0]?.total ?? 0),
    exibidos: lista.length,
  };
}
