"""CLI do instrumento.

Cada etapa do pipeline (frente 05) é um subcomando separado e retomável, para
que uma falha às 3 da manhã no `fetch` não obrigue a refazer o que já foi pago.
Neste passo só `init` e `status` existem; os demais entram na ordem da frente 05.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, db, graph, identity, ingest, probe


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
            if args.seeds:
                seeds = load_seeds(Path(args.seeds))
            elif getattr(args, "global_firehose", False):
                seeds = []
            else:
                seeds = identity.seed_dids(conn)
                if not seeds:
                    print("nenhuma semente registrada. Rode `nabote seeds --file lista.txt`,\n"
                          "ou passe --global para consumir o firehose inteiro.",
                          file=sys.stderr)
                    return 1
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
                  f"{r['edges']:>6} arestas  {r['communities']:>4} comunidades")
            print(f"{'':12}  maior componente {r['largest']} nós "
                  f"({r['core_share']:.0%} do grafo) · "
                  f"{r['trivial']} componentes de até {graph.TRIVIAL_COMPONENT} atores")
            if r["core_share"] < 0.5:
                print(f"{'':12}  ATENÇÃO: menos da metade dos atores está no núcleo. "
                      f"O grafo é uma pilha de cacos, não uma rede.")
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

        recorte = "" if args.community is None else " AND c.community_id = :com"
        rows = conn.execute(f"""
            SELECT a.handle, a.platform_user_id AS did, a.tier,
                   MAX(CASE WHEN m.metric='pagerank'    THEN m.value END) pr,
                   MAX(CASE WHEN m.metric='in_degree_w' THEN m.value END) ind,
                   MAX(CASE WHEN m.metric='ei_index'    THEN m.value END) ei,
                   c.community_id AS com
            FROM actor_metric m
            JOIN actor a ON a.actor_id = m.actor_id
            LEFT JOIN actor_community c ON c.actor_id = m.actor_id
                 AND c.window_start = m.window_start AND c.scope = m.scope
            WHERE m.window_start=:w AND m.scope=:s{recorte}
            GROUP BY a.actor_id ORDER BY pr DESC LIMIT :n
        """, {"w": window, "s": scope, "n": args.top,
              "com": args.community}).fetchall()

        if args.community is not None:
            print(f"atores da comunidade #{args.community}"
                  + ("" if rows else "  — vazia nesta visão") + "\n")

        print(f"{'ator':<34}{'tier':<6}{'com':<5}{'pagerank':>10}{'in-deg':>9}{'E-I':>8}")
        print("-" * 72)
        for r in rows:
            nome = r["handle"] or r["did"]
            print(f"{nome[:33]:<34}{r['tier']:<6}{r['com'] if r['com'] is not None else '-':<5}"
                  f"{r['pr']:>10.4f}{r['ind']:>9.1f}{r['ei']:>8.2f}")

        todas = conn.execute(
            "SELECT community_id, size, ei_mean FROM community "
            "WHERE window_start=? AND scope=? ORDER BY size DESC",
            (window, scope)).fetchall()
        visiveis = [r for r in todas if r["size"] >= args.min_community]

        print(f"\ncomunidades  ({len(todas)} no total)")
        for r in visiveis[:args.top]:
            ei = f"{r['ei_mean']:+.2f}" if r["ei_mean"] is not None else "  -  "
            nota = "  (E-I mecânico)" if r["size"] <= graph.TRIVIAL_COMPONENT else ""
            print(f"  #{r['community_id']:<4} {r['size']:>5} atores   "
                  f"E-I médio {ei}{nota}")

        cauda = todas[len(visiveis[:args.top]):]
        if cauda:
            atores = sum(r["size"] for r in cauda)
            maior = max(r["size"] for r in cauda)
            print(f"  … + {len(cauda)} comunidades de até {maior} atores "
                  f"({atores} atores no total)")
            print(f"     comunidade de até {graph.TRIVIAL_COMPONENT} atores tem E-I "
                  f"−1,00 por construção: não existe aresta externa possível.")

        # A lista de arestas TEM de respeitar a visão. Mostrar uma citação sob
        # `--view amp` é mentira barata: o grafo analisado não a contém, e quem
        # lê o dump conclui coisa errada sobre o que produziu as comunidades.
        tipos = graph.VIEWS[args.view]
        marcadores = ",".join("?" * len(tipos))
        print(f"\narestas mais pesadas da visão {args.view} "
              f"({' + '.join(tipos)})")
        for r in conn.execute(f"""
            SELECT s.handle AS sh, s.platform_user_id AS sd,
                   d.handle AS dh, d.platform_user_id AS dd, e.kind, e.weight
            FROM edge_window e
            JOIN actor s ON s.actor_id=e.src_actor_id
            JOIN actor d ON d.actor_id=e.dst_actor_id
            WHERE e.window_start=? AND e.scope=? AND e.kind IN ({marcadores})
            ORDER BY e.weight DESC LIMIT ?
        """, (window, args.scope, *tipos, args.top)).fetchall():
            print(f"  {(r['sh'] or r['sd'])[:26]:<27} -{r['kind']:>8}-> "
                  f"{(r['dh'] or r['dd'])[:26]:<27} {r['weight']:.0f}")

        # …e o que a visão deixou de fora precisa ficar visível, senão filtrar
        # vira esconder.
        totais = conn.execute(
            "SELECT kind, SUM(weight) AS w FROM edge_window "
            "WHERE window_start=? AND scope=? GROUP BY kind ORDER BY w DESC",
            (window, args.scope)).fetchall()
        print("\npeso total por tipo na janela inteira")
        print("  " + " · ".join(
            f"{r['kind']} {r['w']:.0f}" + ("" if r["kind"] in tipos else " (fora da visão)")
            for r in totais))
        return 0
    finally:
        conn.close()


def cmd_seeds(args: argparse.Namespace) -> int:
    """Registra a lista curada. Aceita handle ou DID — você não precisa caçar DIDs."""
    conn = db.connect(args.db)
    try:
        if not args.file:
            dids = identity.seed_dids(conn)
            if not dids:
                print("nenhuma semente registrada. Use: nabote seeds --file lista.txt")
                return 0
            print(f"{len(dids)} sementes registradas\n")
            for r in conn.execute(
                "SELECT handle, platform_user_id, tier FROM actor "
                "WHERE tier IN ('A','B') ORDER BY tier, handle, platform_user_id"):
                print(f"  {r['tier']}  {(r['handle'] or '—'):<34} {r['platform_user_id']}")
            return 0

        entries = identity.parse_seed_file(Path(args.file).read_text(encoding="utf-8"))
        if not entries:
            print(f"{args.file} não tem nenhuma entrada útil.", file=sys.stderr)
            return 1
        print(f"resolvendo {len(entries)} entradas...\n")

        ok, falhas = identity.register_seeds(conn, entries, tier=args.tier)
        for nome, did in ok:
            print(f"  ok      {nome:<34} {did}")
        for nome, motivo in falhas:
            print(f"  FALHOU  {nome:<34} {motivo}", file=sys.stderr)

        print(f"\n{len(ok)} registradas como tier {args.tier}"
              + (f", {len(falhas)} falharam" if falhas else ""))
        if falhas and not ok:
            return 1
        print(f"total de sementes no banco: {len(identity.seed_dids(conn))}")
        return 0
    finally:
        conn.close()


def cmd_cycle(args: argparse.Namespace) -> int:
    """fetch → aggregate → analyze → dump. Um comando por ciclo de coleta."""
    steps = [
        ("fetch", cmd_fetch, {"fixture": None, "seeds": None, "host": args.host,
                              "kind": args.kind, "campaign": args.campaign,
                              "max_events": args.max_events, "max_seconds": args.max_seconds,
                              "author_tier": "A", "no_resume": False}),
        ("aggregate", cmd_aggregate, {"window": None, "all": True, "scope": "all"}),
        ("analyze", cmd_analyze, {"window": None, "all": True, "scope": "all",
                                  "view": args.view}),
        ("dump", cmd_dump, {"window": None, "all": False, "scope": "all",
                            "min_community": 1, "community": None,
                            "view": args.view, "top": args.top}),
    ]
    for name, func, extra in steps:
        print(f"\n{'═' * 4} {name} {'═' * (62 - len(name))}")
        sub_args = argparse.Namespace(db=args.db, command=name, **extra)
        code = func(sub_args)
        if code:
            print(f"\n`{name}` falhou (código {code}); ciclo interrompido.", file=sys.stderr)
            return code
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Busca contas do Bluesky por NOME, para você escolher — não adivinha.

    Uma lista de curadoria normalmente nasce como nomes de pessoas. No Bluesky
    boa parte delas não tem conta, e homônimos e paródias são comuns. Este
    comando mostra os candidatos; a escolha é sua, e depois vai para o arquivo
    de sementes.
    """
    nomes = (identity.parse_name_file(Path(args.file).read_text(encoding="utf-8"))
             if args.file else args.name)
    if not nomes:
        print("passe --name NOME (repetível) ou --file lista_de_nomes.txt", file=sys.stderr)
        return 1

    achou = faltou = 0
    for nome in nomes:
        print(f"\n{nome}")
        try:
            candidatos = identity.search_actors_enriched(nome, limit=args.limit)
        except identity.ResolveError as exc:
            print(f"  erro: {exc}", file=sys.stderr)
            return 1
        if not candidatos:
            print("  — nenhuma conta encontrada no Bluesky")
            faltou += 1
            continue
        achou += 1
        for c in candidatos:
            seg = f"{c['followers']:,}".replace(",", ".") if c["followers"] is not None else "?"
            posts = f"{c['posts']:,}".replace(",", ".") if c["posts"] is not None else "?"
            marcas = []
            if c["nao_oficial"]:
                marcas.append(f"⚠ diz-se não-oficial ('{c['nao_oficial']}')")
            if c["dominio_proprio"]:
                marcas.append("◆ domínio próprio")
            if c["posts"] == 0:
                marcas.append("○ nunca postou")
            print(f"  {c['handle']:<34} {seg:>9} seg  {posts:>7} posts  "
                  f"{c['created_at']}  {c['display_name'][:24]}")
            if marcas:
                print(f"  {'':<34} {' '.join(marcas)}")
            if c["description"]:
                print(f"  {'':<34} {c['description'][:88]}")

    print(f"\n{achou} nomes com candidatos, {faltou} sem nenhum.")
    print("Ordenado por seguidores. ⚠ marca conta que se declara não-oficial na bio;\n"
          "◆ domínio próprio costuma indicar conta institucional; ○ nunca postou.\n"
          "Nada disso decide — confira cada handle antes de pôr na lista de sementes.\n"
          "Coletar a conta errada atribui discurso a quem não disse.")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Mostra esquema e amostra de uma base externa, antes de escrever adaptador."""
    alvo = Path(args.path)
    if not alvo.exists():
        print(f"não encontrei {alvo}", file=sys.stderr)
        return 1

    if alvo.suffix == ".zip":
        membros = probe.list_zip_members(alvo)
        if not membros:
            print(f"{alvo} não tem nenhum .parquet dentro", file=sys.stderr)
            return 1

        total = sum(tam for _, tam in membros)
        print(f"{alvo.name}  —  {len(membros)} arquivos parquet, "
              f"{total / 1e9:.2f} GB descomprimidos\n")
        if args.list:
            for nome, tam in membros:
                print(f"  {tam / 1e6:>8.1f} MB  {nome}")
            return 0

        if args.member:
            tamanho = dict(membros).get(args.member)
            if tamanho is None:
                print(f"{args.member} não está no zip. Use --list para ver os nomes.",
                      file=sys.stderr)
                return 1
            print(f"inspecionando: {args.member}  ({tamanho / 1e6:.1f} MB)\n")
            tabela = probe.read_parquet_member(alvo, args.member)
        else:
            # Do menor para o maior, PULANDO os vazios. Arquivo de 0 linhas não
            # tem tipo para inferir e faria o diagnóstico mentir — foi o que
            # aconteceu na primeira execução contra a base real.
            tabela = None
            vazios: list[str] = []
            for nome, tamanho in sorted(membros, key=lambda par: par[1]):
                candidata = probe.read_parquet_member(alvo, nome)
                if candidata.num_rows == 0:
                    vazios.append(nome)
                    if len(vazios) >= 8:
                        break
                    continue
                if vazios:
                    print(f"pulei {len(vazios)} arquivo(s) vazio(s): "
                          f"{', '.join(v.split('/')[-1] for v in vazios[:3])}"
                          f"{'…' if len(vazios) > 3 else ''}\n")
                print(f"inspecionando: {nome}  ({tamanho / 1e6:.1f} MB)\n")
                tabela = candidata
                break
            if tabela is None:
                print(f"os {len(vazios)} menores arquivos estão vazios. "
                      f"Escolha um maior com --member (veja --list).", file=sys.stderr)
                return 1
    else:
        print(f"inspecionando: {alvo.name}\n")
        tabela = probe.read_parquet_file(alvo)

    print(probe.describe(tabela, sample_rows=args.rows))
    print(probe.diagnose(tabela))
    return 0


def cmd_load_x(args: argparse.Namespace) -> int:
    """Carrega a base histórica do X (parquet em zip).

    Cada arquivo vira um `collection_run` com kind='campanha' e o termo de busca
    como rótulo. Isso não é detalhe burocrático: a base foi coletada por termo em
    Trending Topics, em dias esparsos. Sem o rótulo, um pico de volume ficaria
    indistinguível de uma mudança na própria intensidade de coleta — e a análise
    temporal mentiria sem avisar.
    """
    from .sources.x_parquet import XParquetSource, members_of, parse_member_name

    alvo = Path(args.path)
    if not alvo.exists():
        print(f"não encontrei {alvo}", file=sys.stderr)
        return 1

    conn = db.connect(args.db)
    try:
        if db.current_version(conn) == 0:
            print("banco não migrado. Rode: nabote init", file=sys.stderr)
            return 1

        membros = members_of(alvo)
        if args.member:
            membros = [m for m in membros if args.member in m]
            if not membros:
                print(f"nenhum arquivo casa com {args.member!r}", file=sys.stderr)
                return 1

        # retomada: pula o que já foi carregado com sucesso
        feitos = {r["query"] for r in conn.execute(
            "SELECT query FROM collection_run WHERE source LIKE 'x_parquet:%' "
            "AND status = 'ok' AND query IS NOT NULL")}
        pendentes = [m for m in membros if m not in feitos]
        if feitos:
            print(f"{len(feitos)} arquivo(s) já carregado(s), pulando\n")
        if args.files:
            pendentes = pendentes[: args.files]
        if not pendentes:
            print("nada a carregar — tudo já está no banco.")
            return 0

        print(f"carregando {len(pendentes)} de {len(membros)} arquivos\n")
        total = ingest.Stats()
        for i, membro in enumerate(pendentes, 1):
            data, termo = parse_member_name(membro)
            fonte = XParquetSource(alvo, membro)
            run_id, st = ingest.ingest(
                conn, fonte, kind="campanha", campaign_label=termo or membro,
                author_tier=args.author_tier, resume=False)
            conn.execute("UPDATE collection_run SET query = ? WHERE run_id = ?",
                         (membro, run_id))
            for campo, valor in st.as_dict().items():
                setattr(total, campo, getattr(total, campo) + valor)
            print(f"  [{i:>3}/{len(pendentes)}] {data}  {(termo or '?')[:34]:<35} "
                  f"{st.posts_new:>7,} posts  {st.interactions_new:>7,} arestas"
                  .replace(",", "."))

        print("\ntotal")
        for campo, valor in total.as_dict().items():
            if valor:
                print(f"  {campo:<22} {valor:>10,}".replace(",", "."))
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
    fetch.add_argument("--global", dest="global_firehose", action="store_true",
                       help="consome o firehose inteiro, sem filtro de sementes")

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
    d.add_argument("--min-community", type=int, default=1, metavar="N",
                   help="esconde comunidades com menos de N atores; a cauda vira "
                        "uma linha de resumo (padrão: 1, mostra todas)")
    d.add_argument("--community", type=int, default=None, metavar="ID",
                   help="lista só os atores desta comunidade")

    lx = sub.add_parser("load-x", help="carrega base histórica do X (parquet em zip)")
    lx.add_argument("path", help="caminho do .zip")
    lx.add_argument("--files", type=int, default=None,
                    help="carrega só os N primeiros arquivos (comece pequeno)")
    lx.add_argument("--member", help="só arquivos cujo nome contenha este texto")
    lx.add_argument("--author-tier", default="C", choices=["A", "B", "C"],
                    help="tier dos autores; C é o certo aqui, porque a coleta foi "
                         "por termo e não por conta curada")

    insp = sub.add_parser("inspect", help="esquema e amostra de uma base externa")
    insp.add_argument("path", help="caminho de um .zip ou .parquet")
    insp.add_argument("--member", help="arquivo dentro do zip (padrão: o menor)")
    insp.add_argument("--list", action="store_true", help="só lista o conteúdo do zip")
    insp.add_argument("--rows", type=int, default=3, help="linhas de amostra")

    disc = sub.add_parser("discover", help="busca contas no Bluesky por nome de pessoa")
    disc.add_argument("--name", action="append", help="nome a buscar (repetível)")
    disc.add_argument("--file", help="arquivo com um nome por linha")
    disc.add_argument("--limit", type=int, default=5, help="candidatos por nome")

    seeds = sub.add_parser("seeds", help="registra ou lista a lista curada de perfis")
    seeds.add_argument("--file", help="arquivo com um handle ou DID por linha")
    seeds.add_argument("--tier", default="A", choices=["A", "B"])

    cycle = sub.add_parser("cycle", help="fetch + aggregate + analyze + dump")
    cycle.add_argument("--host", default=JetstreamDefaults.host)
    cycle.add_argument("--kind", default="baseline", choices=["baseline", "campanha"])
    cycle.add_argument("--campaign")
    cycle.add_argument("--max-events", type=int, default=None)
    cycle.add_argument("--max-seconds", type=float, default=180.0,
                       help="padrão 180s — um ciclo de coleta tem fim")
    cycle.add_argument("--view", default=graph.DEFAULT_VIEW, choices=sorted(graph.VIEWS))
    cycle.add_argument("--top", type=int, default=15)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fetch" and args.kind == "campanha" and not args.campaign:
        build_parser().error("--kind=campanha exige --campaign RÓTULO")
    return {"init": cmd_init, "status": cmd_status, "fetch": cmd_fetch,
            "aggregate": cmd_aggregate, "analyze": cmd_analyze,
            "dump": cmd_dump, "seeds": cmd_seeds, "discover": cmd_discover,
            "inspect": cmd_inspect, "load-x": cmd_load_x,
            "cycle": cmd_cycle}[args.command](args)


if __name__ == "__main__":
    # Encerramento direto. O pyarrow às vezes aborta na finalização do
    # interpretador ("terminate called without an active exception"), depois de
    # a saída já ter sido impressa — assustador e sem consequência. Nada aqui
    # depende de atexit: as conexões são fechadas em `finally`.
    _codigo = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_codigo)
