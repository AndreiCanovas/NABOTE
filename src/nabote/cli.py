"""CLI do instrumento.

Cada etapa do pipeline (frente 05) é um subcomando separado e retomável, para
que uma falha às 3 da manhã no `fetch` não obrigue a refazer o que já foi pago.
Neste passo só `init` e `status` existem; os demais entram na ordem da frente 05.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, db


def cmd_init(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        before = db.current_version(conn)
        applied = db.migrate(conn)
        after = db.current_version(conn)
        if applied:
            for version, name in applied:
                print(f"aplicada  {version:03d}  {name}")
            print(f"\nbanco em {args.db} — schema v{before} → v{after}")
        else:
            print(f"banco em {args.db} já está na v{after}; nada a fazer")
        return 0
    finally:
        conn.close()


def cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.db)
    if not path.exists():
        print(f"banco não existe em {path}. Rode: nabote init", file=sys.stderr)
        return 1

    conn = db.connect(path)
    try:
        print(f"banco    {path}  ({path.stat().st_size / 1024:.1f} KiB)")
        print(f"schema   v{db.current_version(conn)}")

        counts = db.table_counts(conn)
        counts.pop("schema_migrations", None)
        populated = {k: v for k, v in counts.items() if v}
        print(f"tabelas  {len(counts)}  ({len(populated)} com dados)")
        for name, n in sorted(populated.items(), key=lambda kv: -kv[1]):
            print(f"  {name:26s} {n:>9,}".replace(",", "."))

        run = conn.execute(
            "SELECT run_id, kind, source, started_at, status, items_fetched, cost_usd "
            "FROM collection_run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if run:
            print(
                f"\núltimo run  #{run['run_id']} {run['kind']} via {run['source']}"
                f"\n            {run['started_at']} — {run['status']}, "
                f"{run['items_fetched']} itens, US$ {run['cost_usd']:.4f}"
            )
        else:
            print("\núltimo run  nenhum — o banco está vazio, como esperado no passo 0")

        total = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS t FROM collection_run"
        ).fetchone()["t"]
        print(f"custo total US$ {total:.4f}")
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nabote", description="Instrumento analítico de mapeamento do discurso público."
    )
    parser.add_argument("--version", action="version", version=f"nabote {__version__}")
    parser.add_argument(
        "--db", default=str(db.DEFAULT_DB), help=f"caminho do banco (padrão: {db.DEFAULT_DB})"
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="cria o banco e aplica as migrações pendentes")
    sub.add_parser("status", help="mostra versão do schema, volume e custo acumulado")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {"init": cmd_init, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
