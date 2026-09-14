"""Resolução de handle → DID e registro da lista de sementes.

Por que isto existe: o Jetstream filtra por DID, não por handle. Sem esta
camada, montar a lista de sementes viria a ser caçar `did:plc:...` um por um
na mão — a parte mais chata e mais fácil de errar da frente 02.

Handle muda, DID não. Guardamos os dois, e é o DID que manda.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable

from .atproto import PLATFORM
from .db import utcnow

RESOLVE_URL = (
    "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle={handle}"
)
SEARCH_URL = (
    "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActors?q={q}&limit={limit}"
)
# searchActors não devolve contagem de seguidores — e essa é justamente a
# informação que separa conta oficial de fã-clube. getProfiles devolve, em
# lotes de até 25.
PROFILES_URL = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles"
PROFILES_BATCH = 25
TIMEOUT = 15


class ResolveError(RuntimeError):
    pass


def normalize_handle(raw: str) -> str:
    """'@Fulano.BSky.Social' → 'fulano.bsky.social'. Handle sem ponto ganha o
    domínio padrão, que é o erro de digitação mais comum."""
    handle = raw.strip().lstrip("@").lower()
    if handle and "." not in handle:
        handle = f"{handle}.bsky.social"
    return handle


def resolve_handle(handle: str) -> str:
    """Devolve o DID. Levanta ResolveError com motivo legível."""
    url = RESOLVE_URL.format(handle=urllib.parse.quote(handle))
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            did = json.loads(response.read().decode("utf-8")).get("did")
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            raise ResolveError(f"handle inexistente ou digitado errado: {handle}") from exc
        raise ResolveError(f"HTTP {exc.code} ao resolver {handle}") from exc
    except urllib.error.URLError as exc:
        raise ResolveError(
            f"sem acesso à API do Bluesky ao resolver {handle} ({exc.reason}). "
            f"Rode numa máquina com saída para public.api.bsky.app."
        ) from exc
    if not did:
        raise ResolveError(f"resposta sem DID para {handle}")
    return did


def parse_seed_file(text: str) -> list[str]:
    """Uma entrada por linha; `#` comenta. Aceita handle OU DID."""
    entries = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def register_seeds(
    conn: sqlite3.Connection,
    entries: Iterable[str],
    tier: str = "A",
    resolver: Callable[[str], str] = resolve_handle,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Resolve e registra as sementes. Devolve (ok, falhas).

    `resolver` é injetável para que os testes rodem sem rede.

    Um DID já conhecido nunca é rebaixado, mesma regra da ingestão: promoção e
    rebaixamento são decisão de curadoria, e registrar semente é promoção.
    """
    from .ingest import upsert_actor

    ok: list[tuple[str, str]] = []
    falhas: list[tuple[str, str]] = []

    for raw in entries:
        if raw.startswith("did:"):
            did, handle = raw, None
        else:
            handle = normalize_handle(raw)
            try:
                did = resolver(handle)
            except ResolveError as exc:
                falhas.append((raw, str(exc)))
                continue

        actor_id = upsert_actor(conn, did, tier)
        if handle:
            conn.execute(
                "UPDATE actor SET handle = ?, last_seen_at = ? WHERE actor_id = ?",
                (handle, utcnow(), actor_id),
            )
        ok.append((handle or did, did))

    return ok, falhas


def seed_dids(conn: sqlite3.Connection, tiers: tuple[str, ...] = ("A", "B")) -> list[str]:
    """DIDs registrados como semente — o filtro que vai para o Jetstream."""
    placeholders = ",".join("?" * len(tiers))
    rows = conn.execute(
        f"SELECT platform_user_id FROM actor WHERE platform = ? AND tier IN ({placeholders}) "
        f"ORDER BY platform_user_id",
        (PLATFORM, *tiers),
    ).fetchall()
    return [r["platform_user_id"] for r in rows]


