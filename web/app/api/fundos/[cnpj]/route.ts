import { NextResponse } from "next/server";

import { query } from "@/lib/db";

export const dynamic = "force-dynamic";

/** GET /api/fundos/{cnpj} — cadastro, série de PL/cotistas e documentos. */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ cnpj: string }> },
) {
  const { cnpj: bruto } = await params;
  const cnpj = bruto.replace(/\D/g, "").padStart(14, "0");

  try {
    const fundos = await query<Record<string, unknown>>(
      `SELECT f.*, m.pl_atual, m.pl_medio_12m, m.cotistas_atual, m.competencia_ultima
       FROM fundos f LEFT JOIN fundos_metricas m ON m.cnpj = f.cnpj
       WHERE f.cnpj = $1`,
      [cnpj],
    );
    if (fundos.length === 0) {
      return NextResponse.json({ erro: "fundo não encontrado" }, { status: 404 });
    }

    const [serie, documentos] = await Promise.all([
      query(
        `SELECT competencia, pl, num_cotistas, valor_cota FROM informes_mensais
         WHERE cnpj = $1 ORDER BY competencia`,
        [cnpj],
      ),
      query(
        `SELECT d.id_fnet, d.categoria, d.tipo, d.data_referencia, d.data_entrega,
                d.situacao, d.versao, d.formato, d.url_storage, d.status_download,
                (t.id_fnet IS NOT NULL) AS tem_texto
         FROM documentos d LEFT JOIN documento_textos t ON t.id_fnet = d.id_fnet
         WHERE d.cnpj = $1 ORDER BY d.data_entrega DESC NULLS LAST, d.id_fnet DESC`,
        [cnpj],
      ),
    ]);

    return NextResponse.json({ fundo: fundos[0], serie, documentos });
  } catch (erro) {
    console.error("consulta de fundo falhou", erro);
    return NextResponse.json({ erro: (erro as Error).message }, { status: 500 });
  }
}
