"""Normalização e busca de trechos em texto livre (regulamentos).

A normalização preserva comprimento: cada caractere do original vira exatamente
um caractere no texto normalizado. Isso deixa os offsets alinhados entre `texto`
e `texto_norm`, permitindo achar a posição em minúsculas/sem acento e recortar o
trecho no texto ORIGINAL, com acentuação e caixa preservadas para exibição.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

CONTEXTO_PADRAO = 160  # caracteres de cada lado do trecho casado

# Conectores intercambiáveis na redação jurídica ("cédula DO produto" vs "DE produto")
CONECTORES = frozenset({"de", "do", "da", "dos", "das", "no", "na", "nos", "nas",
                        "em", "a", "o", "as", "os", "e", "ou"})

# Marcas de vedação: o texto MENCIONA o ativo, mas para proibi-lo. Sinalizamos o
# trecho para o analista julgar — buscar menção não é o mesmo que buscar permissão.
NEGACOES = (
    "vedad", "vedar", "veda-se", "proibid", "proibir", "nao podera", "nao poderao",
    "nao sera permitid", "nao serao permitid", "nao autorizad", "impedid",
    "abster-se", "excluid", "nao podem", "nao pode",
)
JANELA_NEGACAO = 120  # caracteres antes do match onde a vedação é procurada


def fold_char(ch: str) -> str:
    """Um caractere -> um caractere: minúsculo e sem acento."""
    low = ch.lower()
    base = low[0] if low else ch  # 'İ'.lower() tem 2 chars; fica só o primeiro
    decomposed = unicodedata.normalize("NFD", base)
    return decomposed[0] if decomposed else base


def fold(text: str) -> str:
    return "".join(fold_char(c) for c in text)


def normalize_nfc(text: str) -> str:
    """Texto extraído é gravado em NFC para o fold preservar comprimento."""
    return unicodedata.normalize("NFC", text)


def tokens(termo: str) -> list[str]:
    return re.findall(r"[0-9a-z]+", fold(termo))


def _token_regex(token: str, flexivel: bool) -> str:
    if not flexivel:
        return re.escape(token)
    if token in CONECTORES:
        # "cédula DO produto" também casa "cédula DE produto"
        return f"(?:{'|'.join(sorted(CONECTORES))})"
    # plural nos dois sentidos: 'cédulas' no texto casa 'cédula' no termo e vice-versa
    raiz = token[:-1] if token.endswith("s") and len(token) > 3 else token
    return re.escape(raiz) + "s?"


def termo_regex(termo: str, flexivel: bool = True) -> re.Pattern[str]:
    """Regex tolerante a separadores: casa 'CPR-F', 'CPR F', 'CPR-\\nF', 'CPRF'.

    Os tokens precisam aparecer na ordem, ancorados em fronteira alfanumérica —
    'CPR-F' não casa dentro de 'CPR-Fulano'.

    Com `flexivel` (padrão), absorve as variações típicas da redação jurídica:
    plural e troca de conectores. `flexivel=False` exige a forma literal.
    """
    partes = tokens(termo)
    if not partes:
        raise ValueError(f"termo de busca sem conteúdo pesquisável: {termo!r}")
    corpo = r"[^0-9a-z]*".join(_token_regex(p, flexivel) for p in partes)
    return re.compile(rf"(?<![0-9a-z]){corpo}(?![0-9a-z])")


def token_prefiltro(termo: str) -> str:
    """Token mais longo do termo, usado no LIKE que reduz o universo no SQL.

    Devolve a raiz sem o plural e ignora conectores: o LIKE precisa ser mais
    permissivo que o regex de confirmação, nunca mais restrito, senão descarta
    documentos que o regex aceitaria.
    """
    partes = tokens(termo)
    if not partes:
        raise ValueError(f"termo de busca sem conteúdo pesquisável: {termo!r}")
    candidatos = [p for p in partes if p not in CONECTORES] or partes
    maior = max(candidatos, key=len)
    return maior[:-1] if maior.endswith("s") and len(maior) > 3 else maior


@dataclass
class Trecho:
    """Um trecho de texto com o pedaço casado localizado dentro dele."""

    texto: str
    match: str
    inicio: int  # posição do match dentro de `texto`
    posicao_documento: int  # offset do match no documento inteiro
    # o termo aparece logo após uma vedação ("vedada a aquisição de CPR-F"):
    # menção não é permissão, e quem lê o trecho precisa saber disso
    possivel_vedacao: bool = False

    @property
    def fim(self) -> int:
        return self.inicio + len(self.match)

    def destacado(self, abre: str = "«", fecha: str = "»") -> str:
        return f"{self.texto[: self.inicio]}{abre}{self.match}{fecha}{self.texto[self.fim :]}"


def detecta_vedacao(texto_norm: str, inicio: int, janela: int = JANELA_NEGACAO) -> bool:
    """Procura marca de vedação logo antes do termo encontrado."""
    contexto = texto_norm[max(0, inicio - janela) : inicio]
    contexto = re.sub(r"\s+", " ", contexto)
    return any(marca in contexto for marca in NEGACOES)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def encontrar_trechos(
    texto: str,
    texto_norm: str,
    termos: list[str],
    *,
    max_trechos: int = 3,
    contexto: int = CONTEXTO_PADRAO,
    flexivel: bool = True,
) -> list[Trecho]:
    """Localiza os termos e devolve trechos recortados do texto original.

    `texto_norm` deve ter sido gerado por `fold(texto)` (mesmo comprimento).
    """
    achados: list[Trecho] = []
    vistos: set[int] = set()
    for termo in termos:
        for match in termo_regex(termo, flexivel=flexivel).finditer(texto_norm):
            inicio, fim = match.span()
            if inicio in vistos:
                continue
            vistos.add(inicio)
            antes = _collapse(texto[max(0, inicio - contexto) : inicio])
            casado = _collapse(texto[inicio:fim])
            depois = _collapse(texto[fim : fim + contexto])
            prefixo = f"{antes} " if antes else ""
            sufixo = f" {depois}" if depois else ""
            achados.append(
                Trecho(
                    texto=f"{prefixo}{casado}{sufixo}",
                    match=casado,
                    inicio=len(prefixo),
                    posicao_documento=inicio,
                    possivel_vedacao=detecta_vedacao(texto_norm, inicio),
                )
            )
            if len(achados) >= max_trechos:
                return achados
    return achados


def contem_termo(texto_norm: str, termos: list[str]) -> bool:
    return any(termo_regex(t).search(texto_norm) for t in termos)
