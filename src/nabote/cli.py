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


def cmd_runs(args: argparse.Namespace) -> int:
    """De onde veio cada aresta do grafo.

    Sem isto, o grafo é anônimo: dá para medir comunidade, centralidade e E-I
    sem nunca saber sobre O QUÊ as pessoas estavam falando. Coleta por termo faz
    do termo parte do resultado — um grafo montado com o termo "bbb" e um
    montado com "impeachment" não são a mesma rede vista duas vezes.
    """
    conn = db.connect(args.db)
    try:
        janela = graph.WINDOW_SQL.format(col="p.created_at")
        linhas = conn.execute(f"""
            SELECT r.run_id, r.campaign_label AS termo, r.query AS arquivo,
                   r.kind, r.status, r.items_fetched,
                   COUNT(p.post_id) AS posts,
                   MIN(substr(p.created_at,1,10)) AS de,
                   MAX(substr(p.created_at,1,10)) AS ate,
                   GROUP_CONCAT(DISTINCT {janela}) AS janelas
            FROM collection_run r
            LEFT JOIN post p ON p.run_id = r.run_id
            GROUP BY r.run_id ORDER BY r.run_id
        """).fetchall()
        if not linhas:
            print("nenhum run registrado.", file=sys.stderr)
            return 1

        print(f"{'#':>4} {'termo':<30}{'posts':>9}  {'período':<24}status")
        print("-" * 78)
        for r in linhas:
            periodo = f"{r['de']} … {r['ate']}" if r["de"] else "—"
            print(f"{r['run_id']:>4} {(r['termo'] or '?')[:29]:<30}{r['posts']:>9,}"
                  f"  {periodo:<24}{r['status']}".replace(",", "."))

        print("\npor janela — qual termo alimentou qual semana")
        por_janela: dict[str, list[tuple[str, int]]] = {}
        for r in linhas:
            for j in (r["janelas"] or "").split(","):
                if j:
                    por_janela.setdefault(j, []).append((r["termo"] or "?", r["posts"]))
        for j in sorted(por_janela):
            termos = sorted(por_janela[j], key=lambda t: -t[1])
            total = sum(n for _, n in termos)
            print(f"  {j}  {total:>9,} posts".replace(",", "."))
            for termo, n in termos[:args.top]:
                print(f"    {termo[:40]:<41}{n:>9,}".replace(",", "."))
            if len(termos) > args.top:
                print(f"    … + {len(termos) - args.top} outros termos")
        return 0
    finally:
        conn.close()


