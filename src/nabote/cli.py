"""CLI do instrumento.

Cada etapa do pipeline (frente 05) é um subcomando separado e retomável, para
que uma falha às 3 da manhã no `fetch` não obrigue a refazer o que já foi pago.
Neste passo só `init` e `status` existem; os demais entram na ordem da frente 05.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, db, graph, ingest


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


def load_seeds(path: Path) -> list[str]:
    """Um DID por linha; `#` comenta. É a lista curada à mão da frente 02."""
    dids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            dids.append(line)
    return dids


def cmd_fetch(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        if db.current_version(conn) == 0:
            print("banco não migrado. Rode: nabote init", file=sys.stderr)
            return 1

        if args.fixture:
            from .sources import FixtureSource
            source = FixtureSource(Path(args.fixture))
            print(f"fonte    fixture {args.fixture}")
        else:
            from .sources import JetstreamSource
            seeds = load_seeds(Path(args.seeds)) if args.seeds else []
            source = JetstreamSource(host=args.host, wanted_dids=seeds)
            print(f"fonte    jetstream {args.host}")
            print(f"filtro   {len(seeds) or 'nenhum — firehose global'} "
                  f"{'DIDs' if seeds else ''}".rstrip())

        cursor = ingest.get_cursor(conn, source.name) if not args.no_resume else None
        print(f"cursor   {cursor or 'nenhum — começando do evento mais recente'}")
        if args.max_events or args.max_seconds:
            teto = ", ".join(filter(None, [
                f"{args.max_events} eventos" if args.max_events else None,
                f"{args.max_seconds}s" if args.max_seconds else None]))
            print(f"teto     {teto}")
        print()

        try:
            run_id, stats = ingest.ingest(
                conn, source, kind=args.kind, campaign_label=args.campaign,
                max_events=args.max_events, max_seconds=args.max_seconds,
                author_tier=args.author_tier, resume=not args.no_resume,
            )
        except KeyboardInterrupt:
            print("\ninterrompido — o cursor foi salvo, `fetch` retoma daqui", file=sys.stderr)
            return 130

        print(f"run #{run_id}")
        for key, value in stats.as_dict().items():
            if value:
                print(f"  {key:22s} {value:>8,}".replace(",", "."))
        if not stats.events_seen:
            print("  nenhum evento — nada novo desde o cursor")
        return 0
    finally:
        conn.close()


def _windows(conn, args) -> list[str]:
    if args.window:
        return [args.window]
    found = graph.windows_present(conn)
    if not found:
        return []
    return found if args.all else found[-1:]


def cmd_aggregate(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        windows = _windows(conn, args)
        if not windows:
            print("nenhuma interação no banco. Rode `fetch` antes.", file=sys.stderr)
            return 1
        for window in windows:
            n = graph.aggregate_window(conn, window, scope=args.scope)
            print(f"{window}  {n:>6} arestas agregadas  (scope={args.scope})")
        return 0
    finally:
        conn.close()


def cmd_analyze(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        windows = _windows(conn, args)
        if not windows:
            print("nenhuma interação no banco. Rode `fetch` antes.", file=sys.stderr)
            return 1
        for window in windows:
            r = graph.analyze_window(conn, window, view=args.view, edge_scope=args.scope)
            if not r["nodes"]:
                print(f"{window}  vazio para a visão {args.view}")
                continue
            print(f"{window}  scope={r['scope']:<12} {r['nodes']:>5} nós  "
                  f"{r['edges']:>6} arestas  {r['communities']:>3} comunidades")
        return 0
    finally:
        conn.close()


def cmd_dump(args: argparse.Namespace) -> int:
    """Dump cru para depuração — não é a camada de exportação.

    O plano prevê exatamente isto logo depois do passo 2: um jeito rápido de
    enxergar o que a coleta trouxe, antes de existir qualquer relatório.
    """
    conn = db.connect(args.db)
    try:
        windows = _windows(conn, args)
        if not windows:
            print("nada a mostrar.", file=sys.stderr)
            return 1
        window = windows[-1]
        scope = args.view if args.scope == "all" else f"{args.view}:{args.scope}"

        head = conn.execute(
            "SELECT COUNT(*) AS n FROM actor_metric WHERE window_start=? AND scope=?",
            (window, scope)).fetchone()["n"]
        if not head:
            print(f"janela {window} sem métricas para scope={scope}. "
                  f"Rode `analyze --view {args.view}`.", file=sys.stderr)
            return 1

        print(f"janela {window}   visão {scope}\n")

        rows = conn.execute("""
            SELECT a.handle, a.platform_user_id AS did, a.tier,
                   MAX(CASE WHEN m.metric='pagerank'    THEN m.value END) pr,
                   MAX(CASE WHEN m.metric='in_degree_w' THEN m.value END) ind,
                   MAX(CASE WHEN m.metric='ei_index'    THEN m.value END) ei,
                   c.community_id AS com
            FROM actor_metric m
            JOIN actor a ON a.actor_id = m.actor_id
            LEFT JOIN actor_community c ON c.actor_id = m.actor_id
                 AND c.window_start = m.window_start AND c.scope = m.scope
            WHERE m.window_start=? AND m.scope=?
            GROUP BY a.actor_id ORDER BY pr DESC LIMIT ?
        """, (window, scope, args.top)).fetchall()

        print(f"{'ator':<34}{'tier':<6}{'com':<5}{'pagerank':>10}{'in-deg':>9}{'E-I':>8}")
        print("-" * 72)
        for r in rows:
            nome = r["handle"] or r["did"]
            print(f"{nome[:33]:<34}{r['tier']:<6}{r['com'] if r['com'] is not None else '-':<5}"
                  f"{r['pr']:>10.4f}{r['ind']:>9.1f}{r['ei']:>8.2f}")

        print("\ncomunidades")
        for r in conn.execute(
            "SELECT community_id, size, ei_mean FROM community "
            "WHERE window_start=? AND scope=? ORDER BY size DESC", (window, scope)):
            ei = f"{r['ei_mean']:+.2f}" if r["ei_mean"] is not None else "  -  "
            print(f"  #{r['community_id']:<4} {r['size']:>4} atores   E-I médio {ei}")

        print("\narestas mais pesadas")
        for r in conn.execute("""
            SELECT s.handle AS sh, s.platform_user_id AS sd,
                   d.handle AS dh, d.platform_user_id AS dd, e.kind, e.weight
            FROM edge_window e
            JOIN actor s ON s.actor_id=e.src_actor_id
            JOIN actor d ON d.actor_id=e.dst_actor_id
            WHERE e.window_start=? AND e.scope=?
            ORDER BY e.weight DESC LIMIT ?
        """, (window, args.scope, args.top)).fetchall():
            print(f"  {(r['sh'] or r['sd'])[:26]:<27} -{r['kind']:>8}-> "
                  f"{(r['dh'] or r['dd'])[:26]:<27} {r['weight']:.0f}")
        return 0
    finally:
        conn.close()


class JetstreamDefaults:
    """Constantes lidas sem importar o cliente WebSocket."""
    host = "jetstream2.us-east.bsky.network"


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

    fetch = sub.add_parser("fetch", help="coleta eventos e grava post/interaction")
    fetch.add_argument("--fixture", help="lê de um arquivo JSONL em vez da rede (testes)")
    fetch.add_argument("--seeds", help="arquivo com um DID por linha; filtra o firehose")
    fetch.add_argument("--host", default=JetstreamDefaults.host,
                       help=f"instância do Jetstream (padrão: {JetstreamDefaults.host})")
    fetch.add_argument("--kind", default="baseline", choices=["baseline", "campanha"])
    fetch.add_argument("--campaign", help="rótulo da campanha; obrigatório se --kind=campanha")
    fetch.add_argument("--max-events", type=int, default=None,
                       help="teto de eventos — orçamento é código, não disciplina")
    fetch.add_argument("--max-seconds", type=float, default=None, help="teto de tempo")
    fetch.add_argument("--author-tier", default="C", choices=["A", "B", "C"],
                       help="tier dado a autores novos (use A numa coleta com --seeds)")
    fetch.add_argument("--no-resume", action="store_true",
                       help="ignora o cursor salvo e começa do zero")

    def _janela(p, com_view=False):
        p.add_argument("--window", help="janela YYYY-MM-DD (segunda-feira)")
        p.add_argument("--all", action="store_true", help="todas as janelas com dado")
        p.add_argument("--scope", default="all", help="'all' ou 'topic:<id>'")
        if com_view:
            p.add_argument("--view", default=graph.DEFAULT_VIEW, choices=sorted(graph.VIEWS),
                           help="amp = repost+citação (padrão) · reply = respostas")
        return p

    _janela(sub.add_parser("aggregate", help="interaction → edge_window"))
    _janela(sub.add_parser("analyze", help="grafo, comunidades e métricas"), com_view=True)
    d = _janela(sub.add_parser("dump", help="dump cru da janela, para depuração"),
                com_view=True)
    d.add_argument("--top", type=int, default=15)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fetch" and args.kind == "campanha" and not args.campaign:
        build_parser().error("--kind=campanha exige --campaign RÓTULO")
    return {"init": cmd_init, "status": cmd_status, "fetch": cmd_fetch,
            "aggregate": cmd_aggregate, "analyze": cmd_analyze,
            "dump": cmd_dump}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
