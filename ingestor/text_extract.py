"""Extração de texto dos documentos baixados, para busca por conteúdo.

PDFs com camada de texto são lidos com pypdf (Python puro, sem dependência de
sistema). PDFs escaneados não têm texto extraível: são marcados com
origem='vazio' e formam a fila de OCR (opcional, extra `[ocr]`).
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import os
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Documento, DocumentoTexto
from .parsing import decode_csv_bytes
from .storage import Storage
from .textos import fold, normalize_nfc

log = logging.getLogger(__name__)

# Abaixo disso, um PDF é tratado como escaneado (sem camada de texto útil)
MIN_CARACTERES_PDF = 200


@dataclass
class Extracao:
    texto: str
    origem: str
    num_paginas: int | None = None


def extrair_pdf(data: bytes) -> Extracao:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    paginas = [(page.extract_text() or "") for page in reader.pages]
    texto = normalize_nfc("\n".join(paginas).strip())
    origem = "pdf" if len(texto) >= MIN_CARACTERES_PDF else "vazio"
    return Extracao(texto=texto, origem=origem, num_paginas=len(reader.pages))


def extrair_ocr(data: bytes) -> Extracao:
    """OCR de PDF escaneado. Requer `pip install -e .[ocr]` + tesseract/poppler."""
    from pdf2image import convert_from_bytes
    from pytesseract import image_to_string

    paginas = convert_from_bytes(data, dpi=int(os.environ.get("OCR_DPI", "200")))
    texto = "\n".join(image_to_string(img, lang="por") for img in paginas)
    return Extracao(texto=normalize_nfc(texto.strip()), origem="ocr", num_paginas=len(paginas))


def extrair_xml(data: bytes) -> Extracao:
    return Extracao(texto=normalize_nfc(decode_csv_bytes(data).strip()), origem="xml")


def extrair(data: bytes, formato: str | None) -> Extracao:
    if formato == "pdf":
        return extrair_pdf(data)
    if formato in {"xml", "html", "json"}:
        return extrair_xml(data)
    if formato == "bin" or formato is None:
        return Extracao(texto="", origem="vazio")
    # zip e afins: sem tratamento específico por ora
    return Extracao(texto="", origem="vazio")


def extrair_textos(
    session: Session,
    storage: Storage,
    *,
    categorias: list[str] | None = None,
    limite: int = 100,
    reprocessar: bool = False,
    ocr: bool = False,
) -> dict:
    """Extrai texto dos documentos já baixados que ainda não têm texto.

    `reprocessar=True` refaz documentos já extraídos (útil ao trocar o extrator).
    `ocr=True` processa a fila de escaneados (origem='vazio') em vez dos novos.
    """
    stmt = (
        select(Documento)
        .where(Documento.status_download == "baixado")
        .order_by(Documento.data_entrega.desc())
        .limit(limite)
    )
    if categorias:
        stmt = stmt.where(Documento.categoria.in_(categorias))

    if ocr:
        stmt = stmt.join(DocumentoTexto).where(DocumentoTexto.origem == "vazio")
    elif not reprocessar:
        ja_extraidos = select(DocumentoTexto.id_fnet)
        stmt = stmt.where(Documento.id_fnet.not_in(ja_extraidos))

    extraidos = vazios = erros = 0
    for doc in session.execute(stmt).scalars().all():
        try:
            data = storage.get(doc.url_storage)
            resultado = extrair_ocr(data) if ocr else extrair(data, doc.formato)
        except Exception as exc:
            log.error("extração do documento %s falhou: %s", doc.id_fnet, exc)
            erros += 1
            continue

        registro = session.get(DocumentoTexto, doc.id_fnet) or DocumentoTexto(id_fnet=doc.id_fnet)
        registro.texto = resultado.texto
        registro.texto_norm = fold(resultado.texto)
        registro.num_paginas = resultado.num_paginas
        registro.num_caracteres = len(resultado.texto)
        registro.origem = resultado.origem
        registro.extraido_em = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        session.add(registro)
        session.flush()

        if resultado.origem == "vazio":
            vazios += 1
        else:
            extraidos += 1

    log.info(
        "extração: %s com texto, %s sem texto (fila de OCR), %s erros",
        extraidos,
        vazios,
        erros,
    )
    return {"extraidos": extraidos, "vazios": vazios, "erros": erros}
