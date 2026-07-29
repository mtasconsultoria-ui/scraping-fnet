/**
 * Espelha tests/test_textos.py: as duas implementações precisam concordar,
 * porque o Python indexa e o TypeScript exibe.
 */
import { describe, expect, it } from "vitest";

import {
  detectaVedacao,
  encontrarTrechos,
  fold,
  termoRegex,
  termoRegexSql,
  tokenPrefiltro,
  tokens,
} from "../lib/textos";

describe("fold", () => {
  it("preserva o comprimento", () => {
    for (const original of [
      "Cédula do Produto Rural",
      "AQUISIÇÃO DE ATIVOS",
      "ÁÉÍÓÚÂÊÔÃÕÇàéîõü",
      "Investimento — R$ 1.000,00 (mil reais)",
    ]) {
      expect(fold(original).length).toBe(original.length);
    }
  });

  it("remove acento e caixa", () => {
    expect(fold("Cédula do Produto Rural")).toBe("cedula do produto rural");
    expect(fold("AQUISIÇÃO")).toBe("aquisicao");
    expect(fold("CPR-F")).toBe("cpr-f");
  });
});

describe("tokens e prefiltro", () => {
  it("tokeniza", () => {
    expect(tokens("CPR-F")).toEqual(["cpr", "f"]);
    expect(tokens("Cédula do Produto Rural Financeira")).toEqual([
      "cedula", "do", "produto", "rural", "financeira",
    ]);
  });

  it("escolhe o token mais longo sem plural e sem conector", () => {
    expect(tokenPrefiltro("CPR-F")).toBe("cpr");
    expect(tokenPrefiltro("Cédula do Produto Rural")).toBe("produto");
    expect(tokenPrefiltro("Cédula do Produto Rural Financeira")).toBe("financeira");
    // empate de comprimento fica com o primeiro (mesma regra do Python)
    expect(tokenPrefiltro("cédulas do produto")).toBe("cedula");
  });
});

describe("termoRegex", () => {
  it("tolera separadores", () => {
    for (const variante of ["cpr-f", "cpr f", "cpr  f", "cpr-\nf", "cprf", "cpr.f"]) {
      expect(termoRegex("CPR-F").test(variante)).toBe(true);
    }
  });

  it("respeita fronteira de palavra", () => {
    expect(termoRegex("CPR-F").test("cpr-fulano")).toBe(false);
    expect(termoRegex("CPR-F").test("xcpr-f")).toBe(false);
    expect(termoRegex("CPR-F").test("adquirir cpr-f, conforme")).toBe(true);
  });

  it("não casa CPR sozinho", () => {
    expect(termoRegex("CPR-F").test("o fundo adquire cpr e outros ativos")).toBe(false);
  });

  it("absorve plural e conectores", () => {
    const rx = () => termoRegex("Cédula do Produto Rural Financeira");
    expect(rx().test(fold("cédulas de produto rural financeiras"))).toBe(true);
    expect(rx().test(fold("cédula do produto rural financeira"))).toBe(true);
  });

  it("modo literal não flexibiliza", () => {
    const rx = () => termoRegex("Cédula do Produto Rural", false);
    expect(rx().test(fold("cédula do produto rural"))).toBe(true);
    expect(rx().test(fold("cédulas de produto rural"))).toBe(false);
  });
});

describe("termoRegexSql", () => {
  it("não usa lookarounds (POSIX não suporta)", () => {
    const padrao = termoRegexSql("CPR-F");
    expect(padrao).not.toContain("(?<");
    expect(padrao).not.toContain("(?!");
    expect(padrao.startsWith("\\y")).toBe(true);
    expect(padrao.endsWith("\\y")).toBe(true);
  });
});

describe("encontrarTrechos", () => {
  it("recorta o texto original preservando acentos e caixa", () => {
    const texto =
      "O Fundo poderá adquirir, nos termos da regulamentação aplicável, " +
      "CPR-F (Cédula do Produto Rural Financeira) emitidas por produtores rurais.";
    const trechos = encontrarTrechos(texto, ["CPR-F"]);
    expect(trechos).toHaveLength(1);
    expect(trechos[0].match).toBe("CPR-F");
    expect(trechos[0].texto.slice(trechos[0].inicio, trechos[0].fim)).toBe("CPR-F");
    expect(trechos[0].texto).toContain("Cédula do Produto Rural");
  });

  it("casa termo quebrado por fim de linha", () => {
    const texto = "adquirir Cédula do\nProduto Rural Financeira conforme o art. 5º";
    const trechos = encontrarTrechos(texto, ["Cédula do Produto Rural Financeira"]);
    expect(trechos).toHaveLength(1);
    expect(trechos[0].match).toBe("Cédula do Produto Rural Financeira");
  });

  it("respeita o limite de trechos", () => {
    const texto = Array(5).fill("... CPR-F ...").join(" ");
    expect(encontrarTrechos(texto, ["CPR-F"], { maxTrechos: 3 })).toHaveLength(3);
    expect(encontrarTrechos(texto, ["CPR-F"], { maxTrechos: 10 })).toHaveLength(5);
  });

  it("não encontra o que não existe", () => {
    expect(encontrarTrechos("regulamento sem menção ao ativo", ["CPR-F"])).toEqual([]);
  });
});

describe("vedação", () => {
  it("detecta vedação antes do termo", () => {
    const texto = "Art. 5o - É vedada a aquisição de CPR-F pelo Fundo.";
    const norm = fold(texto);
    expect(detectaVedacao(norm, norm.indexOf("cpr-f"))).toBe(true);
  });

  it("não marca permissão", () => {
    const texto = "Art. 5o - O Fundo poderá adquirir CPR-F e CRA.";
    const norm = fold(texto);
    expect(detectaVedacao(norm, norm.indexOf("cpr-f"))).toBe(false);
  });

  it("marca o trecho", () => {
    const trechos = encontrarTrechos("É vedada ao Fundo a aquisição de CPR-F.", ["CPR-F"]);
    expect(trechos[0].possivelVedacao).toBe(true);
  });
});
