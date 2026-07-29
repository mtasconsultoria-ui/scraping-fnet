import { NextResponse } from "next/server";

import { buscar } from "@/lib/busca";
import { filtrosDaQuery } from "@/lib/filtros";

export const dynamic = "force-dynamic";

/**
 * GET /api/fundos
 *
 * Filtros (repetíveis ou separados por vírgula): termo, tipo, publicoAlvo,
 * categoria; escalares: situacao, plMin, plMax, cotistasMin, cotistasMax,
 * limite, offset; flags "1": todosTermos, excluirVedacoes, literal, todasVersoes.
 */
export async function GET(request: Request) {
  const params = new URL(request.url).searchParams;
  const filtros = filtrosDaQuery(params);
  try {
    const resultado = await buscar(filtros);
    return NextResponse.json({ ...resultado, filtros });
  } catch (erro) {
    console.error("busca falhou", erro);
    return NextResponse.json({ erro: (erro as Error).message }, { status: 500 });
  }
}