def cmd_themes(args: argparse.Namespace) -> int:
    """Do que cada comunidade estava falando.

    Nível 1 do plano: as pautas EMERGEM da estrutura, em vez de o tema ser
    escolhido antes e virar filtro. A comunidade é descoberta pelo grafo, sem
    olhar texto nenhum; só depois se pergunta sobre o que ela falava.

    Nesta base o rótulo sai de graça, porque a coleta foi por Trending Topic e
    o termo veio no nome do arquivo. Em produção o rótulo virá do passo 3
    (clustering de texto) — e estes termos servem de gabarito para conferir se
    aquele clustering acerta.

    O termo é atribuído pelos posts AUTORADOS na comunidade. Ator Tier C não
    escreveu nada na amostra, então não vota: ele é alvo, não voz.
    """
    conn = db.connect(args.db)
    try:
        windows = _windows(conn, args)
        if not windows:
            print("nada a mostrar.", file=sys.stderr)
            return 1
        scope = _escopo(args)

        for window in windows:
            todas = conn.execute(
                "SELECT community_id, size, ei_mean, ei_choice, choice_actors FROM community "
                "WHERE window_start=? AND scope=? ORDER BY size DESC",
                (window, scope)).fetchall()
            if not todas:
                print(f"{window}: sem comunidades para scope={scope}. "
                      f"Rode `analyze --view {args.view}`.", file=sys.stderr)
                continue
            # "não analisado" e "o filtro cortou tudo" são problemas diferentes,
            # e mandar rodar `analyze` de novo no segundo caso é conselho errado.
            comunidades = [c for c in todas
                           if c["size"] >= args.min_community][:args.top]
            if not comunidades:
                print(f"{window}: {len(todas)} comunidades, nenhuma com "
                      f"{args.min_community}+ atores (a maior tem "
                      f"{todas[0]['size']}). Baixe o --min-community.",
                      file=sys.stderr)
                continue

            janela_sql = graph.WINDOW_SQL.format(col="p.created_at")
            recorte = (f"ac.window_start=? AND ac.scope=? AND {janela_sql} = ?")
            valores = (window, scope, window)

            termos: dict[int, list[tuple[str, int]]] = {}
            for r in conn.execute(f"""
                SELECT ac.community_id AS com, cr.campaign_label AS termo,
                       COUNT(*) AS posts
                FROM actor_community ac
                JOIN post p ON p.actor_id = ac.actor_id
                JOIN collection_run cr ON cr.run_id = p.run_id
                WHERE {recorte}
                GROUP BY ac.community_id, cr.campaign_label
            """, valores):
                termos.setdefault(r["com"], []).append((r["termo"] or "?", r["posts"]))

            # Autores distintos PRECISA ser contado por comunidade, nunca somando
            # o distinto de cada termo: quem falou de dois assuntos seria contado
            # duas vezes, e o total passa do tamanho da comunidade.
            vozes = {r["com"]: r["autores"] for r in conn.execute(f"""
                SELECT ac.community_id AS com, COUNT(DISTINCT p.actor_id) AS autores
                FROM actor_community ac
                JOIN post p ON p.actor_id = ac.actor_id
                WHERE {recorte}
                GROUP BY ac.community_id
            """, valores)}

            print(f"\njanela {window}   visão {scope}")
            for c in comunidades:
                ei = f"{c['ei_mean']:+.2f}" if c["ei_mean"] is not None else "  -  "
                # O E-I cru anda junto com o tamanho; o "com escolha" é o
                # que se pode comparar entre comunidades.
                if c["ei_choice"] is not None:
                    ei = (f"{c['ei_choice']:+.2f} (escolha, n={c['choice_actors']})"
                          f"   cru {c['ei_mean']:+.2f}")
                lista = sorted(termos.get(c["community_id"], []), key=lambda t: -t[1])
                total = sum(n for _, n in lista)
                print(f"\n  #{c['community_id']:<4} {c['size']:>5} atores   "
                      f"E-I {ei}   {vozes.get(c['community_id'], 0)} com voz")
                if not total:
                    # Comunidade só de alvos: existe no grafo, não fala nele.
                    print("        (ninguém autorou post nesta janela)")
                    continue
                for termo, n in lista[:args.terms]:
                    print(f"        {termo[:34]:<35}{n:>7,}  {n / total:>5.0%}"
                          .replace(",", "."))
                if len(lista) > args.terms:
                    print(f"        … + {len(lista) - args.terms} outros termos")
        return 0
    finally:
        conn.close()


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


def _escopo(args: argparse.Namespace) -> str:
    """Escopo analítico pedido, na gramática do schema.

      <visão>                    grafo cheio
      <visão>:topic:<id>         recortado por tópico
      <visão>:core               só quem tem mais de uma aresta
      <visão>@<escopo>           partição importada daquele escopo
    """
    if getattr(args, "partition", None):
        return f"{args.view}@{args.partition}"
    scope = args.view if args.scope == "all" else f"{args.view}:{args.scope}"
    return scope + ":core" if getattr(args, "core", False) else scope


def _avisa_migracao(conn) -> None:
    """Schema velho faz comando novo mentir em silêncio. Avisa antes de rodar."""
    pendentes = db.pending_migrations(conn)
    if pendentes:
        nomes = ", ".join(n for _, n in pendentes)
        print(f"AVISO: {len(pendentes)} migração(ões) pendente(s): {nomes}\n"
              f"       Rode `nabote init` — sem isso as colunas novas não existem.",
              file=sys.stderr)


