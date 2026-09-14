"""Fonte Bluesky Jetstream.

Jetstream é infraestrutura oficial do Bluesky: consome o firehose cru do AT
Protocol (CBOR/CAR, com assinaturas) e reemite como JSON filtrável por
WebSocket. Custo zero, sem chave, sem cota — é o que permite validar todo o
pipeline antes de gastar o primeiro centavo com dado de X.

Obrigações que vêm junto (Diretrizes de Desenvolvedor do Bluesky): honrar
exclusões, manter contato público monitorado e segurança razoável sobre o que
for armazenado. A primeira está implementada em `ingest.py`; as outras duas são
operacionais.
"""

from __future__ import annotations

import json
from typing import Any, Iterator
from urllib.parse import urlencode

from ..atproto import WANTED_COLLECTIONS

# Instâncias públicas operadas pelo Bluesky. Trocar de host é a mitigação
# imediata se uma delas ficar indisponível.
PUBLIC_INSTANCES = (
    "jetstream1.us-east.bsky.network",
    "jetstream2.us-east.bsky.network",
    "jetstream1.us-west.bsky.network",
    "jetstream2.us-west.bsky.network",
)
DEFAULT_HOST = PUBLIC_INSTANCES[1]

# Limites documentados pelo serviço.
MAX_WANTED_DIDS = 10_000
MAX_WANTED_COLLECTIONS = 100


class JetstreamSource:
    """Cliente WebSocket do Jetstream.

    `wanted_dids` é o que transforma o firehose global (centenas de eventos por
    segundo) numa coleta barata: filtrando pela lista de sementes, o servidor
    manda só o que interessa.
    """

    name = "bluesky_jetstream"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        wanted_dids: list[str] | None = None,
        wanted_collections: tuple[str, ...] = WANTED_COLLECTIONS,
        open_timeout: float = 20.0,
        recv_timeout: float | None = 60.0,
    ) -> None:
        if wanted_dids and len(wanted_dids) > MAX_WANTED_DIDS:
            raise ValueError(
                f"Jetstream aceita no máximo {MAX_WANTED_DIDS} DIDs por conexão; "
                f"recebeu {len(wanted_dids)}. Divida a lista em conexões."
            )
        if len(wanted_collections) > MAX_WANTED_COLLECTIONS:
            raise ValueError(
                f"Jetstream aceita no máximo {MAX_WANTED_COLLECTIONS} coleções."
            )
        self.host = host
        self.wanted_dids = wanted_dids or []
        self.wanted_collections = wanted_collections
        self.open_timeout = open_timeout
        self.recv_timeout = recv_timeout

    def url(self, cursor: str | None = None) -> str:
        params: list[tuple[str, str]] = [
            ("wantedCollections", c) for c in self.wanted_collections
        ]
        params += [("wantedDids", d) for d in self.wanted_dids]
        if cursor:
            params.append(("cursor", str(cursor)))
        return f"wss://{self.host}/subscribe?{urlencode(params)}"

    def events(self, cursor: str | None = None) -> Iterator[dict[str, Any]]:
        # importado aqui para que o resto do pacote (e os testes) não dependa
        # de websockets estar instalado
        from websockets.sync.client import connect

        with connect(self.url(cursor), open_timeout=self.open_timeout) as ws:
            while True:
                raw = ws.recv(timeout=self.recv_timeout)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    # mensagem torta não derruba a conexão inteira
                    continue
