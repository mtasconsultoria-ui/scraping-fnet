/**
 * Testes da camada de busca contra PostgreSQL de verdade.
 *
 * Exercitam o que os testes unitários não alcançam: a regex POSIX do operador
 * `~`, a subquery de documento vigente e a montagem dos parâmetros. Rodam só
 * com TEST_DATABASE_URL definido (no CI, um service container de Postgres).
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const url = process.env.TEST_DATABASE_URL;
const suite = url ? describe : describe.skip;

if (url) process.env.DATABASE_URL = url;
process.env.PGSSL = process.env.PGSSL ?? "disable";

const CNPJ_PERMITE = "36110774000165";
const CNPJ_VEDA = "11111111000191";
const CNPJ_SEM = "28737771000185";

const TEXTO_PERMITE =
  "REGULAMENTO DO FUNDO. Art. 5o - A carteira podera ser composta por CPR-F " +
  "(Cedula do Produto Rural Financeira), CRA e duplicatas do agronegocio.";
const TEXTO_VEDA =
  "REGULAMENTO DO FUNDO. Art. 5o - O Fundo adquirira CPR fisica, vedada a " +
  "aquisicao de cedulas de produto rural financeiras.";
const TEXTO_SEM =
  "REGULAMENTO DO FUNDO. Art. 5o - O Fundo investira em imoveis urbanos e CRI.";

suite("busca contra PostgreSQL", () => {
  let buscar: typeof import("../lib/busca").buscar;
  let FILTROS_PADRAO: typeof import("../lib/busca").FILTROS_PADRAO;
  let query: typeof import("../lib/db").query;
  let getPool: typeof import("../lib/db").getPool;

  beforeAll(async () => {
    ({ buscar, FILTROS_PADRAO } = await import("../lib/busca"));
    ({ query, getPool } = await import("../lib/db"));

    await query(`
      CREATE TABLE IF NOT EXISTS fundos (
        cnpj varchar(14) PRIMARY KEY, denominacao text, tipo_veiculo varchar(20),
        publico_alvo varchar(60), situacao varchar(60), administrador text,
        gestor text, segmento varchar(80), classe_cvm text, dt_registro date,
        atualizado_em timestamp DEFAULT now())`);
    await query(`
      CREATE TABLE IF NOT EXISTS fundos_metricas (
        cnpj varchar(14) PRIMARY KEY REFERENCES fundos(cnpj), competencia_ultima date,
        pl_atual numeric(20,2), pl_medio_12m numeric(20,2), cotistas_atual integer,
        atualizado_em timestamp DEFAULT now())`);
    await query(`
      CREATE TABLE IF NOT EXISTS documentos (
        id_fnet bigint PRIMARY KEY, cnpj varchar(14) REFERENCES fundos(cnpj),
        denominacao_fundo text, categoria varchar(120), tipo varchar(120),
        especie varchar(120), data_referencia varchar(20), competencia date,
        data_entrega timestamp, situacao varchar(20), versao integer,
        modalidade varchar(20), status_download varchar(20) NOT NULL DEFAULT 'pendente',
        url_storage text, formato varchar(10), baixado_em timestamp, raw_json text)`);
    await query(`
      CREATE TABLE IF NOT EXISTS documento_textos (
        id_fnet bigint PRIMARY KEY REFERENCES documentos(id_fnet), texto text,
        texto_norm text, num_paginas integer, num_caracteres integer,
        origem varchar(20), extraido_em timestamp)`);

    await query(`TRUNCATE documento_textos, documentos, fundos_metricas, fundos CASCADE`);

    const fundos: [string, string, string, string, number, number][] = [
      [CNPJ_PERMITE, "AGRO PLUS FIAGRO", "FIAGRO", "Investidores Qualificados", 412500000, 1843],
      [CNPJ_VEDA, "SAFRA CURTA FIDC", "FIDC", "Investidores Qualificados", 55000000, 12],
      [CNPJ_SEM, "RENDA URBANA FII", "FII", "Investidores em Geral", 1250000000, 45201],
    ];
    for (const [cnpj, nome, tipo, publico, pl, cotistas] of fundos) {
      await query(
        `INSERT INTO fundos (cnpj, denominacao, tipo_veiculo, publico_alvo, situacao, administrador)
         VALUES ($1,$2,$3,$4,'EM FUNCIONAMENTO NORMAL','ADMIN S.A.')`,
        [cnpj, nome, tipo, publico],
      );
      await query(
        `INSERT INTO fundos_metricas (cnpj, competencia_ultima, pl_atual, pl_medio_12m, cotistas_atual)
         VALUES ($1,'2026-05-01',$2,$2,$3)`,
        [cnpj, pl, cotistas],
      );
    }

    // fold() do texto: minúsculo e sem acento (os textos já são ASCII aqui)
    const docs: [number, string, string, string][] = [
      [9101, CNPJ_PERMITE, TEXTO_PERMITE, "2026-06-11"],
      [9104, CNPJ_VEDA, TEXTO_VEDA, "2026-06-11"],
      [9103, CNPJ_SEM, TEXTO_SEM, "2026-06-11"],
      // versão antiga do mesmo fundo, para testar `apenasVigente`
      [4000, CNPJ_PERMITE, "REGULAMENTO ANTIGO. Vedada a aquisicao de CPR-F.", "2020-01-11"],
    ];
    for (const [id, cnpj, texto, entrega] of docs) {
      await query(
        `INSERT INTO documentos (id_fnet, cnpj, categoria, tipo, data_referencia, data_entrega,
                                 status_download, formato)
         VALUES ($1,$2,'Regulamento','Regulamento','10/06/2026',$3,'baixado','pdf')`,
        [id, cnpj, entrega],
      );
      await query(
        `INSERT INTO documento_textos (id_fnet, texto, texto_norm, num_caracteres, origem)
         VALUES ($1,$2,lower($2),length($2),'pdf')`,
        [id, texto],
      );
    }
  });

  afterAll(async () => {
    await getPool().end();
  });

  it("encontra apenas os fundos que mencionam o termo", async () => {
    // "CPR-F" literal só existe no regulamento do AGRO PLUS: o SAFRA CURTA
    // escreve "CPR fisica" e o sinônimo por extenso; o RENDA URBANA, nenhum.
    const r = await buscar({ ...FILTROS_PADRAO, termos: ["CPR-F"] });
    expect(r.fundos.map((f) => f.cnpj)).toEqual([CNPJ_PERMITE]);
  });

  it("o regex do Postgres descarta 'CPR' solto", async () => {
    // SAFRA CURTA tem "CPR fisica"; só entra pelo sinônimo, não por "CPR-F"
    const r = await buscar({ ...FILTROS_PADRAO, termos: ["CPR-F"] });
    const safra = r.fundos.find((f) => f.cnpj === CNPJ_VEDA);
    expect(safra).toBeUndefined();
  });

  it("casa plural e conector trocados", async () => {
    const r = await buscar({
      ...FILTROS_PADRAO,
      termos: ["Cédula do Produto Rural Financeira"],
    });
    expect(r.fundos.map((f) => f.cnpj).sort()).toEqual([CNPJ_VEDA, CNPJ_PERMITE].sort());
  });

  it("sinaliza vedação e permite descartá-la", async () => {
    const termos = ["CPR-F", "Cédula do Produto Rural Financeira"];
    const todos = await buscar({ ...FILTROS_PADRAO, termos });
    const veda = todos.fundos.find((f) => f.cnpj === CNPJ_VEDA);
    expect(veda?.documentos[0].trechos.every((t) => t.possivelVedacao)).toBe(true);

    const permissivos = await buscar({ ...FILTROS_PADRAO, termos, excluirVedacoes: true });
    expect(permissivos.fundos.map((f) => f.cnpj)).not.toContain(CNPJ_VEDA);
    expect(permissivos.fundos.map((f) => f.cnpj)).toContain(CNPJ_PERMITE);
    expect(permissivos.exibidos).toBe(permissivos.fundos.length);
  });

  it("considera só o regulamento vigente", async () => {
    const vigente = await buscar({ ...FILTROS_PADRAO, termos: ["CPR-F"] });
    const ids = vigente.fundos.flatMap((f) => f.documentos.map((d) => d.idFnet));
    expect(ids).toContain(9101);
    expect(ids).not.toContain(4000);

    const todas = await buscar({ ...FILTROS_PADRAO, termos: ["CPR-F"], apenasVigente: false });
    expect(todas.fundos.flatMap((f) => f.documentos.map((d) => d.idFnet))).toContain(4000);
  });

  it("combina filtros estruturados com a busca textual", async () => {
    const termos = ["CPR-F"];
    expect(
      (await buscar({ ...FILTROS_PADRAO, termos, tiposVeiculo: ["FIAGRO"] })).fundos,
    ).toHaveLength(1);
    expect(
      (await buscar({ ...FILTROS_PADRAO, termos, tiposVeiculo: ["FII"] })).fundos,
    ).toHaveLength(0);
    expect(
      (await buscar({ ...FILTROS_PADRAO, termos, plMin: 500_000_000 })).fundos,
    ).toHaveLength(0);
    expect(
      (await buscar({ ...FILTROS_PADRAO, termos, publicoAlvo: ["qualificado"] })).fundos,
    ).toHaveLength(1);
  });

  it("sem termos, lista por filtros estruturados", async () => {
    const r = await buscar({ ...FILTROS_PADRAO, cotistasMin: 1000 });
    expect(r.fundos.map((f) => f.cnpj).sort()).toEqual([CNPJ_SEM, CNPJ_PERMITE].sort());
    expect(r.totalAproximado).toBe(2);
  });

  it("pagina os resultados", async () => {
    const pagina1 = await buscar({ ...FILTROS_PADRAO, limite: 1, offset: 0 });
    const pagina2 = await buscar({ ...FILTROS_PADRAO, limite: 1, offset: 1 });
    expect(pagina1.fundos).toHaveLength(1);
    expect(pagina2.fundos).toHaveLength(1);
    expect(pagina1.fundos[0].cnpj).not.toBe(pagina2.fundos[0].cnpj);
    expect(pagina1.totalAproximado).toBe(3);
  });

  it("serializa datas em ISO e números como number", async () => {
    const r = await buscar({ ...FILTROS_PADRAO, termos: ["CPR-F"] });
    const fundo = r.fundos.find((f) => f.cnpj === CNPJ_PERMITE)!;
    expect(fundo.competenciaUltima).toBe("2026-05-01");
    expect(typeof fundo.plMedio12m).toBe("number");
    expect(fundo.plMedio12m).toBe(412500000);
    expect(fundo.documentos[0].dataEntrega).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });
});
