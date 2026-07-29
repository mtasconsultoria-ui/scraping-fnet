import { NextResponse } from "next/server";

import { query } from "@/lib/db";
import { encontrarTrechos } from "@/lib/textos";

export const dynamic = "force-dynamic";

/**
 * GET /api/documentos/{id}?termo=CPR-F
 *
 * Metadados do documento e, quando `termo` é informado, os trechos do texto em
 * que ele aparece (com sinalização de vedação). O arquivo em si fica no object
 * storage — `url_storage` é o ponteiro para ele.
 */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const idFnet = Number(id);
  if (!Number.isInteger(idFnet)) {
    return NextResponse.json({ erro: "id inválido" }, { status: 400 });
  }
  const busca = new URL(request.url).searchParams;
  const termos = busca.getAll("termo").flatMap((t) => t.split(",")).map((t) => t.trim()).filter(Boolean);
  const maxTrechos = Number(busca.get("maxTrechos") ?? 10);

  try {
    const rows = await query<Record<string, unknown>>(
      `SELECT d.*, t.texto, t.num_paginas, t.num_caracteres, t.origem, t.extraido_em
       FROM documentos d LEFT JOIN documento_textos t ON t.id_fnet = d.id_fnet
       WHERE d.id_fnet = $1`,
      [idFnet],
    );
    if (rows.length === 0) {
      return NextResponse.json({ erro: "documento não encontrado" }, { status: 404 });
    }

    const { texto, ...documento } = rows[0];
    const trechos = termos.length
      ? encontrarTrechos(String(texto ?? ""), termos, {
          maxTrechos: Number.isFinite(maxTrechos) ? maxTrechos : 10,
        })
      : [];
    return NextResponse.json({ documento, trechos });
  } catch (erro) {
    console.error("consulta de documento falhou", erro);
    return NextResponse.json({ erro: (erro as Error).message }, { status: 500 });
  }
}
