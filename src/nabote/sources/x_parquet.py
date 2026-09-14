"""Fonte: base histórica do X em parquet.

Formato da base "2023 Brazilian Early Political Events" (Zenodo, CC-BY), que
é o mesmo que qualquer exportação denormalizada da API v2 tende a ter.

Três armadilhas deste formato, todas descobertas inspecionando o dado real e
todas capazes de custar horas se descobertas depois:

1. OS CAMPOS ANINHADOS SÃO repr DE PYTHON, NÃO JSON. Vêm com aspas simples:
   `{'user': 'fulano', 'user_id': 123}`. `json.loads` falha. É `ast.literal_eval`.

2. A DATA NÃO TEM FUSO NEM 'T': "2023-01-25 02:53:06". Assumimos UTC, que é o
   que a API v2 devolve, e normalizamos — senão a janela semanal sai errada.

3. `user` É O HANDLE, NÃO O ID. O identificador estável está em
   `user_info['id']`. Handle muda, id não: um vira `handle`, o outro vira
   `platform_user_id`. Trocar os dois faria o mesmo ator virar dois quando
   alguém mudasse de nome.

Ganho em relação ao Bluesky: os alvos trazem handle E id
(`{'user': ..., 'user_id': ...}`), então ator Tier C nasce com nome legível.
"""

from __future__ import annotations

import ast
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..events import NormalizedEvent, Target

PLATFORM = "x"

# "2023-01-24-CPMF.parquet" → data + termo de busca. O termo É o rótulo de
# tópico: a base foi coletada por termo em Trending Topics, então cada arquivo
# já vem com uma etiqueta que o clustering do passo 3 pode usar como gabarito.
_NOME = re.compile(r"^(?P<data>\d{4}-\d{2}-\d{2})-(?P<termo>.+)\.parquet$")

# (coluna de flag, coluna de alvo, tipo de aresta) — a ordem define a
# precedência de `post_type`, porque um post pode ser mais de um ao mesmo tempo.
_REFERENCIAS = (
    ("is_retweet", "retweeted_from", "repost"),
    ("is_reply", "reply_to", "reply"),
    ("is_quote", "quoted_from", "quote"),
)


def parse_member_name(nome: str) -> tuple[str | None, str | None]:
    """Devolve (data, termo de busca) a partir do nome do arquivo."""
    m = _NOME.match(Path(nome).name)
    return (m.group("data"), m.group("termo")) if m else (None, None)


def _literal(valor: Any) -> Any:
    """Converte repr de Python em objeto. Devolve None se não der.

    Nunca levanta: uma linha torta no meio de 13,9 milhões não pode derrubar
    a ingestão inteira.
    """
    if valor is None or isinstance(valor, (dict, list)):
        return valor
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        return ast.literal_eval(valor)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def _iso(valor: Any) -> str | None:
    """'2023-01-25 02:53:06' → '2023-01-25T02:53:06Z'."""
    if not valor:
        return None
    texto = str(valor).strip().replace("Z", "+00:00")
    for tentativa in (texto, texto.replace(" ", "T"), texto[:19].replace(" ", "T")):
        try:
            momento = datetime.fromisoformat(tentativa)
        except ValueError:
            continue
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=timezone.utc)
        return momento.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _alvo(bruto: Any, kind: str) -> Target | None:
    """`{'user': 'fulano', 'user_id': 123, ...}` → Target."""
    dado = _literal(bruto)
    if not isinstance(dado, dict):
        return None
    uid = dado.get("user_id")
    handle = dado.get("user")
    if uid is None and not handle:
        return None
    # sem id estável, o handle serve de identificador — pior, mas melhor que
    # perder a aresta
    return Target(kind=kind, uid=str(uid) if uid is not None else str(handle),
                  handle=str(handle) if handle else None)


def row_to_event(linha: dict[str, Any]) -> NormalizedEvent | None:
    """Uma linha do parquet → evento normalizado, ou None se inaproveitável."""
    post_uid = linha.get("tweet_id")
    quando = _iso(linha.get("created_at"))
    if post_uid is None or not quando:
        return None

    info = _literal(linha.get("user_info")) or {}
    handle = linha.get("user")
    uid = info.get("id")
    if uid is None and not handle:
        return None

    alvos: list[Target] = []
    post_type = "original"
    for flag, coluna, kind in _REFERENCIAS:
        if not linha.get(flag):
            continue
        alvo = _alvo(linha.get(coluna), kind)
        if alvo:
            if post_type == "original":
                post_type = kind
            alvos.append(alvo)

    vistos = {(t.kind, t.uid) for t in alvos}
    for menc in (_literal(linha.get("mentions")) or []):
        if not isinstance(menc, dict):
            continue
        m_uid = menc.get("id") or menc.get("username")
        if m_uid is None:
            continue
        chave = ("mention", str(m_uid))
        if chave in vistos:
            continue
        vistos.add(chave)
        alvos.append(Target("mention", str(m_uid),
                            handle=str(menc["username"]) if menc.get("username") else None))

    return NormalizedEvent(
        platform=PLATFORM, kind="post",
        actor_uid=str(uid) if uid is not None else str(handle),
        actor_handle=str(handle) if handle else None,
        occurred_at=quando, cursor=str(post_uid), post_uid=str(post_uid),
        post_type=post_type, text=linha.get("tweet_content"),
        lang="pt",  # a base inteira é filtrada por português na origem
        targets=alvos, raw=linha,
    )


class XParquetSource:
    """Lê um arquivo do zip. Um arquivo = uma campanha, com o termo como rótulo.

    Não abre o zip inteiro: os arquivos vão de 0,4 a 23 MB e são lidos um por
    vez, em memória. 2,97 GB descomprimidos não precisam caber em lugar nenhum.
    """

    def __init__(self, zip_path: Path | str, member: str) -> None:
        self.zip_path = Path(zip_path)
        self.member = member
        self.data, self.termo = parse_member_name(member)
        self.name = f"x_parquet:{Path(member).name}"
        self.skipped = 0

    def events(self, cursor: str | None = None) -> Iterator[NormalizedEvent]:
        import io

        import pyarrow.parquet as pq

        self.skipped = 0
        with zipfile.ZipFile(self.zip_path) as zf:
            with zf.open(self.member) as handle:
                tabela = pq.read_table(io.BytesIO(handle.read()))

        for lote in tabela.to_batches(max_chunksize=2000):
            for linha in lote.to_pylist():
                ev = row_to_event(linha)
                if ev is None:
                    self.skipped += 1
                    continue
                yield ev


def members_of(zip_path: Path | str) -> list[str]:
    """Arquivos parquet do zip, em ordem cronológica pelo nome."""
    with zipfile.ZipFile(zip_path) as zf:
        return sorted(i.filename for i in zf.infolist()
                      if i.filename.endswith(".parquet") and not i.is_dir())
