"""Cliente da API JSON não documentada do FundosNET (B3/CVM).

A tela pública de busca é alimentada por um endpoint estilo DataTables:

    GET /fnet/publico/pesquisarGerenciadorDocumentosDados
        ?s=<offset>&l=<page size>&o[0][dataEntrega]=asc
        &tipoFundo=<id>&idCategoriaDocumento=<id>&cnpjFundo=<digitos>
        &dataInicial=dd/MM/yyyy&dataFinal=dd/MM/yyyy

O download é GET /fnet/publico/downloadDocumento?id=<id>; o corpo costuma vir
codificado em base64 (detectado e decodificado aqui pelos magic bytes).

Por ser API não documentada, tudo é defensivo: headers de navegador, rate limit,
retry com backoff e validação de shape com erros descritivos.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import time
from html.parser import HTMLParser

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://fnet.bmfbovespa.com.br/fnet/publico"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

_MAGIC_EXT = (
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"<?xml", "xml"),
    (b"{", "json"),
)


class FnetShapeError(RuntimeError):
    """A resposta do FNET não tem o formato esperado — layout mudou."""


def detect_ext(data: bytes) -> str:
    stripped = data.lstrip()
    for magic, ext in _MAGIC_EXT:
        if stripped.startswith(magic):
            return ext
    if stripped[:1] == b"<":
        return "html"
    return "bin"


def maybe_base64(data: bytes) -> bytes:
    """Decodifica o corpo se ele for base64 de um formato conhecido."""
    if detect_ext(data) != "bin":
        return data
    compact = b"".join(data.split())
    if not re.fullmatch(rb"[A-Za-z0-9+/]+={0,2}", compact or b"x"):
        return data
    try:
        decoded = base64.b64decode(compact, validate=True)
    except Exception:
        return data
    return decoded if detect_ext(decoded) != "bin" else data


class FnetClient:
    def __init__(
        self,
        base_url: str | None = None,
        min_interval: float | None = None,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("FNET_BASE", DEFAULT_BASE)).rstrip("/")
        if min_interval is None:
            min_interval = float(os.environ.get("FNET_MIN_INTERVAL", "1.0"))
        self.min_interval = min_interval
        self.retries = retries
        self._last_request = 0.0
        self._client = httpx.Client(
            headers=BROWSER_HEADERS,
            timeout=60.0,
            follow_redirects=True,
            transport=transport,
        )

    # ------------------------------------------------------------------
    # HTTP básico
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        url = f"{self.base_url}/{path}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                delay = 2**attempt
                log.warning("fnet retry %s/%s em %ss: %s", attempt, self.retries, delay, path)
                time.sleep(delay)
            self._throttle()
            try:
                response = self._client.get(url, params=params, headers=headers)
                if response.status_code >= 500:
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response
                    )
                    continue
                response.raise_for_status()  # 4xx não é transitório: falha direto
                return response
            except httpx.TransportError as exc:
                last_error = exc
        raise RuntimeError(f"FNET falhou após {self.retries} tentativas: {path}") from last_error

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    # ------------------------------------------------------------------
    # Busca de documentos
    # ------------------------------------------------------------------

    def search_documents(
        self,
        *,
        start: int = 0,
        length: int = 100,
        tipo_fundo: int | None = None,
        cnpj: str | None = None,
        id_categoria: int | None = None,
        id_tipo: int | None = None,
        id_especie: int | None = None,
        data_inicial: str | None = None,
        data_final: str | None = None,
        ordem: str = "asc",
    ) -> dict:
        """Uma página da busca. Datas no formato dd/MM/yyyy (o que a UI envia).

        Semântica do `tipoFundo`, medida em produção (2026-07-29):
        - `0` filtra por um tipo inexistente: a busca devolve SEMPRE zero;
        - vazio NÃO é "todos": devolve apenas os documentos mais recentes
          (~centenas, os "do dia"), enquanto um tipo específico devolve o
          acervo inteiro (FIDC=2 sozinho: ~395 mil documentos).

        Para varrer o acervo, itere por tipo (ver `listar_tipos_fundo`) — é o
        que `fnet_sync.sync_documentos` e a prospecção fazem quando nenhum
        tipo é informado.
        """
        params: dict = {
            "d": 1,
            "s": start,
            "l": length,
            f"o[0][dataEntrega]": ordem,
            "tipoFundo": tipo_fundo if tipo_fundo else "",
            "idCategoriaDocumento": id_categoria or 0,
            "idTipoDocumento": id_tipo or 0,
            "idEspecieDocumento": id_especie or 0,
            "paginaCertificados": "false",
            "_": int(time.time() * 1000),
        }
        if cnpj:
            params["cnpjFundo"] = re.sub(r"\D", "", cnpj)
        if data_inicial:
            params["dataInicial"] = data_inicial
        if data_final:
            params["dataFinal"] = data_final

        response = self._get(
            "pesquisarGerenciadorDocumentosDados",
            params=params,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/abrirGerenciadorDocumentosCVM",
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FnetShapeError(
                f"resposta da busca não é JSON (primeiros bytes: {response.content[:120]!r})"
            ) from exc
        if not isinstance(payload, dict) or "data" not in payload:
            raise FnetShapeError(f"JSON da busca sem chave 'data': {list(payload)[:10]}")
        return payload

    def iter_documents(self, *, page_size: int = 100, max_pages: int | None = None, **filters):
        """Itera todos os documentos da busca, paginando por dataEntrega ascendente."""
        start = 0
        pages = 0
        while True:
            payload = self.search_documents(start=start, length=page_size, **filters)
            data = payload.get("data") or []
            yield from data
            pages += 1
            start += page_size
            total = payload.get("recordsFiltered") or payload.get("recordsTotal") or 0
            if len(data) < page_size or start >= total:
                return
            if max_pages is not None and pages >= max_pages:
                log.warning("iter_documents: max_pages=%s atingido; busca truncada", max_pages)
                return

    # ------------------------------------------------------------------
    # Download e domínios
    # ------------------------------------------------------------------

    def download_document(self, doc_id: int) -> bytes:
        response = self._get("downloadDocumento", params={"id": doc_id})
        content = maybe_base64(response.content)
        if not content:
            raise FnetShapeError(f"downloadDocumento?id={doc_id} retornou corpo vazio")
        return content

    def listar_tipos_fundo(self) -> list[tuple[int, str]]:
        """Ids reais dos tipos de fundo, lidos da própria página de filtros.

        Necessário porque a busca com tipoFundo vazio não percorre o acervo —
        só devolve os documentos mais recentes; a varredura completa é por tipo.
        """
        grupos = self.fetch_domains()
        tipos: list[tuple[int, str]] = []
        for valor, rotulo in grupos.get("tipoFundo", []):
            valor = valor.strip()
            if valor.isdigit() and int(valor) != 0:  # descarta o placeholder de "todos"
                tipos.append((int(valor), rotulo.strip()))
        if not tipos:
            raise FnetShapeError("nenhum tipo de fundo com id numérico nos filtros do FNET")
        return tipos

    def fetch_domains(self) -> dict[str, list[tuple[str, str]]]:
        """Raspa os <select> da página de filtros: grupo -> [(valor, rótulo)]."""
        response = self._get(
            "abrirGerenciadorDocumentosCVM", headers={"Accept": "text/html,*/*"}
        )
        parser = _SelectParser()
        parser.feed(response.text)
        if not parser.selects:
            raise FnetShapeError("nenhum <select> encontrado na página de filtros do FNET")
        return parser.selects

    def close(self) -> None:
        self._client.close()


class _SelectParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.selects: dict[str, list[tuple[str, str]]] = {}
        self._select: str | None = None
        self._option_value: str | None = None
        self._option_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = dict(attrs)
        if tag == "select":
            self._select = attrs.get("id") or attrs.get("name")
            if self._select:
                self.selects.setdefault(self._select, [])
        elif tag == "option" and self._select:
            self._option_value = attrs.get("value", "")
            self._option_text = []

    def handle_data(self, data: str) -> None:
        if self._option_value is not None:
            self._option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._select and self._option_value is not None:
            label = "".join(self._option_text).strip()
            if label:
                self.selects[self._select].append((self._option_value, label))
            self._option_value = None
        elif tag == "select":
            self._select = None
