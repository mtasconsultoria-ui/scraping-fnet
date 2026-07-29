import { NextResponse } from "next/server";

import { buscar } from "@/lib/busca";
import { nomeArquivoCsv, resultadoParaCsv } from "@/lib/csv";
import { LIMITE_EXPORT, filtrosDaQuery } from "@/lib/filtros";

export const dynamic = "force-dynamic";

/**
 * GET /api/fundos
 *
 * Filtros (repetíveis ou separados por vírgula): termo, tipo, publicoAlvo,
 * categoria; escalares: situacao, plMin, plMax, cotistasMin, cotistasMax,
 * limite, offset; flags "1": todosTermos, excluirVedacoes, literal, todasVersoes.
 *
 * `formato=csv` devolve a mesma consulta como planilha, com um teto de linhas
 * próprio (a UI exporta o filtro inteiro, não apenas a página exibida).
 */
export async function GET(request: Request) {
  const params = new URL(request.url).searchParams;
  const csv = params.get("formato") === "csv";
  const filtros = filtrosDaQuery(params, { export: csv });
  try {
    const resultado = await buscar(filtros);
    if (!csv) {
      return NextResponse.json({ ...resultado, filtros });
    }
    return new NextResponse(resultadoParaCsv(resultado), {
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": `attachment; filename="${nomeArquivoCsv(filtros.termos)}"`,
        "X-Total-Aproximado": String(resultado.totalAproximado),
        "X-Limite-Export": String(LIMITE_EXPORT),
      },
    });
  } catch (erro) {
    console.error("busca falhou", erro);
    return NextResponse.json({ erro: (erro as Error).message }, { status: 500 });
  }
}
