/**
 * Port da normalização e busca de trechos de `ingestor/textos.py`.
 *
 * As duas implementações precisam concordar: o Python grava `texto_norm` no
 * banco e o TypeScript recorta os trechos para exibição. `tests/textos.test.ts`
 * cobre os mesmos casos da suíte Python.
 *
 * Para evitar qualquer divergência de offset entre as linguagens (Python conta
 * code points, JS conta code units UTF-16), o `texto_norm` do banco é usado
 * apenas no filtro SQL; aqui o fold é recalculado a partir do texto original.
 */

export const CONTEXTO_PADRAO = 160;

export const CONECTORES = new Set([
  "de", "do", "da", "dos", "das", "no", "na", "nos", "nas",
  "em", "a", "o", "as", "os", "e", "ou",
]);

export const NEGACOES = [
  "vedad", "vedar", "veda-se", "proibid", "proibir", "nao podera", "nao poderao",
  "nao sera permitid", "nao serao permitid", "nao autorizad", "impedid",
  "abster-se", "excluid", "nao podem", "nao pode",
];

export const JANELA_NEGACAO = 120;

/** Um caractere -> um caractere: minúsculo e sem acento (preserva comprimento). */
export function foldChar(ch: string): string {
  const low = ch.toLowerCase();
  const base = low.length > 0 ? low[0] : ch;
  const decomposed = base.normalize("NFD");
  return decomposed.length > 0 ? decomposed[0] : base;
}

export function fold(text: string): string {
  let out = "";
  for (let i = 0; i < text.length; i++) out += foldChar(text[i]);
  return out;
}

export function tokens(termo: string): string[] {
  return fold(termo).match(/[0-9a-z]+/g) ?? [];
}

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function tokenPattern(token: string, flexivel: boolean): string {
  if (!flexivel) return escapeRegex(token);
  if (CONECTORES.has(token)) return `(?:${[...CONECTORES].sort().join("|")})`;
  const raiz = token.endsWith("s") && token.length > 3 ? token.slice(0, -1) : token;
  return `${escapeRegex(raiz)}s?`;
}

/** Regex tolerante a separadores, plural e conectores, ancorada em fronteira. */
export function termoRegex(termo: string, flexivel = true): RegExp {
  const partes = tokens(termo);
  if (partes.length === 0) throw new Error(`termo sem conteúdo pesquisável: ${termo}`);
  const corpo = partes.map((p) => tokenPattern(p, flexivel)).join("[^0-9a-z]*");
  return new RegExp(`(?<![0-9a-z])${corpo}(?![0-9a-z])`, "g");
}

/**
 * Padrão para o filtro no PostgreSQL (regex POSIX, sem lookarounds).
 *
 * Usa `\y` (fronteira de palavra do Postgres) no lugar dos lookarounds. É uma
 * aproximação deliberadamente mais permissiva: o SQL nunca pode descartar o que
 * `termoRegex` aceitaria, e a confirmação final acontece sempre em JS.
 */
export function termoRegexSql(termo: string, flexivel = true): string {
  const partes = tokens(termo);
  if (partes.length === 0) throw new Error(`termo sem conteúdo pesquisável: ${termo}`);
  const corpo = partes.map((p) => tokenPattern(p, flexivel)).join("[^0-9a-z]*");
  return `\\y${corpo}\\y`;
}

/** Token mais longo, sem plural e ignorando conectores (prefiltro LIKE). */
export function tokenPrefiltro(termo: string): string {
  const partes = tokens(termo);
  if (partes.length === 0) throw new Error(`termo sem conteúdo pesquisável: ${termo}`);
  const candidatos = partes.filter((p) => !CONECTORES.has(p));
  const pool = candidatos.length > 0 ? candidatos : partes;
  const maior = pool.reduce((a, b) => (b.length > a.length ? b : a));
  return maior.endsWith("s") && maior.length > 3 ? maior.slice(0, -1) : maior;
}

export interface Trecho {
  texto: string;
  match: string;
  inicio: number;
  fim: number;
  posicaoDocumento: number;
  possivelVedacao: boolean;
}

function collapse(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

export function detectaVedacao(textoNorm: string, inicio: number, janela = JANELA_NEGACAO): boolean {
  const contexto = collapse(textoNorm.slice(Math.max(0, inicio - janela), inicio));
  return NEGACOES.some((marca) => contexto.includes(marca));
}

export function encontrarTrechos(
  texto: string,
  termos: string[],
  opcoes: { maxTrechos?: number; contexto?: number; flexivel?: boolean; textoNorm?: string } = {},
): Trecho[] {
  const { maxTrechos = 3, contexto = CONTEXTO_PADRAO, flexivel = true } = opcoes;
  const textoNorm = opcoes.textoNorm ?? fold(texto);
  const achados: Trecho[] = [];
  const vistos = new Set<number>();

  for (const termo of termos) {
    const rx = termoRegex(termo, flexivel);
    rx.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = rx.exec(textoNorm)) !== null) {
      const inicio = match.index;
      const fim = inicio + match[0].length;
      if (match[0].length === 0) {
        rx.lastIndex++;
        continue;
      }
      if (!vistos.has(inicio)) {
        vistos.add(inicio);
        const antes = collapse(texto.slice(Math.max(0, inicio - contexto), inicio));
        const casado = collapse(texto.slice(inicio, fim));
        const depois = collapse(texto.slice(fim, fim + contexto));
        const prefixo = antes ? `${antes} ` : "";
        const sufixo = depois ? ` ${depois}` : "";
        achados.push({
          texto: `${prefixo}${casado}${sufixo}`,
          match: casado,
          inicio: prefixo.length,
          fim: prefixo.length + casado.length,
          posicaoDocumento: inicio,
          possivelVedacao: detectaVedacao(textoNorm, inicio),
        });
        if (achados.length >= maxTrechos) return achados;
      }
    }
  }
  return achados;
}
