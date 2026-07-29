"""Gerador de PDF mínimo válido, para testar a extração de texto sem binários no repo."""
from __future__ import annotations


def _escape(text: str) -> bytes:
    data = text.encode("latin-1", errors="replace")
    for char, repl in ((b"\\", b"\\\\"), (b"(", b"\\("), (b")", b"\\)")):
        data = data.replace(char, repl)
    return data


def make_pdf(linhas: list[str]) -> bytes:
    """PDF de uma página, fonte Helvetica/WinAnsi, uma linha de texto por item."""
    comandos = [b"BT", b"/F1 12 Tf", b"14 TL", b"50 750 Td"]
    for i, linha in enumerate(linhas):
        if i:
            comandos.append(b"T*")
        comandos.append(b"(" + _escape(linha) + b") Tj")
    comandos.append(b"ET")
    stream = b"\n".join(comandos)

    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    saida = bytearray(b"%PDF-1.4\n")
    offsets = []
    for numero, corpo in enumerate(objetos, start=1):
        offsets.append(len(saida))
        saida += f"{numero} 0 obj\n".encode() + corpo + b"\nendobj\n"

    xref_pos = len(saida)
    saida += f"xref\n0 {len(objetos) + 1}\n".encode()
    saida += b"0000000000 65535 f \n"
    for offset in offsets:
        saida += f"{offset:010d} 00000 n \n".encode()
    saida += (
        f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n".encode()
        + b"%%EOF\n"
    )
    return bytes(saida)