def cmd_analyze(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        _avisa_migracao(conn)
        windows = _windows(conn, args)
        if not windows:
            print("nenhuma interação no banco. Rode `fetch` antes.", file=sys.stderr)
            return 1
        for window in windows:
            r = graph.analyze_window(conn, window, view=args.view,
                                     edge_scope=args.scope, core=args.core,
                                     partition_scope=args.partition)
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

            # Quantos atores sustentam o E-I comparável. Comunidade onde quase
            # ninguém teve escolha tem E-I frágil, e isso tem de ficar visível.
            linha = conn.execute(
                "SELECT COUNT(*) n, SUM(choice_actors) atores, SUM(size) total "
                "FROM community WHERE window_start=? AND scope=? "
                "AND choice_actors > 0", (window, r["scope"])).fetchone()
            todos = conn.execute(
                "SELECT SUM(size) total FROM community "
                "WHERE window_start=? AND scope=?", (window, r["scope"])
            ).fetchone()["total"] or 1
            atores = linha["atores"] or 0
            # "amplificaram" seria impreciso: um hub tem muitas arestas de
            # ENTRADA e pode nunca ter amplificado ninguém. O que a força ≥ 2
            # garante é mais de uma aresta, logo mais de uma chance de
            # atravessar para outra comunidade.
            conc = r.get("concentracao") or {}
            if conc.get("top1", 0) >= graph.CONCENTRATION_ALERT:
                print(f"{'':12}  ATENÇÃO: 1 ator concentra {conc['top1']:.0%} do "
                      f"peso de saída ({conc['n']} maiores: {conc['topn']:.0%}). "
                      f"A métrica descreve essa conta, não a rede.")
            if r.get("sem_particao"):
                print(f"{'':12}  {r['sem_particao']} atores deste grafo não estão "
                      f"na partição importada e ficaram de fora")
            print(f"{'':12}  E-I com escolha: {atores} de {todos} atores "
                  f"({atores / todos:.0%}) têm mais de uma aresta, "
                  f"em {linha['n']} de {r['communities']} comunidades")
        return 0
    finally:
        conn.close()


# Faixas fixas de propósito: comparar a distribuição entre janelas só funciona
# se as faixas não mudarem junto com o dado.
_FAIXAS = ((500, "≥500"), (100, "100-499"), (10, "10-99"), (4, "4-9"), (0, "≤3"))


def _distribuicao(tamanhos: list[int]) -> str:
    """Histograma de tamanhos numa linha — responde 'isto é rede ou cacos?'.

    Contar comunidades não distingue vinte grupos grandes de mil díades. A
    distribuição distingue, e cabe numa linha.
    """
    if not tamanhos:
        return "nenhuma comunidade"
    contagem = dict.fromkeys((r for _, r in _FAIXAS), 0)
    for tamanho in tamanhos:
        for piso, rotulo in _FAIXAS:
            if tamanho >= piso:
                contagem[rotulo] += 1
                break
    return "distribuição  " + " · ".join(
        f"{rotulo}: {n}" for _, rotulo in _FAIXAS if (n := contagem[rotulo]))


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
        scope = _escopo(args)

        head = conn.execute(
            "SELECT COUNT(*) AS n FROM actor_metric WHERE window_start=? AND scope=?",
            (window, scope)).fetchone()["n"]
        if not head:
            print(f"janela {window} sem métricas para scope={scope}. "
                  f"Rode `analyze --view {args.view}`.", file=sys.stderr)
            return 1

        print(f"janela {window}   visão {scope}\n")

        recorte = "" if args.community is None else " AND c.community_id = :com"
        # PageRank é herdado: quem é repostado por um hub recebe quase todo o
        # rank dele. Num grafo fragmentado isso põe contas de in-degree 1 acima
        # de contas com dezenas de arestas. O filtro existe para a lista poder
        # ser lida como ranking.
        if args.min_degree:
            recorte += " AND m.actor_id IN (SELECT actor_id FROM actor_metric "
            recorte += ("WHERE window_start=:w AND scope=:s AND metric='in_degree_w' "
                        "AND value >= :grau)")
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
              "com": args.community, "grau": args.min_degree}).fetchall()

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
            "SELECT community_id, size, ei_mean, ei_choice, choice_actors FROM community "
            "WHERE window_start=? AND scope=? ORDER BY size DESC",
            (window, scope)).fetchall()
        tamanhos = [r["size"] for r in todas]

        # `--min-community` é pedido explícito: quem pede "todas acima de 50"
        # quer todas, não as 20 primeiras. `--top` só limita quando não há
        # corte — senão o filtro engana silenciosamente.
        if args.min_community > 1:
            mostradas = [r for r in todas if r["size"] >= args.min_community]
        else:
            mostradas = todas[:args.top]

        print(f"\ncomunidades  ({len(todas)} no total, {sum(tamanhos)} atores)")
        print("  " + _distribuicao(tamanhos))
        for r in mostradas:
            ei = f"{r['ei_mean']:+.2f}" if r["ei_mean"] is not None else "  -  "
            # O E-I cru anda junto com o tamanho e engana quem compara
            # direto; o "com escolha" é o comparável.
            escolha = ("  escolha " + f"{r['ei_choice']:+.2f}"
                       + f" (n={r['choice_actors']})"
                       if r["ei_choice"] is not None else "  escolha    —")
            nota = "  (E-I mecânico)" if r["size"] <= graph.TRIVIAL_COMPONENT else ""
            print(f"  #{r['community_id']:<4} {r['size']:>5} atores   "
                  f"cru {ei}{escolha}{nota}")

        cauda = todas[len(mostradas):]
        if cauda:
            restantes = [r["size"] for r in cauda]
            meio = sorted(restantes)[len(restantes) // 2]
            print(f"  … + {len(cauda)} comunidades restantes: "
                  f"{max(restantes)} a {min(restantes)} atores, mediana {meio}, "
                  f"{sum(restantes)} atores no total")
        if min(tamanhos, default=0) <= graph.TRIVIAL_COMPONENT:
            print(f"     comunidade de até {graph.TRIVIAL_COMPONENT} atores tem E-I "
                  f"−1,00 por construção: não existe aresta externa possível.")

        # A lista de arestas TEM de respeitar a visão. Mostrar uma citação sob
        # `--view amp` é mentira barata: o grafo analisado não a contém, e quem
        # lê o dump conclui coisa errada sobre o que produziu as comunidades.
        tipos = graph.VIEWS[args.view]
        marcadores = ",".join("?" * len(tipos))
        # Sob `--core`, a lista tem de mostrar arestas DO NÚCLEO. Senão o
        # cabeçalho anuncia um grafo e a lista exibe outro — inclusive atores
        # que foram podados. Quem está no núcleo é quem tem métrica no escopo.
        no_nucleo = ("" if not getattr(args, "core", False) else f"""
              AND e.src_actor_id IN (SELECT actor_id FROM actor_community
                                     WHERE window_start=? AND scope=?)
              AND e.dst_actor_id IN (SELECT actor_id FROM actor_community
                                     WHERE window_start=? AND scope=?)""")
        extra = (window, scope, window, scope) if no_nucleo else ()
        print(f"\narestas mais pesadas da visão {scope} "
              f"({' + '.join(tipos)})")
        for r in conn.execute(f"""
            SELECT s.handle AS sh, s.platform_user_id AS sd,
                   d.handle AS dh, d.platform_user_id AS dd, e.kind, e.weight
            FROM edge_window e
            JOIN actor s ON s.actor_id=e.src_actor_id
            JOIN actor d ON d.actor_id=e.dst_actor_id
            WHERE e.window_start=? AND e.scope=? AND e.kind IN ({marcadores})
                  {no_nucleo}
            ORDER BY e.weight DESC LIMIT ?
        """, (window, args.scope, *tipos, *extra, args.top)).fetchall():
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
                                  "view": args.view, "core": False,
                                  "partition": None}),
        ("dump", cmd_dump, {"window": None, "all": False, "scope": "all",
                            "min_community": 1, "community": None, "core": False,
                            "partition": None, "min_degree": 0.0,
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


def _no_intervalo(data: str | None, de: str | None, ate: str | None) -> bool:
    """Data do nome do arquivo dentro do intervalo, inclusive nas duas pontas.

    Arquivo sem data reconhecível fica de fora de qualquer recorte: incluí-lo
    seria admitir no banco algo cuja posição no tempo ninguém sabe.
    """
    if data is None:
        return False
    return (de is None or data >= de) and (ate is None or data <= ate)


def _cobertura(selecionados, pendentes, feitos, todos, parse) -> None:
    """Mostra a seleção por SEMANA e diz se cada uma está completa.

    Semana pela metade é o problema silencioso desta base: a análise agrega por
    janela semanal, então carregar quatro dos sete dias de uma semana produz uma
    janela cujo volume foi decidido pelo recorte, não pelo mundo. O número sai
    plausível e a série temporal mente.

    "Completa" aqui é relativa ao ZIP, não ao calendário: a coleta original foi
    em dias esparsos de Trending Topics, então uma semana pode legitimamente ter
    três dias. O que importa é não deixar de fora um dia que existe.
    """
    por_janela: dict[str, dict[str, set]] = {}
    for membro in todos:
        data, termo = parse(membro)
        if data is None:
            continue
        janela = graph.window_start_for(data)
        alvo = por_janela.setdefault(janela, {"zip": set(), "sel": set(),
                                              "termos": set()})
        alvo["zip"].add(data)
        if membro in selecionados:
            alvo["sel"].add(data)
            alvo["termos"].add(termo or "?")

    print(f"\n{'semana':<13}{'dias':>10}{'termos':>9}   situação")
    print("-" * 56)
    for janela in sorted(j for j, v in por_janela.items() if v["sel"]):
        v = por_janela[janela]
        faltam = v["zip"] - v["sel"]
        situacao = ("completa" if not faltam
                    else f"PARCIAL — faltam {len(faltam)} dia(s): "
                         + ", ".join(sorted(faltam)[:3]))
        print(f"{janela:<13}{len(v['sel']):>4}/{len(v['zip']):<5}"
              f"{len(v['termos']):>9}   {situacao}")

    nao_carregados = [m for m in selecionados if m not in pendentes and m not in feitos]
    if nao_carregados:
        print(f"\n{len(nao_carregados)} arquivo(s) da seleção ficaram de fora "
              f"por causa do --files")


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

        # Recorte por DATA, não por contagem. `--files N` pega um prefixo
        # cronológico e não tem como saber onde uma semana termina: corta no
        # meio, e a janela resultante tem volume decidido por onde a lista foi
        # truncada. Aí um pico de coleta vira indistinguível de um pico no
        # mundo, que é exatamente o que o schema foi feito para evitar.
        if args.date_from or args.date_to:
            antes = len(membros)
            membros = [m for m in membros
                       if _no_intervalo(parse_member_name(m)[0],
                                        args.date_from, args.date_to)]
            print(f"recorte por data: {len(membros)} de {antes} arquivos")
            if not membros:
                print("nenhum arquivo no intervalo pedido.", file=sys.stderr)
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

        _cobertura(membros, pendentes, feitos, members_of(alvo), parse_member_name)
        if args.dry_run:
            print("\n--dry-run: nada foi carregado.")
            return 0

        print(f"\ncarregando {len(pendentes)} de {len(membros)} arquivos\n")
        total = ingest.Stats()
        for i, membro in enumerate(pendentes, 1):
            data, termo = parse_member_name(membro)
            fonte = XParquetSource(alvo, membro)
            run_id, st = ingest.ingest(
                conn, fonte, kind="campanha", campaign_label=termo or membro,
                author_tier=args.author_tier, resume=False,
                store_raw=not args.no_raw)
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

    def _janela(p, com_view=False, com_core=False):
        p.add_argument("--window", help="janela YYYY-MM-DD (segunda-feira)")
        p.add_argument("--all", action="store_true", help="todas as janelas com dado")
        p.add_argument("--scope", default="all", help="'all' ou 'topic:<id>'")
        if com_view:
            p.add_argument("--view", default=graph.DEFAULT_VIEW, choices=sorted(graph.VIEWS),
                           help="amp = repost+citação (padrão) · reply = respostas")
        if com_core:
            p.add_argument("--partition", metavar="ESCOPO", default=None,
                           help="importa a partição deste escopo (ex.: amp:core) "
                                "em vez de detectar comunidade aqui. Grava em "
                                "<visão>@<escopo>. É a única forma correta de "
                                "comparar visões: o Leiden no grafo de respostas "
                                "inventa comunidades próprias, sem relação com "
                                "as do grafo de amplificação")
            p.add_argument("--core", action="store_true",
                           help="só atores com mais de uma aresta, podados até o "
                                "ponto fixo. Grava em <visão>:core, ao lado da "
                                "análise cheia — uma mede alcance, a outra "
                                "fechamento")
        return p

    th = _janela(sub.add_parser("themes", help="do que cada comunidade estava falando"),
                 com_view=True, com_core=True)
    th.add_argument("--top", type=int, default=20, help="comunidades (padrão: 20)")
    th.add_argument("--terms", type=int, default=5, help="termos por comunidade")
    th.add_argument("--min-community", type=int, default=50, metavar="N",
                    help="ignora comunidades com menos de N atores (padrão: 50)")

    runs = sub.add_parser("runs", help="de onde veio cada aresta: termo por janela")
    runs.add_argument("--top", type=int, default=10,
                      help="termos por janela (padrão: 10)")

    _janela(sub.add_parser("aggregate", help="interaction → edge_window"))
    _janela(sub.add_parser("analyze", help="grafo, comunidades e métricas"),
            com_view=True, com_core=True)
    d = _janela(sub.add_parser("dump", help="dump cru da janela, para depuração"),
                com_view=True, com_core=True)
    d.add_argument("--top", type=int, default=15)
    d.add_argument("--min-degree", type=float, default=0.0, metavar="G",
                   help="só atores com in-degree ponderado ≥ G. PageRank é "
                        "herdado — sem isto, conta com uma aresta só aparece "
                        "acima de conta com dezenas")
    d.add_argument("--min-community", type=int, default=1, metavar="N",
                   help="lista TODAS as comunidades com N atores ou mais, "
                        "ignorando --top; a cauda vira uma linha de resumo")
    d.add_argument("--community", type=int, default=None, metavar="ID",
                   help="lista só os atores desta comunidade")

    lx = sub.add_parser("load-x", help="carrega base histórica do X (parquet em zip)")
    lx.add_argument("path", help="caminho do .zip")
    lx.add_argument("--files", type=int, default=None,
                    help="carrega só os N primeiros arquivos (comece pequeno)")
    lx.add_argument("--member", help="só arquivos cujo nome contenha este texto")
    lx.add_argument("--from", dest="date_from", metavar="AAAA-MM-DD",
                    help="carrega só arquivos com data a partir desta (inclusive)")
    lx.add_argument("--to", dest="date_to", metavar="AAAA-MM-DD",
                    help="carrega só arquivos com data até esta (inclusive)")
    lx.add_argument("--dry-run", action="store_true",
                    help="mostra a cobertura por semana e não carrega nada")
    lx.add_argument("--no-raw", action="store_true",
                    help="não arquiva o payload cru. O arquivo existe porque "
                         "recoletar de uma API custa dinheiro; o zip já está no "
                         "seu disco, então guardar de novo só duplica gigabytes")
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
            "aggregate": cmd_aggregate, "analyze": cmd_analyze, "runs": cmd_runs,
            "themes": cmd_themes,
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
