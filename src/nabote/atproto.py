"""Normalização de registros do AT Protocol para o modelo do banco.

Este módulo é puro: recebe o dicionário de um evento e devolve estrutura
normalizada. Não toca no banco nem na rede — é o que torna o parser testável
sem fixture de I/O.

Duas coisas do AT Protocol que moldam o resto do código:

1. REPOST NÃO É POST. É um tipo de registro próprio (`app.bsky.feed.repost`)
   cujo `subject.uri` aponta para o post amplificado. Guardamos como linha em
   `post` com `post_type='repost'` e texto nulo, para que a aresta tenha um
   `post_id` de origem.

2. TODA AT URI CARREGA O AUTOR. `at://<did>/<collection>/<rkey>` — o DID é o
   dono do repositório, ou seja, o autor. É daqui que sai o mecanismo Tier C:
   de um repost extraímos o DID de quem foi amplificado sem nunca ter coletado
   aquela conta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

PLATFORM = "bluesky"

COLLECTION_POST = "app.bsky.feed.post"
COLLECTION_REPOST = "app.bsky.feed.repost"
WANTED_COLLECTIONS = (COLLECTION_POST, COLLECTION_REPOST)

_MENTION_FACET = "app.bsky.richtext.facet#mention"


@dataclass(frozen=True)
class AtUri:
    did: str
    collection: str
    rkey: str

    @property
    def rid(self) -> str:
        """Identificador estável do registro dentro da plataforma."""
        return f"{self.did}/{self.collection}/{self.rkey}"


def parse_at_uri(uri: str) -> AtUri | None:
    """`at://did:plc:xxx/app.bsky.feed.post/3abc` → AtUri, ou None se malformada.

    Retorna None em vez de levantar: um payload torto de um único evento não
    pode derrubar o ciclo inteiro de coleta.
    """
    if not isinstance(uri, str) or not uri.startswith("at://"):
        return None
    parts = uri[len("at://"):].split("/")
    if len(parts) != 3 or not all(parts):
        return None
    return AtUri(did=parts[0], collection=parts[1], rkey=parts[2])


@dataclass
class Target:
    """Uma aresta que sai deste post."""
    kind: str          # repost | reply | quote | mention
    did: str           # ator de destino
    uri: str | None    # AT URI do post alvo, quando existe


@dataclass
class NormalizedEvent:
    kind: str                       # commit | identity | account
    did: str                        # ator de origem
    time_us: int
    operation: str | None = None    # create | update | delete
    collection: str | None = None
    rkey: str | None = None
    post_type: str | None = None    # original | repost | reply | quote
    text: str | None = None
    lang: str | None = None
    created_at: str | None = None
    handle: str | None = None       # só em eventos de identidade
    targets: list[Target] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def rid(self) -> str | None:
        if self.collection and self.rkey:
            return f"{self.did}/{self.collection}/{self.rkey}"
        return None


def _quote_uri(embed: Any) -> str | None:
    """Extrai a URI citada, cobrindo as duas formas de embed de citação."""
    if not isinstance(embed, dict):
        return None
    etype = embed.get("$type")
    if etype == "app.bsky.embed.record":
        record = embed.get("record")
        if isinstance(record, dict):
            return record.get("uri")
    if etype == "app.bsky.embed.recordWithMedia":
        outer = embed.get("record")
        if isinstance(outer, dict):
            inner = outer.get("record")
            if isinstance(inner, dict):
                return inner.get("uri")
    return None


def _mention_dids(facets: Any) -> Iterable[str]:
    if not isinstance(facets, list):
        return
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        for feature in facet.get("features") or []:
            if isinstance(feature, dict) and feature.get("$type") == _MENTION_FACET:
                did = feature.get("did")
                if isinstance(did, str) and did:
                    yield did


def _first_lang(record: dict[str, Any]) -> str | None:
    langs = record.get("langs")
    if isinstance(langs, list) and langs and isinstance(langs[0], str):
        return langs[0]
    return None


def _targets_from_post(record: dict[str, Any], author_did: str) -> tuple[str, list[Target]]:
    """Devolve (post_type, arestas) de um registro app.bsky.feed.post.

    Um post pode ser resposta E citação ao mesmo tempo. `post_type` é um rótulo
    único — resposta tem precedência —, mas as arestas são todas emitidas.
    Autointeração é descartada aqui: o schema a rejeitaria de qualquer forma.
    """
    targets: list[Target] = []
    post_type = "original"

    reply = record.get("reply")
    if isinstance(reply, dict):
        parent = reply.get("parent")
        if isinstance(parent, dict):
            uri = parse_at_uri(parent.get("uri", ""))
            if uri:
                post_type = "reply"
                if uri.did != author_did:
                    targets.append(Target("reply", uri.did, parent.get("uri")))
            else:
                post_type = "reply"

    quoted = _quote_uri(record.get("embed"))
    if quoted:
        uri = parse_at_uri(quoted)
        if uri:
            if post_type == "original":
                post_type = "quote"
            if uri.did != author_did:
                targets.append(Target("quote", uri.did, quoted))

    seen = {(t.kind, t.did) for t in targets}
    for did in _mention_dids(record.get("facets")):
        if did != author_did and ("mention", did) not in seen:
            seen.add(("mention", did))
            targets.append(Target("mention", did, None))

    return post_type, targets


def normalize(event: dict[str, Any]) -> NormalizedEvent | None:
    """Evento cru do Jetstream → NormalizedEvent, ou None se irrelevante.

    Ignorar silenciosamente é deliberado: o firehose traz tipos que não nos
    interessam, e cada um deles não deve virar erro.
    """
    if not isinstance(event, dict):
        return None
    did = event.get("did")
    time_us = event.get("time_us")
    kind = event.get("kind")
    if not isinstance(did, str) or not isinstance(time_us, int) or not kind:
        return None

    if kind == "identity":
        identity = event.get("identity") or {}
        return NormalizedEvent(
            kind="identity", did=did, time_us=time_us,
            handle=identity.get("handle"), raw=event,
        )

    if kind != "commit":
        return None

    commit = event.get("commit")
    if not isinstance(commit, dict):
        return None
    collection = commit.get("collection")
    operation = commit.get("operation")
    rkey = commit.get("rkey")
    if collection not in WANTED_COLLECTIONS or not rkey or not operation:
        return None

    base = NormalizedEvent(
        kind="commit", did=did, time_us=time_us, operation=operation,
        collection=collection, rkey=rkey, raw=event,
    )

    # delete não traz `record` — só a coordenada do que sumiu
    if operation == "delete":
        return base

    record = commit.get("record")
    if not isinstance(record, dict):
        return None

    base.created_at = record.get("createdAt")

    if collection == COLLECTION_REPOST:
        subject = record.get("subject")
        uri_str = subject.get("uri") if isinstance(subject, dict) else None
        uri = parse_at_uri(uri_str or "")
        if not uri or uri.did == did:
            return None  # repost do próprio post não é aresta
        base.post_type = "repost"
        base.targets = [Target("repost", uri.did, uri_str)]
        return base

    base.post_type, base.targets = _targets_from_post(record, did)
    base.text = record.get("text")
    base.lang = _first_lang(record)
    return base
