"""Ingestão: evento normalizado → raw_payload → actor / post / interaction.

Ordem deliberada, e ela importa: o payload bruto é gravado ANTES de qualquer
escrita derivada. Se o mapeamento estiver errado, o dado já está salvo e o
reprocessamento não custa nova coleta — que no X custa dinheiro.

Este módulo não sabe de plataforma. Quem traduz o formato de cada rede é a
fonte, em `sources/`; aqui só chega `NormalizedEvent`.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Any

from .db import utcnow
from .events import NormalizedEvent

COMMIT_EVERY = 250

# C < B < A. Só serve para comparar, nunca para rebaixar.
_TIER_RANK = {"C": 0, "B": 1, "A": 2}


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


def upsert_actor(
    conn: sqlite3.Connection, platform: str, uid: str, tier: str,
    handle: str | None = None, stats: Stats | None = None,
) -> int:
    """Devolve o actor_id, criando o ator se for a primeira vez que o vemos.

    Tier SOBE, nunca desce. Um ator visto antes só como alvo de aresta (C) que
    depois tem post próprio coletado está, por definição, na lista de coleta —
    e precisa virar A/B mesmo tendo sido visto como alvo primeiro. Sem isso o
    tier dependeria da ordem de chegada dos eventos, que é aleatória.

    O handle é atualizado sempre que vier: ele muda com o tempo, e o último
    visto é o mais útil. O que nunca muda é o `uid`, que é a chave.
    """
    row = conn.execute(
        "SELECT actor_id, tier, handle FROM actor "
        "WHERE platform = ? AND platform_user_id = ?", (platform, uid),
    ).fetchone()

    if row:
        sobe = _TIER_RANK.get(tier, 0) > _TIER_RANK.get(row["tier"], 0)
        novo_handle = handle and handle != row["handle"]
        if sobe or novo_handle:
            conn.execute(
                "UPDATE actor SET tier = ?, handle = ?, last_seen_at = ? WHERE actor_id = ?",
                (tier if sobe else row["tier"], handle or row["handle"],
                 utcnow(), row["actor_id"]),
            )
        else:
            conn.execute("UPDATE actor SET last_seen_at = ? WHERE actor_id = ?",
                         (utcnow(), row["actor_id"]))
        return row["actor_id"]

    now = utcnow()
    cur = conn.execute(
        "INSERT INTO actor (platform, platform_user_id, handle, tier, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
        (platform, uid, handle, tier, now, now),
    )
    if stats:
        stats.actors_new += 1
    return cur.lastrowid


def _store_raw(conn: sqlite3.Connection, run_id: int, platform: str,
               post_uid: str, raw: dict[str, Any]) -> None:
    blob = gzip.compress(json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8"))
    conn.execute(
        "INSERT OR REPLACE INTO raw_payload "
        "(run_id, platform, platform_post_id, fetched_at, payload_gz) VALUES (?,?,?,?,?)",
        (run_id, platform, post_uid, utcnow(), blob),
    )


def _apply_delete(conn: sqlite3.Connection, platform: str, post_uid: str,
                  stats: Stats) -> None:
    """Honra a exclusão: conteúdo sai, aresta fica marcada.

    `raw_payload` é removido em TODOS os runs, não só no atual — senão a cópia
    do conteúdo sobreviveria no arquivo bruto e a exclusão seria cosmética.
    """
    row = conn.execute(
        "SELECT post_id FROM post WHERE platform = ? AND platform_post_id = ?",
        (platform, post_uid),
    ).fetchone()
    if not row:
        stats.deletes_unknown += 1
        return
    conn.execute("UPDATE post SET deleted_at = ?, text = NULL WHERE post_id = ?",
                 (utcnow(), row["post_id"]))
    conn.execute("DELETE FROM raw_payload WHERE platform = ? AND platform_post_id = ?",
                 (platform, post_uid))
    stats.deletes_applied += 1


def handle_event(conn: sqlite3.Connection, run_id: int, ev: NormalizedEvent,
                 stats: Stats, author_tier: str = "C") -> None:
    if ev is None:
        stats.events_ignored += 1
        return

    if ev.kind == "identity":
        if ev.actor_handle:
            upsert_actor(conn, ev.platform, ev.actor_uid, author_tier,
                         ev.actor_handle, stats)
            stats.identities_updated += 1
        return

    if not ev.post_uid:
        stats.events_ignored += 1
        return

    if ev.kind == "delete":
        _apply_delete(conn, ev.platform, ev.post_uid, stats)
        return

    _store_raw(conn, run_id, ev.platform, ev.post_uid, ev.raw)
    actor_id = upsert_actor(conn, ev.platform, ev.actor_uid, author_tier,
                            ev.actor_handle, stats)

    # O alvo principal vira o pai do post. É por aqui que um ator Tier C entra
    # no grafo sem nunca ter sido coletado.
    principal = next((t for t in ev.targets
                      if t.kind in ("repost", "reply", "quote")), None)
    parent_actor_id = None
    parent_uid = None
    if principal:
        parent_actor_id = upsert_actor(conn, ev.platform, principal.uid, "C",
                                       principal.handle, stats)
        parent_uid = principal.post_uid

    cur = conn.execute(
        "INSERT INTO post (platform, platform_post_id, actor_id, created_at, lang, text, "
        "post_type, parent_platform_post_id, parent_actor_id, collected_at, run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (platform, platform_post_id) DO NOTHING",
        (ev.platform, ev.post_uid, actor_id, ev.occurred_at, ev.lang, ev.text,
         ev.post_type, parent_uid, parent_actor_id, utcnow(), run_id),
    )
    if cur.rowcount == 0:
        stats.posts_dup += 1
        return
    stats.posts_new += 1
    post_id = cur.lastrowid

    for alvo in ev.targets:
        dst_id = (parent_actor_id if (principal and alvo is principal)
                  else upsert_actor(conn, ev.platform, alvo.uid, "C", alvo.handle, stats))
        if dst_id == actor_id:
            continue
        cur = conn.execute(
            "INSERT INTO interaction (post_id, src_actor_id, dst_actor_id, kind, occurred_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT (post_id, kind, dst_actor_id) DO NOTHING",
            (post_id, actor_id, dst_id, alvo.kind, ev.occurred_at),
        )
        if cur.rowcount:
            stats.interactions_new += 1


def start_run(conn: sqlite3.Connection, source: str, kind: str = "baseline",
              campaign_label: str | None = None, query: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO collection_run (source, kind, campaign_label, query, started_at) "
        "VALUES (?,?,?,?,?)", (source, kind, campaign_label, query, utcnow()))
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, stats: Stats,
               status: str = "ok", cost_usd: float = 0.0, error: str | None = None) -> None:
    conn.execute(
        "UPDATE collection_run SET ended_at = ?, items_fetched = ?, cost_usd = ?, "
        "status = ?, error = ? WHERE run_id = ?",
        (utcnow(), stats.posts_new, cost_usd, status, error, run_id))


def get_cursor(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute("SELECT cursor FROM source_state WHERE source = ?",
                       (source,)).fetchone()
    return row["cursor"] if row else None


def set_cursor(conn: sqlite3.Connection, source: str, cursor: str) -> None:
    conn.execute(
        "INSERT INTO source_state (source, cursor, updated_at) VALUES (?,?,?) "
        "ON CONFLICT (source) DO UPDATE SET cursor = excluded.cursor, "
        "updated_at = excluded.updated_at", (source, cursor, utcnow()))


def ingest(conn: sqlite3.Connection, source: Any, *, kind: str = "baseline",
           campaign_label: str | None = None, max_events: int | None = None,
           max_seconds: float | None = None, author_tier: str = "C",
           resume: bool = True, cost_usd: float = 0.0) -> tuple[int, Stats]:
    """Consome a fonte até o teto de eventos ou de tempo. Devolve (run_id, stats).

    Os tetos não são conveniência: o plano exige que orçamento seja código, e
    um coletor sem limite contra um firehose é um jeito de descobrir isso pela
    fatura.
    """
    run_id = start_run(conn, source.name, kind, campaign_label)
    stats = Stats()
    started = time.monotonic()
    cursor = get_cursor(conn, source.name) if resume else None
    last_cursor = cursor
    status, error = "ok", None

    try:
        conn.execute("BEGIN")
        for ev in source.events(cursor):
            stats.events_seen += 1
            handle_event(conn, run_id, ev, stats, author_tier)
            if ev is not None and ev.cursor:
                last_cursor = ev.cursor

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
        # o que a fonte descartou por ser irrelevante nunca chega aqui, mas o
        # número importa para explicar uma coleta de volume baixo
        stats.events_ignored += getattr(source, "skipped", 0)
        if last_cursor:
            set_cursor(conn, source.name, last_cursor)
        finish_run(conn, run_id, stats, status=status, cost_usd=cost_usd, error=error)
        conn.execute("COMMIT")

    return run_id, stats
