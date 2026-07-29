import { NextResponse } from "next/server";

import { query } from "@/lib/db";

export const dynamic = "force-dynamic";

const LIMITES_DIAS = { cvm: 9, fnet: 3 } as const;
const MAX_MESES_ATRASO_INFORME = 4;

interface Verificacao {
  nome: string;
  status: "ok" | "aviso" | "erro";
  mensagem: string;
}

/**
 * GET /api/health — diagnóstico para monitoração externa.
 *
 * Espelha `ingestor/monitoramento.py`: além de erros explícitos, verifica o
 * *frescor* dos dados, que é como uma ingestão quebrada costuma se manifestar
 * (o job "passa", mas nada novo entra). Responde 503 quando há erro, para que
 * um monitor de uptime dispare sem precisar interpretar o corpo.
 */
export async function GET() {
  try {
    const [execucoes, competencias, contagens] = await Promise.all([
      query<{ fonte: string; status: string; terminado_em: Date | null; iniciado_em: Date; erro: string | null }>(
        `SELECT fonte, status, terminado_em, iniciado_em, erro FROM sync_runs
         WHERE id IN (SELECT MAX(id) FROM sync_runs GROUP BY fonte) ORDER BY fonte`,
      ),
      query<{ competencia: Date | null }>(`SELECT MAX(competencia) AS competencia FROM informes_mensais`),
      query<{ fundos: string; documentos: string; textos: string }>(
        `SELECT (SELECT COUNT(*) FROM fundos) AS fundos,
                (SELECT COUNT(*) FROM documentos) AS documentos,
                (SELECT COUNT(*) FROM documento_textos) AS textos`,
      ),
    ]);

    const agora = Date.now();
    const verificacoes: Verificacao[] = [];

    if (execucoes.length === 0) {
      verificacoes.push({
        nome: "execucoes",
        status: "erro",
        mensagem: "nenhuma execução de ingestão registrada",
      });
    }
    for (const run of execucoes) {
      const grupo = run.fonte.startsWith("fnet") ? "fnet" : "cvm";
      if (run.status === "erro") {
        verificacoes.push({
          nome: `execucao:${run.fonte}`,
          status: "erro",
          mensagem: `última execução falhou: ${run.erro ?? "sem detalhe"}`,
        });
        continue;
      }
      const referencia = run.terminado_em ?? run.iniciado_em;
      const dias = Math.floor((agora - new Date(referencia).getTime()) / 86_400_000);
      const limite = LIMITES_DIAS[grupo];
      verificacoes.push({
        nome: `frescor:${run.fonte}`,
        status: dias > limite ? "erro" : "ok",
        mensagem:
          dias > limite
            ? `sem execução bem-sucedida há ${dias} dias (limite ${limite})`
            : `última execução há ${dias} dia(s)`,
      });
    }

    const competencia = competencias[0]?.competencia;
    if (!competencia) {
      verificacoes.push({ nome: "informes", status: "erro", mensagem: "nenhum informe carregado" });
    } else {
      const data = new Date(competencia);
      const hoje = new Date();
      const meses =
        (hoje.getUTCFullYear() - data.getUTCFullYear()) * 12 +
        (hoje.getUTCMonth() - data.getUTCMonth());
      verificacoes.push({
        nome: "informes",
        status: meses > MAX_MESES_ATRASO_INFORME ? "erro" : "ok",
        mensagem: `competência mais recente: ${data.toISOString().slice(0, 7)}`,
      });
    }

    const status = verificacoes.some((v) => v.status === "erro")
      ? "erro"
      : verificacoes.some((v) => v.status === "aviso")
        ? "aviso"
        : "ok";

    return NextResponse.json(
      {
        status,
        verificacoes,
        contagens: {
          fundos: Number(contagens[0]?.fundos ?? 0),
          documentos: Number(contagens[0]?.documentos ?? 0),
          textos: Number(contagens[0]?.textos ?? 0),
        },
      },
      { status: status === "erro" ? 503 : 200 },
    );
  } catch (erro) {
    console.error("health falhou", erro);
    return NextResponse.json(
      { status: "erro", verificacoes: [{ nome: "banco", status: "erro", mensagem: (erro as Error).message }] },
      { status: 503 },
    );
  }
}
