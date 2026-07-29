import pytest

from ingestor.textos import (
    detecta_vedacao,
    encontrar_trechos,
    fold,
    normalize_nfc,
    termo_regex,
    token_prefiltro,
    tokens,
)


def test_fold_preserva_comprimento():
    """Offsets precisam bater entre texto e texto_norm."""
    for original in [
        "Cédula do Produto Rural",
        "AQUISIÇÃO DE ATIVOS",
        "ÁÉÍÓÚÂÊÔÃÕÇàéîõü",
        "Investimento — R$ 1.000,00 (mil reais)",
        "ĲSTRAÑO",
    ]:
        assert len(fold(original)) == len(original), original


def test_fold_remove_acento_e_caixa():
    assert fold("Cédula do Produto Rural") == "cedula do produto rural"
    assert fold("AQUISIÇÃO") == "aquisicao"
    assert fold("CPR-F") == "cpr-f"


def test_tokens_e_prefiltro():
    assert tokens("CPR-F") == ["cpr", "f"]
    assert tokens("Cédula do Produto Rural Financeira") == [
        "cedula", "do", "produto", "rural", "financeira",
    ]
    assert token_prefiltro("CPR-F") == "cpr"
    assert token_prefiltro("Cédula do Produto Rural") == "produto"
    with pytest.raises(ValueError):
        token_prefiltro("---")


def test_regex_tolera_separadores():
    rx = termo_regex("CPR-F")
    for variante in ["cpr-f", "cpr f", "cpr  f", "cpr-\nf", "cprf", "cpr.f"]:
        assert rx.search(variante), variante


def test_regex_respeita_fronteira():
    rx = termo_regex("CPR-F")
    assert not rx.search("cpr-fulano")
    assert not rx.search("xcpr-f")
    assert rx.search("adquirir cpr-f, conforme")
    assert rx.search("(cpr-f)")


def test_regex_nao_casa_cpr_sozinho():
    """O ponto do caso de uso: distinguir CPR-F de um CPR solto."""
    rx = termo_regex("CPR-F")
    assert not rx.search("o fundo adquire cpr e outros ativos")


def test_encontrar_trechos_recorta_texto_original():
    texto = normalize_nfc(
        "O Fundo poderá adquirir, nos termos da regulamentação aplicável, "
        "CPR-F (Cédula do Produto Rural Financeira) emitidas por produtores rurais."
    )
    trechos = encontrar_trechos(texto, fold(texto), ["CPR-F"])
    assert len(trechos) == 1
    trecho = trechos[0]
    assert trecho.match == "CPR-F"  # caixa original preservada
    assert "Cédula do Produto Rural" in trecho.texto  # acentos preservados
    assert trecho.texto[trecho.inicio : trecho.fim] == "CPR-F"
    assert "«CPR-F»" in trecho.destacado()


def test_encontrar_trechos_com_quebra_de_linha():
    texto = normalize_nfc("adquirir Cédula do\nProduto Rural Financeira conforme o art. 5º")
    trechos = encontrar_trechos(texto, fold(texto), ["Cédula do Produto Rural Financeira"])
    assert len(trechos) == 1
    assert trechos[0].match == "Cédula do Produto Rural Financeira"  # whitespace colapsado


def test_encontrar_trechos_multiplos_e_limite():
    texto = normalize_nfc(" ".join(["... CPR-F ..."] * 5))
    assert len(encontrar_trechos(texto, fold(texto), ["CPR-F"], max_trechos=3)) == 3
    assert len(encontrar_trechos(texto, fold(texto), ["CPR-F"], max_trechos=10)) == 5


def test_encontrar_trechos_sem_match():
    texto = normalize_nfc("regulamento sem menção ao ativo")
    assert encontrar_trechos(texto, fold(texto), ["CPR-F"]) == []


# --- flexibilidade da redação jurídica -------------------------------------


def test_regex_flexivel_absorve_plural():
    rx = termo_regex("Cédula do Produto Rural Financeira")
    assert rx.search(fold("cédulas do produto rural financeiras"))
    assert rx.search(fold("cédula do produto rural financeira"))
    # e no sentido inverso (termo no plural, texto no singular)
    assert termo_regex("cédulas de produto rural").search(fold("cédula de produto rural"))


def test_regex_flexivel_troca_conectores():
    rx = termo_regex("Cédula do Produto Rural")
    for variante in ["cedula do produto rural", "cedula de produto rural",
                     "cedulas de produto rural"]:
        assert rx.search(variante), variante


def test_regex_literal_nao_flexibiliza():
    rx = termo_regex("Cédula do Produto Rural", flexivel=False)
    assert rx.search(fold("cédula do produto rural"))
    assert not rx.search(fold("cédulas de produto rural"))


def test_prefiltro_e_mais_permissivo_que_o_regex():
    """O LIKE do SQL não pode descartar o que o regex aceitaria."""
    termo = "cédulas do produto rural financeiras"
    texto = fold("emitiu cédula do produto rural financeira em favor do fundo")
    assert token_prefiltro(termo) in texto  # prefiltro passa
    assert termo_regex(termo).search(texto)  # e o regex confirma
    # conectores nunca viram prefiltro
    assert token_prefiltro("de do da") in {"de", "do", "da"}
    assert token_prefiltro("Cédula do Produto") == "produto"
    # casos travados também em web/tests/textos.test.ts: as duas implementações
    # precisam escolher o mesmo prefiltro, inclusive no empate de comprimento
    assert token_prefiltro("Cédula do Produto Rural Financeira") == "financeira"
    assert token_prefiltro("cédulas do produto") == "cedula"


# --- vedação ----------------------------------------------------------------


def test_detecta_vedacao():
    texto = normalize_nfc("Art. 5o - É vedada a aquisição de CPR-F pelo Fundo.")
    norm = fold(texto)
    inicio = norm.index("cpr-f")
    assert detecta_vedacao(norm, inicio)


def test_nao_detecta_vedacao_em_permissao():
    texto = normalize_nfc("Art. 5o - O Fundo poderá adquirir CPR-F e CRA.")
    norm = fold(texto)
    assert not detecta_vedacao(norm, norm.index("cpr-f"))


def test_vedacao_distante_nao_contamina():
    """Vedação de outro artigo, longe do termo, não marca o trecho."""
    texto = normalize_nfc(
        "É vedada a aquisição de criptoativos. " + "O regulamento segue. " * 12
        + "O Fundo poderá adquirir CPR-F."
    )
    norm = fold(texto)
    assert not detecta_vedacao(norm, norm.index("cpr-f"))


def test_trecho_marca_vedacao():
    texto = normalize_nfc("Art. 5o - É vedada ao Fundo a aquisição de CPR-F ou similares.")
    trechos = encontrar_trechos(texto, fold(texto), ["CPR-F"])
    assert len(trechos) == 1
    assert trechos[0].possivel_vedacao is True
