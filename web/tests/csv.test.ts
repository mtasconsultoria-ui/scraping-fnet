import { describe, expect, it } from "vitest";

import type { Resultado } from "../lib/busca";
import { COLUNAS, nomeArquivoCsv, resultadoParaCsv } from "../lib/csv";
import { LIMITE_EXPORT, filtrosDaQuery } from "../lib/filtros";

function resultado(overrides: Partial<Resultado["fundos"][0]> = {}): Resultado {
  return {
    totalAproximado: 1,
    exibidos: 1,
    fundos: [
      {
        cnpj: "36110774000165",
        denominacao: "AGRO PLUS FIAGRO",
        tipoVeiculo: "FIAGRO",
        publicoAlvo: "Investidores Qualificados",
        situacao: "EM FUNCIONAMENTO NORMAL",
        administrador: "ADMIN S.A.",
        gestor: "GESTORA LTDA",
        segmento: null,
        plAtual: 412500000,
        plMedio12m: 412500000,
        cotistasAtual: 1843,
        competenciaUltima: "2026-05-01",
        documentos: [
          {
            idFnet: 9101,
            categoria: "Regulamento",
            tipo: "Regulamento",
            dataReferencia: "10/06/2026",
            dataEntrega: "2026-06-11T10:00:00.000Z",
            formato: "pdf",
            urlStorage: "/storage/9101.pdf",
            trechos: [
              {
                texto: "podera adquirir CPR-F conforme",
                match: "CPR-F",
                inicio: 16,
                fim: 21,
                posicaoDocumento: 100,
                possivelVedacao: false,
              },
            ],
          },
        ],
        ...overrides,
      },
    ],
  };
}

describe("resultadoParaCsv", () => {
  it("gera cabeçalho e uma linha por documento", () => {
    const csv = resultadoParaCsv(resultado());
    const linhas = csv.replace(/^﻿/, "").trim().split("\r\n");
    expect(linhas[0]).toBe(COLUNAS.join(";"));
    expect(linhas).toHaveLength(2);
    expect(linhas[1]).toContain("36110774000165");
    expect(linhas[1]).toContain("AGRO PLUS FIAGRO");
    expect(linhas[1]).toContain("downloadDocumento?id=9101");
    expect(linhas[1]).toContain("nao"); // possivel_vedacao
  });

  it("começa com BOM para o Excel ler os acentos", () => {
    expect(resultadoParaCsv(resultado()).startsWith("﻿")).toBe(true);
  });

  it("escapa aspas, separador e quebras de linha", () => {
    const dados = resultado({ denominacao: 'FUNDO "ESPECIAL"; COM ; SEPARADOR' });
    const linha = resultadoParaCsv(dados).split("\r\n")[1];
    expect(linha).toContain('"FUNDO ""ESPECIAL""; COM ; SEPARADOR"');
    // o campo escapado não pode ser quebrado em colunas extras
    expect(linha.split(";").length).toBe(COLUNAS.length + 2); // 2 separadores dentro do campo
  });

  it("marca vedação quando todos os trechos são vedações", () => {
    const dados = resultado();
    dados.fundos[0].documentos[0].trechos[0].possivelVedacao = true;
    expect(resultadoParaCsv(dados).split("\r\n")[1]).toContain(";sim;");
  });

  it("exporta fundo sem documentos (busca só por filtros)", () => {
    const dados = resultado({ documentos: [] });
    const linhas = resultadoParaCsv(dados).trim().split("\r\n");
    expect(linhas).toHaveLength(2);
    expect(linhas[1].endsWith(";;;;;")).toBe(true); // colunas de documento vazias
  });

  it("junta múltiplos trechos numa célula", () => {
    const dados = resultado();
    dados.fundos[0].documentos[0].trechos.push({
      texto: "segundo trecho com CPR-F",
      match: "CPR-F",
      inicio: 19,
      fim: 24,
      posicaoDocumento: 900,
      possivelVedacao: false,
    });
    expect(resultadoParaCsv(dados)).toContain("|||");
  });
});

describe("nomeArquivoCsv", () => {
  it("usa o termo buscado", () => {
    expect(nomeArquivoCsv(["CPR-F"])).toBe("fundos-cpr-f.csv");
    expect(nomeArquivoCsv(["Cédula do Produto"])).toBe("fundos-c-dula-do-produto.csv");
    expect(nomeArquivoCsv([])).toBe("fundos-fundos.csv");
  });
});

describe("filtros de export", () => {
  it("ignora paginação e usa teto próprio", () => {
    const params = new URLSearchParams("termo=CPR-F&offset=50&limite=10");
    const tela = filtrosDaQuery(params);
    expect(tela.offset).toBe(50);
    expect(tela.limite).toBe(10);

    const exportacao = filtrosDaQuery(new URLSearchParams("termo=CPR-F"), { export: true });
    expect(exportacao.offset).toBe(0);
    expect(exportacao.limite).toBe(LIMITE_EXPORT);
  });

  it("respeita o teto máximo do export", () => {
    const filtros = filtrosDaQuery(new URLSearchParams("limite=999999"), { export: true });
    expect(filtros.limite).toBe(LIMITE_EXPORT);
  });
});
