"""Conexão e migração do banco.

O banco é um arquivo SQLite. A escolha é deliberada e está justificada no plano
(frente 04): no volume do MVP o grafo cabe em memória e um banco de grafos não
resolve nenhum problema que exista aqui. O gatilho para migrar para Postgres é
concorrência de escrita, não tamanho.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Tabelas STRICT exigem SQLite >= 3.37. Sem elas o schema aceitaria texto numa
# coluna INTEGER em silêncio, que é a classe de bug mais cara de descobrir tarde.
MIN_SQLITE = (3, 37, 0)

DEFAULT_DB = Path("data/nabote.db")
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_MIGRATION_RE = re.compile(r"^(\d{3})_(.+)\.sql$")


def utcnow() -> str:
    """Timestamp ISO-8601 UTC, o formato usado em todo o schema."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_sqlite_version() -> None:
    actual = tuple(int(p) for p in sqlite3.sqlite_version.split("."))
    if actual < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ é necessário para tabelas "
            f"STRICT; este Python está com {sqlite3.sqlite_version}."
        )


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Abre o banco com os pragmas que o schema pressupõe.

    `foreign_keys` é por conexão no SQLite e vem DESLIGADO por padrão — sem
    ligar aqui, todas as FKs do schema seriam decorativas.
    """
    _check_sqlite_version()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version    INTEGER PRIMARY KEY,
          name       TEXT NOT NULL,
          applied_at TEXT NOT NULL
        ) STRICT
        """
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_migrations_table(conn)
    return {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}


def current_version(conn: sqlite3.Connection) -> int:
    applied = applied_versions(conn)
    return max(applied) if applied else 0


def discover_migrations(directory: Path | str = MIGRATIONS_DIR) -> list[tuple[int, str, Path]]:
    """Migrações disponíveis, ordenadas. Nome: NNN_descricao.sql"""
    directory = Path(directory)
    found: list[tuple[int, str, Path]] = []
    for entry in sorted(directory.glob("*.sql")):
        match = _MIGRATION_RE.match(entry.name)
        if not match:
            raise ValueError(
                f"Migração com nome fora do padrão NNN_descricao.sql: {entry.name}"
            )
        found.append((int(match.group(1)), match.group(2), entry))
    versions = [v for v, _, _ in found]
    if len(versions) != len(set(versions)):
        raise ValueError(f"Versões de migração duplicadas em {directory}")
    return found


def migrate(
    conn: sqlite3.Connection, directory: Path | str = MIGRATIONS_DIR
) -> list[tuple[int, str]]:
    """Aplica as migrações pendentes. Idempotente: rodar de novo não faz nada.

    Retorna a lista de (versão, nome) que foi aplicada nesta chamada.
    """
    already = applied_versions(conn)
    applied_now: list[tuple[int, str]] = []

    for version, name, path in discover_migrations(directory):
        if version in already:
            continue
        sql = path.read_text(encoding="utf-8")
        # executescript emite COMMIT antes de rodar, então o registro da migração
        # vai numa transação própria logo depois.
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, utcnow()),
        )
        applied_now.append((version, name))

    return applied_now


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Contagem de linhas por tabela. Usado pelo `status` e pelos testes."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    counts = {}
    for row in rows:
        name = row["name"]
        counts[name] = conn.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"]
    return counts