def search_actors(name: str, limit: int = 5) -> list[dict]:
    """Busca contas por nome. Devolve candidatos com handle, nome e seguidores.

    Existe porque uma lista de curadoria normalmente nasce como NOMES, não como
    handles — e, no caso do Bluesky, boa parte das pessoas simplesmente não tem
    conta. Buscar e deixar a escolha com a pessoa é a única forma correta:
    chutar o handle de uma figura pública e coletar a conta errada atribui
    discurso a quem não disse.
    """
    url = SEARCH_URL.format(q=urllib.parse.quote(name), limit=limit)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ResolveError(f"HTTP {exc.code} ao buscar '{name}'") from exc
    except urllib.error.URLError as exc:
        raise ResolveError(
            f"sem acesso à API do Bluesky ao buscar '{name}' ({exc.reason})."
        ) from exc

    out = []
    for actor in payload.get("actors", []):
        out.append({
            "handle": actor.get("handle", ""),
            "did": actor.get("did", ""),
            "display_name": actor.get("displayName") or "",
            "followers": actor.get("followersCount"),
            "description": (actor.get("description") or "").replace("\n", " ")[:90],
        })
    return out


def parse_name_file(text: str) -> list[str]:
    """Um nome por linha, `#` comenta. Igual ao arquivo de sementes, mas para busca."""
    return parse_seed_file(text)


# Frases com que uma conta se declara NÃO oficial. Casar texto é mecânico e não
# substitui conferência humana — mas pega o caso mais comum, que é a conta
# avisando na própria bio que não é quem o nome sugere.
_NAO_OFICIAL = (
    "não oficial", "nao oficial", "conta de fã", "conta de fa", "página de fans",
    "pagina de fans", "fan account", "fã clube", "fa clube", "conta de meme",
    "não sou o", "nao sou o", "paródia", "parodia", "perfil de apoio",
    "perfil não oficial", "shitpost", "apoiador", "fake",
)


# Negação isolada no NOME DE EXIBIÇÃO. "Silas Não o Malafaia", "Not Guilherme
# Boulos" — convenção comum de paródia e homônimo no Bluesky. Restrito ao nome
# de exibição de propósito: é curto, e um nome real raramente traz "não"/"not"
# como palavra inteira. Na bio, a mesma busca daria falso positivo à vontade.
_NEGACAO_NO_NOME = re.compile(r"\b(n[ãa]o|not)\b", re.IGNORECASE)


def flag_declared_unofficial(display_name: str, description: str) -> str | None:
    """Devolve o trecho que declara não-oficialidade, se houver."""
    blob = f"{display_name} {description}".lower()
    for marca in _NAO_OFICIAL:
        if marca in blob:
            return marca
    achado = _NEGACAO_NO_NOME.search(display_name or "")
    if achado:
        return f"negação no nome ('{achado.group(0)}')"
    return None


def get_profiles(dids: list[str]) -> dict[str, dict]:
    """Perfis completos por DID: seguidores, posts, data de criação.

    Em lotes de 25, que é o limite do endpoint. Falha de lote não derruba os
    demais — uma lista de 31 nomes não pode ser perdida por um erro isolado.
    """
    out: dict[str, dict] = {}
    for i in range(0, len(dids), PROFILES_BATCH):
        lote = dids[i:i + PROFILES_BATCH]
        query = "&".join(f"actors={urllib.parse.quote(d)}" for d in lote)
        try:
            with urllib.request.urlopen(f"{PROFILES_URL}?{query}", timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
        for profile in payload.get("profiles", []):
            out[profile.get("did", "")] = {
                "followers": profile.get("followersCount"),
                "posts": profile.get("postsCount"),
                "created_at": (profile.get("createdAt") or "")[:10],
            }
    return out


def search_actors_enriched(name: str, limit: int = 5) -> list[dict]:
    """Busca + enriquecimento, ordenado por seguidores.

    A conta real quase sempre tem ordem de grandeza a mais de seguidores que o
    fã-clube homônimo. Ordenar por isso põe o candidato provável no topo, mas
    não decide nada: quem confere é a pessoa.
    """
    candidatos = search_actors(name, limit=limit)
    perfis = get_profiles([c["did"] for c in candidatos if c["did"]])
    for c in candidatos:
        c.update(perfis.get(c["did"], {"followers": None, "posts": None, "created_at": ""}))
        c["nao_oficial"] = flag_declared_unofficial(c["display_name"], c["description"])
        c["dominio_proprio"] = not c["handle"].endswith(".bsky.social")
    candidatos.sort(key=lambda c: (c["followers"] is None, -(c["followers"] or 0)))
    return candidatos
