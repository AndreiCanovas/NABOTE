"""Ingestão: evento cru → raw_payload → actor / post / interaction.

Ordem deliberada, e ela importa: o payload bruto é gravado ANTES de qualquer
parsing. Se o parser quebrar num formato inesperado, o dado já está salvo e o
reprocessamento não custa nova coleta — que no X custaria dinheiro.

Toda a ingestão é idempotente por `(platform, platform_post_id)`. Reprocessar
o mesmo run não duplica nada.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Iterator

from . import atproto
from .atproto import NormalizedEvent, PLATFORM
from .db import utcnow

COMMIT_EVERY = 250


@dataclass
class Stats:
    events_seen: int = 0
    events_ignored: int = 0
    actors_new: int = 0
    posts_new: int = 0
    posts_dup: int = 0
    interactions_new: int = 0
    deletes_applied: int = 0
    deletes_unknown: int = 0
    identities_updated: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _iso_from_time_us(time_us: int) -> str:
    return datetime.fromtimestamp(time_us / 1_000_000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# C < B < A. Só serve para comparar, nunca para rebaixar.
_TIER_RANK = {"C": 0, "B": 1, "A": 2}


def upsert_actor(
    conn: sqlite3.Connection, did: str, tier: str, stats: Stats | None = None
) -> int:
    """Devolve o actor_id, criando o ator se for a primeira vez que o vemos.

    Tier SOBE, nunca desce. Um ator que já apareceu como alvo de aresta (C) e
    depois tem post próprio coletado é, por definição, alguém que está na sua
    lista de coleta — e precisa virar A/B mesmo tendo sido visto como alvo
    primeiro. Sem isso, o tier passaria a depender da ordem de chegada dos
    eventos, que é aleatória.

    O caminho inverso nunca acontece aqui: rebaixar é decisão de curadoria
    (frente 02), não efeito colateral da ingestão.
    """
    row = conn.execute(
        "SELECT actor_id, tier FROM actor WHERE platform = ? AND platform_user_id = ?",
        (PLATFORM, did),
    ).fetchone()
    if row:
        if _TIER_RANK.get(tier, 0) > _TIER_RANK.get(row["tier"], 0):
            conn.execute(
                "UPDATE actor SET tier = ?, last_seen_at = ? WHERE actor_id = ?",
                (tier, utcnow(), row["actor_id"]),
            )
        else:
            conn.execute(
                "UPDATE actor SET last_seen_at = ? WHERE actor_id = ?",
                (utcnow(), row["actor_id"]),
            )
        return row["actor_id"]

    now = utcnow()
    cur = conn.execute(
        "INSERT INTO actor (platform, platform_user_id, tier, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?)",
        (PLATFORM, did, tier, now, now),
    )
    if stats:
        stats.actors_new += 1
    return cur.lastrowid


def _store_raw(conn: sqlite3.Connection, run_id: int, rid: str, event: dict[str, Any]) -> None:
    blob = gzip.compress(json.dumps(event, ensure_ascii=False).encode("utf-8"))
    conn.execute(
        "INSERT OR REPLACE INTO raw_payload "
        "(run_id, platform, platform_post_id, fetched_at, payload_gz) VALUES (?,?,?,?,?)",
        (run_id, PLATFORM, rid, utcnow(), blob),
    )


def _apply_delete(conn: sqlite3.Connection, rid: str, stats: Stats) -> None:
    """Honra a exclusão: conteúdo sai, aresta fica marcada.

    `raw_payload` é removido em todos os runs, não só no atual — senão a cópia
    do conteúdo sobreviveria no arquivo bruto e a exclusão seria cosmética.
    """
    row = conn.execute(
        "SELECT post_id FROM post WHERE platform = ? AND platform_post_id = ?",
        (PLATFORM, rid),
    ).fetchone()
    if not row:
        stats.deletes_unknown += 1
        return
    conn.execute(
        "UPDATE post SET deleted_at = ?, text = NULL WHERE post_id = ?", (utcnow(), row[0])
    )
    conn.execute(
        "DELETE FROM raw_payload WHERE platform = ? AND platform_post_id = ?",
        (PLATFORM, rid),
    )
    stats.deletes_applied += 1


def _insert_post(
    conn: sqlite3.Connection, run_id: int, ev: NormalizedEvent, actor_id: int,
    parent_actor_id: int | None, parent_rid: str | None, stats: Stats,
) -> int | None:
    created_at = ev.created_at or _iso_from_time_us(ev.time_us)
    cur = conn.execute(
        "INSERT INTO post (platform, platform_post_id, actor_id, created_at, lang, text, "
        "post_type, parent_platform_post_id, parent_actor_id, collected_at, run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (platform, platform_post_id) DO NOTHING",
        (PLATFORM, ev.rid, actor_id, created_at, ev.lang, ev.text, ev.post_type,
         parent_rid, parent_actor_id, utcnow(), run_id),
    )
    if cur.rowcount == 0:
        stats.posts_dup += 1
        return None
    stats.posts_new += 1
    return cur.lastrowid


def handle_event(
    conn: sqlite3.Connection, run_id: int, event: dict[str, Any],
    stats: Stats, author_tier: str = "C",
) -> None:
    ev = atproto.normalize(event)
    if ev is None:
        stats.events_ignored += 1
        return

    if ev.kind == "identity":
        if ev.handle:
            actor_id = upsert_actor(conn, ev.did, author_tier, stats)
            conn.execute(
                "UPDATE actor SET handle = ? WHERE actor_id = ? AND "
                "(handle IS NULL OR handle <> ?)",
                (ev.handle, actor_id, ev.handle),
            )
            stats.identities_updated += 1
        return

    rid = ev.rid
    if not rid:
        stats.events_ignored += 1
        return

    if ev.operation == "delete":
        _apply_delete(conn, rid, stats)
        return

    # bruto antes de qualquer parsing persistido
    _store_raw(conn, run_id, rid, event)

    actor_id = upsert_actor(conn, ev.did, author_tier, stats)

    # O alvo principal (repost/reply/quote) vira o pai do post. É por aqui que
    # um ator Tier C entra no grafo sem nunca ter sido coletado.
    primary = next((t for t in ev.targets if t.kind in ("repost", "reply", "quote")), None)
    parent_actor_id = None
    parent_rid = None
    if primary:
        parent_actor_id = upsert_actor(conn, primary.did, "C", stats)
        parsed = atproto.parse_at_uri(primary.uri or "")
        parent_rid = parsed.rid if parsed else None

    post_id = _insert_post(conn, run_id, ev, actor_id, parent_actor_id, parent_rid, stats)
    if post_id is None:
        return

    for target in ev.targets:
        dst_id = parent_actor_id if (primary and target is primary) else \
            upsert_actor(conn, target.did, "C", stats)
        if dst_id == actor_id:
            continue
        cur = conn.execute(
            "INSERT INTO interaction (post_id, src_actor_id, dst_actor_id, kind, occurred_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT (post_id, kind, dst_actor_id) DO NOTHING",
            (post_id, actor_id, dst_id, target.kind,
             ev.created_at or _iso_from_time_us(ev.time_us)),
        )
        if cur.rowcount:
            stats.interactions_new += 1


def start_run(
    conn: sqlite3.Connection, source: str, kind: str = "baseline",
    campaign_label: str | None = None, query: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO collection_run (source, kind, campaign_label, query, started_at) "
        "VALUES (?,?,?,?,?)",
        (source, kind, campaign_label, query, utcnow()),
    )
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection, run_id: int, stats: Stats,
    status: str = "ok", cost_usd: float = 0.0, error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE collection_run SET ended_at = ?, items_fetched = ?, cost_usd = ?, "
        "status = ?, error = ? WHERE run_id = ?",
        (utcnow(), stats.posts_new, cost_usd, status, error, run_id),
    )


def get_cursor(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute(
        "SELECT cursor FROM source_state WHERE source = ?", (source,)
    ).fetchone()
    return row["cursor"] if row else None


def set_cursor(conn: sqlite3.Connection, source: str, cursor: str) -> None:
    conn.execute(
        "INSERT INTO source_state (source, cursor, updated_at) VALUES (?,?,?) "
        "ON CONFLICT (source) DO UPDATE SET cursor = excluded.cursor, "
        "updated_at = excluded.updated_at",
        (source, cursor, utcnow()),
    )


def ingest(
    conn: sqlite3.Connection, source: Any, *, kind: str = "baseline",
    campaign_label: str | None = None, max_events: int | None = None,
    max_seconds: float | None = None, author_tier: str = "C",
    resume: bool = True, cost_usd: float = 0.0,
) -> tuple[int, Stats]:
    """Consome a fonte até o teto de eventos ou de tempo. Devolve (run_id, stats).

    Os tetos não são conveniência: o plano exige que orçamento seja código, e
    um coletor sem limite contra um firehose é um jeito de descobrir isso pela
    fatura. Aqui o custo é zero, mas a mesma função serve para o X.
    """
    run_id = start_run(conn, source.name, kind, campaign_label)
    stats = Stats()
    started = time.monotonic()
    cursor = get_cursor(conn, source.name) if resume else None
    last_cursor = cursor
    status = "ok"
    error = None

    try:
        conn.execute("BEGIN")
        for event in source.events(cursor):
            stats.events_seen += 1
            handle_event(conn, run_id, event, stats, author_tier)
            time_us = event.get("time_us")
            if isinstance(time_us, int):
                last_cursor = str(time_us)

            if stats.events_seen % COMMIT_EVERY == 0:
                if last_cursor:
                    set_cursor(conn, source.name, last_cursor)
                conn.execute("COMMIT")
                conn.execute("BEGIN")

            if max_events is not None and stats.events_seen >= max_events:
                break
            if max_seconds is not None and (time.monotonic() - started) >= max_seconds:
                break
    except Exception as exc:  # noqa: BLE001 — o run precisa registrar a falha
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if last_cursor:
            set_cursor(conn, source.name, last_cursor)
        finish_run(conn, run_id, stats, status=status, cost_usd=cost_usd, error=error)
        conn.execute("COMMIT")

    return run_id, stats
