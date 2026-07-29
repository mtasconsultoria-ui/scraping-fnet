import { NextResponse } from "next/server";

import { query } from "@/lib/db";

export const dynamic = "force-dynamic";

/**
 * GET /api/dominios — opções para popular os filtros da UI.
 *
 * Os valores vêm do que existe de fato no banco (tipos de veículo, públicos-alvo
 * e categorias de documento presentes), mais as tabelas de domínio do FNET.
 */
export async function GET() {
  try {
    const [tipos, publicos, categorias, dominiosFnet] = await Promise.all([
      query(`SELECT DISTINCT tipo_veiculo AS valor FROM fundos
             WHERE tipo_veiculo IS NOT NULL ORDER BY 1`),
      query(`SELECT DISTINCT publico_alvo AS valor FROM fundos
             WHERE publico_alvo IS NOT NULL ORDER BY 1`),
      query(`SELECT categoria AS valor, COUNT(*)::int AS total FROM documentos
             WHERE categoria IS NOT NULL GROUP BY categoria ORDER BY 2 DESC`),
      query(`SELECT grupo, id_fnet, rotulo FROM dominios ORDER BY grupo, rotulo`),
    ]);
    return NextResponse.json({ tipos, publicos, categorias, dominiosFnet });
  } catch (erro) {
    console.error("consulta de domínios falhou", erro);
    return NextResponse.json({ erro: (erro as Error).message }, { status: 500 });
  }
}
