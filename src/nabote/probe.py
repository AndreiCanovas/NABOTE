"""Inspeção de bases externas antes de escrever adaptador.

Escrever um parser contra um formato que você não viu é a forma mais confiável
de perder um dia. Este módulo existe para que a primeira pergunta sobre
qualquer base nova — quais colunas, quais tipos, o que vem preenchido — tenha
resposta em um comando.

Lê parquet de dentro de um .zip sem descompactar: os arquivos do dataset
brasileiro têm de 0,4 a 23 MB cada, e não faz sentido expandir 2,5 GB no disco
só para olhar o cabeçalho.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any


def list_zip_members(path: Path | str, suffix: str = ".parquet") -> list[tuple[str, int]]:
    """(nome, tamanho descomprimido) de cada membro, em ordem."""
    with zipfile.ZipFile(path) as zf:
        return sorted(
            ((i.filename, i.file_size) for i in zf.infolist()
             if i.filename.endswith(suffix) and not i.is_dir()),
            key=lambda pair: pair[0],
        )


def read_parquet_member(path: Path | str, member: str):
    """Carrega um membro do zip em memória e devolve a tabela pyarrow."""
    import pyarrow.parquet as pq

    with zipfile.ZipFile(path) as zf:
        with zf.open(member) as handle:
            return pq.read_table(io.BytesIO(handle.read()))


def read_parquet_file(path: Path | str):
    import pyarrow.parquet as pq

    return pq.read_table(path)


def _short(value: Any, width: int = 62) -> str:
    text = "∅" if value is None else str(value).replace("\n", "⏎")
    return text if len(text) <= width else text[: width - 1] + "…"


def describe(table, sample_rows: int = 3) -> str:
    """Relatório de esquema + amostra, pensado para ser colado numa conversa."""
    linhas: list[str] = []
    total = table.num_rows
    linhas.append(f"linhas: {total:,}".replace(",", ".") + f"   colunas: {table.num_columns}")
    linhas.append("")
    linhas.append(f"{'coluna':<28}{'tipo':<26}{'nulos':>10}  exemplo")
    linhas.append("-" * 104)

    for name in table.schema.names:
        coluna = table.column(name)
        nulos = coluna.null_count
        pct = f"{nulos * 100 // total}%" if total else "—"
        exemplo = None
        for i in range(min(total, 50)):
            valor = coluna[i].as_py()
            if valor is not None:
                exemplo = valor
                break
        tipo = str(table.schema.field(name).type)
        linhas.append(f"{name[:27]:<28}{tipo[:25]:<26}{pct:>10}  {_short(exemplo)}")

    if sample_rows and total:
        linhas.append("")
        linhas.append(f"primeiras {min(sample_rows, total)} linhas completas:")
        for i in range(min(sample_rows, total)):
            linhas.append("")
            linhas.append(f"  --- linha {i} ---")
            for name in table.schema.names:
                linhas.append(f"  {name[:26]:<27} {_short(table.column(name)[i].as_py(), 72)}")

    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Diagnóstico de aptidão para grafo
#
# O modelo de dados da API v2 do X não mudou entre a época da Academic Research
# API (quando esta base foi coletada) e o pay-per-use atual: mesmos campos,
# mesmas expansions. Então o que se descobre aqui vale para a coleta ao vivo
# também — não é conhecimento descartável sobre uma base velha.
#
# O que decide se dá para montar grafo é UMA coisa: para cada retuíte, dá para
# saber QUEM foi retuitado? `referenced_tweets` sozinho dá o id do post
# referenciado, não o autor dele. Os três caminhos, em ordem de confiabilidade:
#   1. coluna com o author_id do post referenciado (expansion
#      referenced_tweets.id.author_id) — direto;
#   2. entities.mentions — num retuíte, o autor original é a primeira menção;
#   3. o texto, que em retuíte começa com "RT @usuario:".
# ---------------------------------------------------------------------------

_PAPEIS: dict[str, tuple[str, ...]] = {
    "id do post":          ("id", "id_str", "tweet_id", "post_id", "status_id"),
    "id do autor":         ("author_id", "user_id", "userid", "author",
                            "user", "from_user_id"),
    "handle do autor":     ("username", "screen_name", "author_username",
                            "user_username", "handle", "user_screen_name",
                            "author_handle", "from_user"),
    "texto":               ("text", "full_text", "content", "tweet", "tweet_text",
                            "body"),
    "data":                ("created_at", "createdat", "date", "timestamp",
                            "tweet_created_at", "datetime"),
    "idioma":              ("lang", "language", "tweet_lang"),
    "posts referenciados": ("referenced_tweets", "referenced_tweet", "references",
                            "in_reply_to_status_id", "retweeted_status",
                            "quoted_status", "quoted_status_id",
                            "in_reply_to_tweet_id", "conversation_id"),
    "autor referenciado":  ("referenced_tweets_author_id", "in_reply_to_user_id",
                            "retweeted_user_id", "quoted_user_id",
                            "referenced_author_id", "original_author_id",
                            "in_reply_to_screen_name", "retweeted_author_id"),
    "menções":             ("entities", "mentions", "entities_mentions",
                            "user_mentions", "entities_mentions_username"),
    "métricas":            ("public_metrics", "retweet_count", "like_count",
                            "reply_count", "quote_count", "favorite_count"),
}


def _casa(nome: str, candidatos: tuple[str, ...]) -> bool:
    """Casamento EXATO, de propósito.

    Substring casa demais: "id" acha `referenced_tweets_author_id`, "tweet" acha
    qualquer coisa. Num diagnóstico, falso positivo é pior que ausência — leva a
    escrever o adaptador contra uma coluna que não existe. Se a base real usar um
    nome fora da lista, ele aparece no esquema logo acima e a gente acrescenta.
    """
    limpo = nome.lower().replace("-", "_").replace(".", "_").strip()
    return limpo in candidatos


def diagnose(table) -> str:
    """Diz, em linguagem de projeto, se esta base dá para montar o grafo."""
    colunas = list(table.schema.names)
    achados: dict[str, list[str]] = {}
    for papel, candidatos in _PAPEIS.items():
        achados[papel] = [c for c in colunas if _casa(c, candidatos)]

    linhas = ["", "diagnóstico — o que o pipeline precisa", "-" * 104]
    linhas.append("  (casamento exato de nome; se algo estiver no esquema acima com")
    linhas.append("   outro nome, o diagnóstico não vê — é só apontar)")
    linhas.append("")
    essenciais = ("id do post", "id do autor", "texto", "data")
    for papel in _PAPEIS:
        col = achados[papel]
        if col:
            marca = "✓"
        elif papel in essenciais:
            marca = "✗"
        else:
            marca = "·"
        linhas.append(f"  {marca} {papel:<24} {', '.join(col) if col else '— não encontrado'}")

    faltam = [p for p in essenciais if not achados[p]]
    linhas.append("")
    if faltam:
        linhas.append(f"  BLOQUEIA: falta {', '.join(faltam)}. Sem isso não há nem post nem ator.")
        return "\n".join(linhas)

    # a pergunta que decide tudo: dá para saber QUEM foi retuitado?
    if achados["autor referenciado"]:
        linhas.append("  GRAFO DIRETO: há coluna com o autor do post referenciado.")
        linhas.append("  As arestas saem sem passo intermediário.")
    elif achados["menções"]:
        linhas.append("  GRAFO POR MENÇÃO: não há autor do post referenciado, mas há menções.")
        linhas.append("  Num retuíte o autor original é a primeira menção — dá para reconstruir,")
        linhas.append("  com perda nos casos em que a menção vier vazia.")
    elif achados["posts referenciados"]:
        linhas.append("  GRAFO PARCIAL: há o id do post referenciado, mas não o autor dele.")
        linhas.append("  Recuperável de duas formas, nesta ordem:")
        linhas.append("    1. cruzar o id com os posts da própria base (só funciona se o post")
        linhas.append("       referenciado também tiver sido coletado — coleta por palavra-chave")
        linhas.append("       deixa muito de fora);")
        linhas.append("    2. extrair do texto, que em retuíte começa com 'RT @usuario:'.")
    else:
        linhas.append("  SEM ARESTAS: nada indica retuíte, resposta ou citação.")
        linhas.append("  Dá para analisar texto e tópicos, mas não existe grafo a montar.")

    if not achados["handle do autor"]:
        linhas.append("")
        linhas.append("  ATENÇÃO: só há id numérico de autor, sem handle. Os atores ficam")
        linhas.append("  ilegíveis no relatório e não dá para cruzar com a lista curada por nome.")
    return "\n".join(linhas)
