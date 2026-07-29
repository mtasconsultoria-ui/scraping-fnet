import type { Resultado } from "./busca";

/**
 * CSV para abrir no Excel em português: separador `;` e BOM UTF-8 — sem o BOM,
 * o Excel lê os acentos como lixo.
 */
const SEP = ";";
const BOM = "﻿";

export const COLUNAS = [
  "cnpj",
  "denominacao",
  "tipo_veiculo",
  "publico_alvo",
  "situacao",
  "administrador",
  "gestor",
  "segmento",
  "pl_atual",
  "pl_medio_12m",
  "cotistas_atual",
  "competencia_ultima",
  "documento_id",
  "documento_categoria",
  "documento_data_referencia",
  "documento_url",
  "possivel_vedacao",
  "trechos",
] as const;

function escapa(valor: unknown): string {
  if (valor == null) return "";
  const texto = String(valor);
  return /[";\n\r]/.test(texto) ? `"${texto.replace(/"/g, '""')}"` : texto;
}

function urlFnet(idFnet: number): string {
  return `https://fnet.bmfbovespa.com.br/fnet/publico/downloadDocumento?id=${idFnet}`;
}

export function resultadoParaCsv(resultado: Resultado): string {
  const linhas: string[] = [COLUNAS.join(SEP)];

  for (const fundo of resultado.fundos) {
    const base = [
      fundo.cnpj,
      fundo.denominacao,
      fundo.tipoVeiculo,
      fundo.publicoAlvo,
      fundo.situacao,
      fundo.administrador,
      fundo.gestor,
      fundo.segmento,
      fundo.plAtual,
      fundo.plMedio12m,
      fundo.cotistasAtual,
      fundo.competenciaUltima,
    ];

    if (fundo.documentos.length === 0) {
      linhas.push([...base, "", "", "", "", "", ""].map(escapa).join(SEP));
      continue;
    }
    // uma linha por documento, com os trechos reunidos numa célula
    for (const doc of fundo.documentos) {
      const trechos = doc.trechos.map((t) => t.texto).join(" ||| ");
      const vedacao =
        doc.trechos.length > 0 && doc.trechos.every((t) => t.possivelVedacao) ? "sim" : "nao";
      linhas.push(
        [
          ...base,
          doc.idFnet,
          doc.categoria,
          doc.dataReferencia,
          urlFnet(doc.idFnet),
          vedacao,
          trechos,
        ]
          .map(escapa)
          .join(SEP),
      );
    }
  }

  return BOM + linhas.join("\r\n") + "\r\n";
}

export function nomeArquivoCsv(termos: string[]): string {
  const base = termos.length
    ? termos[0].replace(/[^\w\-]+/g, "-").replace(/^-|-$/g, "").toLowerCase()
    : "fundos";
  return `fundos-${base || "consulta"}.csv`;
}
