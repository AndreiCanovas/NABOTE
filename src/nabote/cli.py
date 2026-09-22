"""CLI do instrumento.

Cada etapa do pipeline (frente 05) é um subcomando separado e retomável, para
que uma falha às 3 da manhã no `fetch` não obrigue a refazer o que já foi pago.
Neste passo só `init` e `status` existem; os demais entram na ordem da frente 05.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__, atproto, db, dossie, graph, identity, ingest, probe
from .sources import x_api


def carregar_env(caminho: Path = Path(".env")) -> dict[str, str]:
    """Lê o `.env` para dentro de `os.environ`, sem sobrescrever o que já veio.

    O runbook manda a chave para o `.env`; sem isto, `nabote fetch --source x`
    só funcionaria depois de um `source .env` que ninguém lembra de dar. A
    variável já exportada vence, porque quem exportou foi explícito.

    Sem dependência: dotenv resolveria aspas e multilinha, e nada aqui precisa
    disso — o arquivo tem chave, provedor e teto.
    """
    lidas: dict[str, str] = {}
    if not caminho.exists():
        return lidas
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        nome, _, valor = linha.partition("=")
        nome, valor = nome.strip(), valor.strip().strip('"').strip("'")
        if not nome:
            continue
        lidas[nome] = valor
        os.environ.setdefault(nome, valor)
    return lidas


def _fonte_x(conn, args):
    """Monta a fonte do X e imprime o que ela vai fazer ANTES de gastar.

    A conferência prévia existe porque a primeira requisição já custa: sem ela,
    descobrir que a lista de sementes está vazia ou que o teto é baixo demais
    custaria uma chamada e cinco segundos de espera cada vez.
    """
    from .sources import XApiSource

    carregar_env()
    chave = os.environ.get("NABOTE_X_API_KEY", "").strip()
    if not chave:
        print("NABOTE_X_API_KEY não está no ambiente nem no .env.\n"
              "Veja docs/x-api-setup.md, parte 2.", file=sys.stderr)
        return None

    teto = args.teto_usd
    if teto is None:
        try:
            teto = float(os.environ.get("NABOTE_BUDGET_USD_PER_CYCLE", ""))
        except ValueError:
            teto = None

    if args.query:
        fonte = XApiSource(chave, query=args.query, teto_usd=teto,
                           paginas_por_conta=args.paginas,
                           intervalo=args.intervalo)
        print(f"fonte    twitterapi_io · busca {args.query!r}")
        requisicoes = args.paginas
    else:
        contas = identity.seed_handles(conn, x_api.PLATFORM)
        if not contas:
            print("nenhuma semente de X registrada (actor.platform='x', tier A).\n"
                  "Registre as sementes antes de coletar.", file=sys.stderr)
            return None
        cursores = ingest.cursors_for(conn, XApiSource.name)
        fonte = XApiSource(chave, contas=contas, cursores=cursores, teto_usd=teto,
                           paginas_por_conta=args.paginas,
                           intervalo=args.intervalo)
        print(f"fonte    twitterapi_io · baseline")
        print(f"contas   {len(contas)} tier A · "
              f"{sum(1 for c in contas if c in cursores)} com cursor guardado")
        requisicoes = len(contas) * args.paginas

    # O saldo custa uma requisição e vale cada centavo dela: sem ele o aviso
    # prévio fala de tempo e de um teto que pode ser dez vezes o dinheiro que
    # existe na conta — que foi como a primeira coleta gastou três quartos do
    # crédito sem nada disparar.
    saldo = fonte.saldo()
    saldo_usd = saldo / x_api.CREDITOS_POR_USD
    estimado = x_api.estimativa_usd(requisicoes)
    teto = x_api.teto_efetivo(teto, saldo_usd)
    fonte.teto_usd = teto

    print(f"saldo    US$ {saldo_usd:.5f} · {saldo} créditos")
    print(f"custo    ~US$ {estimado:.5f} estimado · {requisicoes} páginas × "
          f"~{x_api.TWEETS_POR_PAGINA} tweets × US$ {x_api.CUSTO_POR_TWEET_USD}")
    print(f"teto     US$ {teto:.5f} (o menor entre o configurado e o saldo)")
    if estimado > saldo_usd:
        print(f"\n  ⚠  O PLANO NÃO CABE NO SALDO. Ele para no meio, em "
              f"~{int(saldo_usd / (x_api.TWEETS_POR_PAGINA * x_api.CUSTO_POR_TWEET_USD))}"
              f" de {requisicoes} páginas.\n"
              f"     Recarregue, ou reduza a coleta (menos sementes, "
              f"ou --paginas menor).\n")
    segundos = requisicoes * args.intervalo
    quanto = f"{segundos:.0f}s" if segundos < 90 else f"{segundos / 60:.0f} min"
    print(f"tempo    ~{quanto} · {requisicoes} requisições a "
          f"{args.intervalo:g}s cada (limite do tier gratuito)")
    return fonte


def _custo_ate_aqui(conn, source) -> None:
    """Quanto já foi gasto quando a coleta morre no meio.

    Falhar não devolve o dinheiro. Sem isto, um run interrompido deixaria
    `cost_usd` em zero e o custo acumulado do mês passaria a mentir.
    """
    if getattr(source, "saldo_inicial", None) is None:
        return
    print(f"gasto até aqui: US$ {source.gasto_usd:.5f}", file=sys.stderr)


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


# `fetch --source x` e `seeds --source x` já chamam o provedor de "x". O nome
# interno dele é outro, e a tradução mora aqui para que a pessoa nunca precise
# saber os dois.
FONTE_DO_APELIDO = {"x": "twitterapi_io", "bluesky": "bluesky_jetstream"}


def _corte_do_status(conn, *, source: str | None, platform: str | None):
    """(condição SQL sobre `post`, parâmetros, título, plataforma) do corte.

    A distinção que este comando errava: PLATAFORMA diz de que rede o dado é;
    FONTE diz por onde ele entrou. O arquivo de 2023 e a coleta por API são a
    mesma plataforma e fontes diferentes, então `platform='x'` responde
    "5,8 milhões de reposts" a uma pergunta sobre uma coleta de 529 posts.
    """
    if source:
        nome = FONTE_DO_APELIDO.get(source, source)
        return ("p.run_id IN (SELECT run_id FROM collection_run WHERE source = ?)",
                [nome], f"fonte {nome}", source)
    return ("p.platform = ?", [platform], f"plataforma {platform}", platform)


def _status_recortado(conn, *, source: str | None, platform: str | None) -> None:
    """O que UM corte do banco contém.

    Existe porque a contagem global não responde a pergunta: num banco com
    milhões de linhas do arquivo de 2023, as 529 que chegaram do X hoje somem
    no total — e é sobre as novas que se quer saber.
    """
    onde, args, titulo, rede = _corte_do_status(
        conn, source=source, platform=platform)

    def milhar(n) -> str:
        return f"{n:,}".replace(",", ".")

    def q(sql, *extra):
        """Roda a consulta com os parâmetros do corte JÁ na frente.

        A ordem não é escolha de estilo: o SQLite liga `?` por POSIÇÃO no
        texto, e `{onde}` aparece antes de qualquer `?` extra em todas as
        consultas daqui. Com os extras na frente — como esta função fazia — a
        troca não levanta erro nenhum: a consulta roda, compara as coisas
        erradas e devolve vazio ou zero, que é a forma mais cara de errar,
        porque parece um resultado.
        """
        return conn.execute(sql, (*args, *extra)).fetchall()

    ROTULO = {"A": "sementes, coletadas toda semana",
              "B": "coletadas todo mês",
              "C": "nunca coletadas — existem por serem alvo de aresta"}

    print(f"\n=== {titulo} ===")

    # Um corte por plataforma junta fontes; dizer quais é o que impede a
    # leitura errada que este comando já produziu uma vez.
    fontes = q(f"SELECT r.source, COUNT(*) n FROM post p "
               f"JOIN collection_run r ON r.run_id = p.run_id WHERE {onde} "
               f"GROUP BY r.source ORDER BY n DESC")
    if len(fontes) > 1:
        print("  ATENÇÃO: este corte junta mais de uma fonte —")
        for r in fontes:
            print(f"    {r['source']:<34} {milhar(r['n']):>9} posts")
        print("  Para ver só a coleta por API: nabote status --source x")

    print(f"\n=== atores ===")
    for r in q(f"""SELECT a.tier, COUNT(DISTINCT a.actor_id) n FROM actor a
                   WHERE a.actor_id IN (
                     SELECT p.actor_id FROM post p WHERE {onde}
                     UNION
                     SELECT i.dst_actor_id FROM interaction i
                     JOIN post p ON p.post_id = i.post_id WHERE {onde})
                   GROUP BY a.tier ORDER BY a.tier""", *args):
        print(f"  {r['tier']}  {milhar(r['n']):>9}  {ROTULO.get(r['tier'], '')}")

    print(f"\n=== posts ===")
    for r in q(f"SELECT p.post_type, COUNT(*) n FROM post p WHERE {onde} "
               f"GROUP BY p.post_type ORDER BY n DESC"):
        print(f"  {r['post_type']:<10} {milhar(r['n']):>11}")
    r = q(f"SELECT MIN(p.created_at) a, MAX(p.created_at) b FROM post p "
          f"WHERE {onde}")[0]
    if r["a"]:
        print(f"  período    {r['a'][:10]} a {r['b'][:10]}")
        # Mínimo e máximo sozinhos enganam: um tweet fixado de 2011 numa coleta
        # de timeline recente estica o período para quinze anos, e a linha passa
        # a descrever uma cobertura que não existe.
        antigos = q(f"SELECT COUNT(*) n FROM post p WHERE {onde} "
                    f"AND p.created_at < datetime(?, '-30 days')", r["b"])[0]["n"]
        if antigos:
            print(f"             {milhar(antigos)} post(s) fora dos últimos 30 dias "
                  f"— tweet fixado estica o mínimo, não é cobertura")

    # 71 respostas viraram 39 arestas na primeira coleta real, e "faltam 32"
    # tem duas explicações opostas. Separar as duas é a diferença entre um
    # comportamento correto e uma aresta perdida em silêncio.
    mudos = q(f"""SELECT p.post_type,
                         SUM(p.parent_actor_id = p.actor_id) thread,
                         SUM(p.parent_actor_id IS NULL)      sem_alvo
                  FROM post p
                  WHERE {onde} AND p.post_type <> 'original'
                    AND p.post_id NOT IN (SELECT post_id FROM interaction)
                  GROUP BY p.post_type ORDER BY p.post_type""")
    if mudos:
        print(f"\n=== posts que não viraram aresta ===")
        for r in mudos:
            partes = []
            if r["thread"]:
                partes.append(f"{milhar(r['thread'])} thread (resposta a si "
                              f"mesmo — não é interação)")
            if r["sem_alvo"]:
                partes.append(f"{milhar(r['sem_alvo'])} sem alvo no payload "
                              f"— ARESTA PERDIDA")
            print(f"  {r['post_type']:<10} {' · '.join(partes)}")

    print(f"\n=== arestas ===")
    for r in q(f"SELECT i.kind, COUNT(*) n FROM interaction i "
               f"JOIN post p ON p.post_id = i.post_id WHERE {onde} "
               f"GROUP BY i.kind ORDER BY n DESC"):
        print(f"  {r['kind']:<10} {milhar(r['n']):>11}")

    print(f"\n=== o que cada semente rendeu ===")
    # `conn.execute` direto e não o ajudante `q`: aqui o `?` do corte aparece
    # ANTES do `?` da plataforma no texto do SQL, e o ajudante põe os extras
    # na frente. Com a ordem trocada a consulta não dá erro — devolve vazio,
    # que é a forma mais cara de errar.
    for r in conn.execute(f"""SELECT a.handle,
                          COUNT(DISTINCT p.post_id)      posts,
                          COUNT(i.interaction_id)        arestas
                   FROM actor a
                   LEFT JOIN post p
                          ON p.actor_id = a.actor_id AND {onde}
                   LEFT JOIN interaction i ON i.post_id = p.post_id
                   WHERE a.tier = 'A' AND a.platform = ?
                   GROUP BY a.actor_id ORDER BY posts DESC, a.handle""",
                          (*args, rede)).fetchall():
        nada = ""
        if not r["posts"]:
            # "nada veio" é pergunta, não resposta. O perfil já foi pago no
            # registro: 12.000 tweets aponta para a chamada, 3 aponta para o
            # handle ter resolvido num homônimo morto.
            s = conn.execute(
                "SELECT s.posts_count, s.followers_count FROM actor_snapshot s "
                "JOIN actor a ON a.actor_id = s.actor_id "
                "WHERE a.handle = ? AND a.platform = ? "
                "ORDER BY s.snapshot_date DESC LIMIT 1", (r["handle"], rede)
            ).fetchone()
            if s and s["posts_count"] is not None:
                nada = (f"   ← nada veio · perfil diz {milhar(s['posts_count'])}"
                        f" tweets, {milhar(s['followers_count'] or 0)} seguidores")
            else:
                nada = "   ← nada veio · sem perfil guardado"
        print(f"  {(r['handle'] or '—'):<22} {r['posts']:>5} posts  "
              f"{r['arestas']:>5} arestas{nada}")

    print(f"\n=== quem mais recebeu, sem nunca ter sido coletado ===")
    for r in q(f"""SELECT d.handle, d.display_name, COUNT(*) n
                   FROM interaction i
                   JOIN post p ON p.post_id = i.post_id
                   JOIN actor d ON d.actor_id = i.dst_actor_id
                   WHERE d.tier = 'C' AND {onde}
                   GROUP BY d.actor_id ORDER BY n DESC LIMIT 15"""):
        print(f"  {milhar(r['n']):>9}  {(r['handle'] or '—'):<22} "
              f"{(r['display_name'] or '—')[:32]}")


def cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.db)
    if not path.exists():
        print(f"banco não existe em {path}. Rode: nabote init", file=sys.stderr)
        return 1

    conn = db.connect(path)
    try:
        if getattr(args, "source", None) or getattr(args, "platform", None):
            _status_recortado(conn, source=getattr(args, "source", None),
                              platform=getattr(args, "platform", None))
            return 0
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
        elif args.source == "x":
            source = _fonte_x(conn, args)
            if source is None:
                return 1
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
        if args.source != "x" or args.query:
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
        except x_api.ErroDoProvedor as erro:
            print(f"\nprovedor recusou: {erro}", file=sys.stderr)
            if erro.excesso:
                print("limite de taxa — suba o --intervalo (tier gratuito: 1 req/5s)",
                      file=sys.stderr)
            elif erro.sem_credito:
                print("crédito esgotado — recarregue no painel do provedor",
                      file=sys.stderr)
            _custo_ate_aqui(conn, source)
            return 1
        except OSError as erro:
            # urllib envolve falha de DNS, TLS e conexão em URLError(OSError).
            # Num coletor que cobra, a pergunta imediata é "fui cobrado?" — e a
            # resposta é o saldo, não o traceback.
            print(f"\nfalha de rede: {erro}", file=sys.stderr)
            _custo_ate_aqui(conn, source)
            return 1

        if getattr(source, "saldo_inicial", None) is not None:
            # atualiza o run com o custo MEDIDO — a diferença de dois saldos
            # lidos no provedor, não uma multiplicação de tabela de preço
            conn.execute("UPDATE collection_run SET cost_usd = ? WHERE run_id = ?",
                         (source.gasto_usd, run_id))
            print(f"custo    US$ {source.gasto_usd:.5f} · "
                  f"{source.saldo_inicial - source.saldo_atual} créditos"
                  f" · saldo {source.saldo_atual:,}".replace(",", "."))
            fora = source.frame.get("left_out") or []
            if fora:
                print(f"cortado  {len(fora)} contas ficaram de fora pelo teto: "
                      f"{', '.join(fora[:5])}{' …' if len(fora) > 5 else ''}")

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


def cmd_export(args: argparse.Namespace) -> int:
    """Passo 4: do banco para arquivos que outra pessoa consegue usar."""
    from . import export as exportador

    conn = db.connect(args.db)
    try:
        _avisa_migracao(conn)
        windows = _windows(conn, args)
        if not windows:
            print("nada a exportar.", file=sys.stderr)
            return 1
        scope = _escopo(args)
        destino_base = Path(args.out)

        for window in windows:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM actor_metric WHERE window_start=? AND scope=?",
                (window, scope)).fetchone()["n"]
            if not n:
                print(f"{window}: sem métricas para scope={scope}. "
                      f"Rode `analyze` antes.", file=sys.stderr)
                continue

            destino = destino_base / f"{window}_{scope.replace(':', '-').replace('@', '-em-')}"
            destino.mkdir(parents=True, exist_ok=True)

            contagens = {
                "actors": exportador.export_actors(conn, window, scope, destino),
                "communities": exportador.export_communities(
                    conn, window, scope, destino),
                "edges": exportador.export_edges(
                    conn, window, args.scope, destino,
                    kinds=list(graph.VIEWS[args.view]),
                    apenas_de=scope if ":core" in scope else None),
                "runs": exportador.export_runs(conn, window, destino),
            }
            manifesto = exportador.manifest(conn, window, scope, contagens)
            (destino / "manifest.json").write_text(
                json.dumps(manifesto, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")

            print(f"{destino}/")
            for nome, quantos in contagens.items():
                print(f"  {nome + '.csv':<20}{quantos:>8,} linhas".replace(",", "."))
            print(f"  {'manifest.json':<20}{len(manifesto['ressalvas']):>8} ressalvas")
        return 0
    finally:
        conn.close()


def _recorte(texto: str) -> tuple[str, str]:
    """'2023-01-23:amp:core' -> ('2023-01-23', 'amp:core').

    Corta no PRIMEIRO dois-pontos: o escopo tem os seus próprios (`amp:core`,
    `reply@amp:core`) e a janela nunca tem.
    """
    if ":" not in texto:
        raise ValueError(f"esperado JANELA:ESCOPO, veio {texto!r}")
    window, scope = texto.split(":", 1)
    return window, scope


def cmd_compare(args: argparse.Namespace) -> int:
    """Duas análises lado a lado, casadas por sobreposição de membros.

    Serve para as duas perguntas que a POC deixou em aberto:

      mesma janela, visões diferentes   promove os seus E discute com os outros?
      janelas diferentes, mesma visão   os campos são os MESMOS toda semana?

    Nunca casa por número de comunidade. A numeração é por tamanho dentro do
    recorte, então "#0" de uma semana não é "#0" da seguinte, e uma tabela
    alinhada por número sairia plausível e falsa.
    """
    conn = db.connect(args.db)
    try:
        (ja, ea), (jb, eb) = _recorte(args.a), _recorte(args.b)
        linhas = graph.match_communities(conn, ja, ea, jb, eb)
        if not linhas:
            print(f"nada em {args.a}. Rode `analyze` para esse escopo.",
                  file=sys.stderr)
            return 1

        def metricas(window: str, scope: str) -> dict[int, sqlite3.Row]:
            return {r["community_id"]: r for r in conn.execute(
                "SELECT community_id, size, ei_mean, ei_choice, choice_actors "
                "FROM community WHERE window_start=? AND scope=?", (window, scope))}

        ma, mb = metricas(ja, ea), metricas(jb, eb)
        print(f"A = {ja}  {ea}")
        print(f"B = {jb}  {eb}\n")
        print(f"{'A':>5}{'B':>7}{'comum':>10}{'do A':>7}{'do B':>7}{'jacc':>7}"
              f"{'atores A':>11}{'atores B':>11}{'E-I A':>9}{'E-I B':>9}{'salto':>9}")
        print("-" * 92)
        mostradas = [l for l in linhas[:args.top]
                     if l["n_a"] >= args.min_community]
        for linha in mostradas:
            ra = ma.get(linha["a"])
            rb = mb.get(linha["b"]) if linha["b"] is not None else None
            # `ei_choice` é o comparável; `ei_mean` anda junto do tamanho.
            va = ra["ei_choice"] if ra and ra["ei_choice"] is not None else None
            vb = rb["ei_choice"] if rb and rb["ei_choice"] is not None else None
            ruido = linha["share_a"] < graph.MATCH_MIN_SHARE
            if ruido:
                vb = None
            # Formatação em variáveis, não em f-string aninhada: a versão
            # aninhada deixava um float cru escapar no ramo do par fraco.
            alvo = "—" if linha["b"] is None or ruido else f"#{linha['b']}"
            txt_a = "—" if va is None else f"{va:+.2f}"
            txt_b = "—" if vb is None else f"{vb:+.2f}"
            salto = "—" if va is None or vb is None else f"{vb - va:+.2f}"
            n_b = 0 if ruido else linha["n_b"]
            print(f"  #{linha['a']:<3}{alvo:>7}{linha['comum']:>10,}"
                  f"{linha['share_a']:>7.0%}{linha['share_b']:>7.0%}"
                  f"{linha['jaccard']:>7.2f}{linha['n_a']:>11,}{n_b:>11,}"
                  f"{txt_a:>9}{txt_b:>9}{salto:>9}".replace(",", "."))

        ruidosos = [l for l in mostradas if l["share_a"] < graph.MATCH_MIN_SHARE]
        if ruidosos:
            print(f"\n{len(ruidosos)} comunidade(s) de A com par ruidoso "
                  f"(menos de {graph.MATCH_MIN_SHARE:.0%} dos membros de A "
                  f"foram parar nele).")
        print("\ncomum = atores nos dois · do A / do B = que fatia de cada lado "
              "eles são")
        print("'do B' 100% com 'do A' baixo é COBERTURA, não discordância: "
              "B é uma amostra de A.")
        return 0
    finally:
        conn.close()


def cmd_radar(args: argparse.Namespace) -> int:
    """Os números do Radar de Pautas de uma ou mais janelas.

    Sai em JSON de propósito: é o insumo de um relatório, não uma leitura de
    terminal, e reunir estes recortes à mão toda semana é como o relatório
    deixaria de ser recorrente.
    """
    from . import radar as radar_mod

    conn = db.connect(args.db)
    try:
        _avisa_migracao(conn)
        windows = _windows(conn, args)
        if not windows:
            print("nada a reportar.", file=sys.stderr)
            return 1

        base = args.base or f"{args.view}:core"
        saida = []
        for window in windows:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM community WHERE window_start=? AND scope=?",
                (window, base)).fetchone()["n"]
            if not n:
                print(f"{window}: sem análise em scope={base}. "
                      f"Rode `analyze --view {args.view} --core`.", file=sys.stderr)
                continue
            saida.append(radar_mod.snapshot(conn, window, view=args.view,
                                            base_scope=base))
        if not saida:
            return 1

        texto = json.dumps(saida, ensure_ascii=False,
                           indent=None if args.compact else 2)
        if args.out:
            Path(args.out).write_text(texto + "\n", encoding="utf-8")
            print(f"{args.out}  {len(saida)} janela(s), "
                  f"{sum(len(j['pautas']) for j in saida)} pautas")
        else:
            print(texto)
        return 0
    finally:
        conn.close()


def _escopo_de_pauta(args: argparse.Namespace) -> str | None:
    """`--topic X --topic Y` → escopo de arestas, ou None se não foi pedido.

    Existe para o usuário não ter de digitar o escopo composto à mão. Os
    rótulos reais da base têm `#` e espaço — `topic:#CPMIdoGolpe+CPMI+CPMI do
    Golpe` — e montar isso na linha de comando doze vezes é uma classe inteira
    de erro de digitação que o programa pode evitar.
    """
    topicos = getattr(args, "topic", None)
    return graph.topic_scope(topicos) if topicos else None


def cmd_dossie(args: argparse.Namespace) -> int:
    """Aprofundamento de UMA pauta, no escopo dedicado dela.

    Não roda sozinho: o radar decide onde aprofundar. A diferença prática em
    relação ao radar é o escopo — tudo aqui sai de `<visão>:topic:<pauta>`, um
    grafo só com as interações daquela coleta.

    A ordem importa e o comando a impõe: sem `aggregate --scope topic:<pauta>`
    e `analyze --edge-scope topic:<pauta>` antes, o escopo está vazio e o
    dossiê sairia com zeros em vez de erro.
    """
    from . import dossie as dossie_mod
    from . import positions as pos_mod

    conn = db.connect(args.db)
    try:
        _avisa_migracao(conn)
        windows = _windows(conn, args)
        if not windows:
            print("nada a reportar.", file=sys.stderr)
            return 1
        window = windows[-1]
        scope = dossie_mod.topic_scope(args.view, args.topic, core=not args.no_core)
        pauta = "+".join(sorted(args.topic))
        edge_scope = graph.edge_scope_of(scope)

        if not conn.execute(
            "SELECT 1 FROM edge_window WHERE window_start=? AND scope=? LIMIT 1",
            (window, edge_scope)).fetchone():
            print(f"{window}: escopo de arestas {edge_scope} vazio. "
                  f"Rode `aggregate --window {window} --scope {edge_scope}`.",
                  file=sys.stderr)
            return 1
        if not conn.execute(
            "SELECT 1 FROM community WHERE window_start=? AND scope=? LIMIT 1",
            (window, scope)).fetchone():
            print(f"{window}: sem análise em scope={scope}. Rode "
                  f"`analyze --window {window} --view {args.view} "
                  f"--scope {edge_scope}"
                  f"{'' if args.no_core else ' --core'}`.", file=sys.stderr)
            return 1

        anterior = None
        if not args.sem_delta:
            todas = graph.windows_present(conn)
            if window in todas and todas.index(window) > 0:
                candidata = todas[todas.index(window) - 1]
                if conn.execute(
                    "SELECT 1 FROM actor_position WHERE window_start=? AND scope=? LIMIT 1",
                    (candidata, scope)).fetchone():
                    anterior = candidata

        eixo = pos_mod.compute_positions(conn, window, scope, view=args.view)
        print(f"eixo: {eixo['atores']} atores posicionados sobre {eixo['alvos']} alvos "
              f"({eixo['descartados']} sem escolha ficaram de fora)", file=sys.stderr)
        for r in eixo["dimensoes"]:
            if not r["atores"]:
                continue
            conc = (f" · {r['concordancia']:.0%} de acerto vs. partição"
                    if r["concordancia"] is not None else "")
            print(f"      dim {r['dim']}: σ = {r['sigma1']:.4f} · "
                  f"{r['fatia_no_meio']:.1%} entre −0,25 e +0,25{conc} · "
                  f"{r['iteracoes']} iter{'' if r['convergiu'] else ' NÃO CONVERGIU'}",
                  file=sys.stderr)
        if eixo["degenerada"]:
            print(f"      AVISO: a dimensão 1 degenerou em indicador de bloco — "
                  f"reproduz a partição e não é eixo de posição. Use a dimensão 2.",
                  file=sys.stderr)
        print(f"      [σ é a correlação entre a posição de quem amplifica e a de "
              f"quem é amplificado. A 'fatia da inércia' não é interpretável em "
              f"tabela esparsa.]", file=sys.stderr)
        print(f"      cobertura: {eixo['amplificados']} amplificados + "
              f"{eixo['amplificadores']} amplificadores · {eixo['com_comunidade']} "
              f"dos {eixo['atores']} têm comunidade no escopo "
              f"(a partição tem {eixo['atores_na_particao']})", file=sys.stderr)
        if not eixo["convergiu"] and eixo["atores"]:
            resto = (f"resíduo {eixo['residuo']:.2e}, tolerância {pos_mod.TOL:.0e}"
                     if eixo["residuo"] is not None else "sem solução")
            print(f"      AVISO: não convergiu em {eixo['iteracoes']} iterações "
                  f"({resto}). O eixo desta janela NÃO é comparável com o de outra.",
                  file=sys.stderr)
        if eixo.get("fatia_no_meio", 1.0) < 0.05:
            print(f"      nota: só {eixo['fatia_no_meio']:.1%} dos atores caem entre "
                  f"−0,25 e +0,25 — o eixo separa dois blocos, não é um contínuo.",
                  file=sys.stderr)

        saida = dossie_mod.snapshot(conn, window, args.topic, view=args.view,
                                    core=not args.no_core, top=args.top,
                                    mapa_top=args.mapa, subpautas=args.subpautas,
                                    janela_anterior=anterior)
        sub = saida["subpautas"]
        if not sub["posts_com_texto"]:
            print(f"aviso: 0 de {sub['posts']} posts têm texto no banco — a seção "
                  f"de sub-pautas sai vazia.", file=sys.stderr)
        else:
            print(f"sub-pautas: {sub['textos_distintos']} textos distintos em "
                  f"{sub['posts_com_texto']} posts "
                  f"({1 - sub['textos_distintos'] / sub['posts_com_texto']:.0%} são "
                  f"repetição de texto já visto) → {len(sub['linhas'])} termos",
                  file=sys.stderr)
        mapa = saida["mapa"]
        if mapa["nos"]:
            print(f"mapa: {len(mapa['nos'])} de {mapa['de']} atores · "
                  f"{mapa['ligacoes_totais']} ligações por audiência, "
                  f"{len(mapa['arestas'])} no esqueleto (alfa {mapa['alfa']}) · "
                  f"apenas {mapa['arestas_diretas']} arestas diretas de amplificação "
                  f"entre eles (peso {mapa['peso_direto']:.0f})", file=sys.stderr)
        nomeadas = saida["comunidades_nomeadas"]
        sem_nome = [c for c in nomeadas.values() if not c["nome"]]
        print(f"nomes: {len(nomeadas) - len(sem_nome)} de {len(nomeadas)} comunidades "
              f"nomeadas pelos termos distintivos", file=sys.stderr)
        for c in sorted(nomeadas.values(), key=lambda v: -v["atores"])[:8]:
            perfis = ", ".join("@" + p for p in c["perfis"][:3])
            print(f"      #{c['id']:<3} {c['atores']:>6} atores  "
                  f"{(c['nome'] or '(sem termo distintivo)'):<38}  {perfis}",
                  file=sys.stderr)
        print("      o termo diz o que a comunidade FALA; os perfis dizem quem ela É. "
              "Podem divergir — troque com `label --set`.", file=sys.stderr)
        if mapa["nos"] and mapa["soltos"]:
            print(f"      {len(mapa['soltos'])} perfil(is) sem ligação forte de "
                  f"audiência no mapa (grau médio {mapa['grau_medio']:.1f})",
                  file=sys.stderr)
        co = saida["coamplificacao"]
        print(f"coamplificação: {co['grupos'] - co['grupos_ignorados']} de "
              f"{co['grupos']} alvos considerados "
              f"({co['grupos_ignorados']} acima de {co['grupo_max']} amplificações "
              f"ficaram de fora por viralidade) → {len(co['clusters'])} cluster(s)",
              file=sys.stderr)

        texto = json.dumps(saida, ensure_ascii=False,
                           indent=None if args.compact else 2)
        if args.out:
            Path(args.out).write_text(texto + "\n", encoding="utf-8")
            print(f"{args.out}  pauta {pauta}  {len(saida['atores'])} atores, "
                  f"{len(saida['comunidades'])} comunidades, "
                  f"{len(sub['linhas'])} sub-pautas")
        else:
            print(texto)
        return 0
    finally:
        conn.close()


def cmd_label(args: argparse.Namespace) -> int:
    """Troca o nome proposto de uma comunidade pelo nome do analista.

    A nomeação automática é ponto de partida: "garimpo · ilegal" descreve, mas
    quem conhece o assunto escreve "Crime ambiental". A evidência que justificou
    o rótulo continua saindo do dado a cada execução, então a troca não apaga a
    procedência — só melhora a leitura.
    """
    from . import dossie as dossie_mod

    conn = db.connect(args.db)
    try:
        if not args.set:
            atuais = dossie_mod.labels_of(conn, args.window, args.scope)
            if not atuais:
                print(f"nenhuma comunidade nomeada em {args.window} {args.scope}.",
                      file=sys.stderr)
                return 1
            for cid in sorted(atuais):
                print(f"  #{cid:<4} {atuais[cid]}")
            return 0
        trocados = 0
        for par in args.set:
            if "=" not in par:
                print(f"formato esperado ID=NOME, recebi {par!r}", file=sys.stderr)
                return 2
            cid, nome = par.split("=", 1)
            if not dossie_mod.override_label(conn, args.window, args.scope,
                                             int(cid), nome.strip()):
                print(f"comunidade #{cid} não existe em {args.window} {args.scope}",
                      file=sys.stderr)
                return 1
            print(f"  #{cid} → {nome.strip()}")
            trocados += 1
        print(f"{trocados} nome(s) trocado(s).", file=sys.stderr)
        return 0
    finally:
        conn.close()


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
            escopo_pedido = _escopo_de_pauta(args) or args.scope
            n = graph.aggregate_window(conn, window, scope=escopo_pedido)
            print(f"{window}  {n:>7,} arestas agregadas  (scope={escopo_pedido})"
                  .replace(",", "."))
            if not n and escopo_pedido != "all":
                print(f"{'':12}  a pauta não aparece nesta janela", file=sys.stderr)

            if not args.by_topic:
                continue
            # Um escopo por pauta, ao lado do escopo cheio. É o que permite
            # perguntar "quem é central NESTA pauta" em vez de "no grafo".
            for topico in graph.topics_in_window(conn, window):
                escopo = graph.topic_scope([topico])
                n = graph.aggregate_window(conn, window, scope=escopo)
                print(f"{'':12}  {n:>7,} arestas  {escopo}".replace(",", "."))
        return 0
    finally:
        conn.close()


def _particao(valor: str | None) -> tuple[str | None, str | None]:
    """'amp:core' -> (None, 'amp:core') · '2023-01-16:amp:core' -> (janela, escopo).

    Distinguir os dois pela forma: escopo nunca começa com uma data.
    """
    if not valor:
        return None, None
    inicio, _, resto = valor.partition(":")
    parece_data = len(inicio) == 10 and inicio[4] == "-" and inicio[7] == "-"
    if parece_data and not resto:
        # Uma data sozinha não é partição nenhuma: falta dizer QUAL análise
        # daquela semana. Melhor recusar que adivinhar.
        raise ValueError(
            f"--partition {valor!r} não diz o escopo. "
            f"Use {valor}:amp:core, ou só amp:core para esta mesma janela.")
    return (inicio, resto) if parece_data else (None, valor)


def _escopo(args: argparse.Namespace) -> str:
    """Escopo analítico pedido, na gramática do schema.

      <visão>                    grafo cheio
      <visão>:topic:<id>         recortado por tópico
      <visão>:core               só quem tem mais de uma aresta
      <visão>@<escopo>           partição importada daquele escopo
    """
    if getattr(args, "partition", None):
        return f"{args.view}@{args.partition}"  # já inclui a janela, se houver
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
    try:
        particao = _particao(args.partition)
    except ValueError as erro:
        print(erro, file=sys.stderr)
        return 1

    conn = db.connect(args.db)
    try:
        _avisa_migracao(conn)
        windows = _windows(conn, args)
        if not windows:
            print("nenhuma interação no banco. Rode `fetch` antes.", file=sys.stderr)
            return 1
        for window in windows:
            pj, pe = particao
            r = graph.analyze_window(conn, window, view=args.view,
                                     edge_scope=_escopo_de_pauta(args) or args.scope,
                                     core=args.core,
                                     partition_scope=pe, partition_window=pj)
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

            # Um grafo por pauta ao lado do grafo da janela. Sem isto o Radar
            # responde "quem é central no grafo" quando a pergunta é "quem é
            # central NESTA pauta" — e as duas respostas são diferentes. O
            # escopo sai sem `:core` de propósito: é o escopo que
            # `radar.topic_actors` procura.
            if getattr(args, "by_topic", False):
                for topico in graph.topics_in_window(conn, window):
                    rt = graph.analyze_window(conn, window, view=args.view,
                                              edge_scope=graph.topic_scope([topico]))
                    if not rt["nodes"]:
                        continue
                    print(f"{'':12}  {rt['nodes']:>6} nós  {rt['edges']:>7} arestas  "
                          f"{rt['communities']:>4} com.  {rt['scope']}")

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
        # que foram podados.
        #
        # O filtro é feito em Python, de propósito. Entregá-lo ao planejador
        # como duas subconsultas `IN` fez o SQLite escolher produto cartesiano
        # sobre o índice único: 72 mil origens × 72 mil destinos × 2 tipos, dez
        # bilhões de sondagens, comando pendurado. Lendo em ordem de peso pelo
        # índice e parando no teto, o custo é proporcional ao que se mostra.
        nucleo = None
        if getattr(args, "core", False):
            nucleo = {r["actor_id"] for r in conn.execute(
                "SELECT actor_id FROM actor_community WHERE window_start=? "
                "AND scope=?", (window, scope))}

        print(f"\narestas mais pesadas da visão {scope} "
              f"({' + '.join(tipos)})")
        cursor = conn.execute(f"""
            SELECT e.src_actor_id AS si, e.dst_actor_id AS di,
                   s.handle AS sh, s.platform_user_id AS sd,
                   d.handle AS dh, d.platform_user_id AS dd, e.kind, e.weight
            FROM edge_window e
            JOIN actor s ON s.actor_id=e.src_actor_id
            JOIN actor d ON d.actor_id=e.dst_actor_id
            WHERE e.window_start=? AND e.scope=? AND e.kind IN ({marcadores})
            ORDER BY e.weight DESC
        """, (window, args.scope, *tipos))
        mostradas = 0
        for r in cursor:
            if nucleo is not None and (r["si"] not in nucleo or r["di"] not in nucleo):
                continue
            print(f"  {(r['sh'] or r['sd'])[:26]:<27} -{r['kind']:>8}-> "
                  f"{(r['dh'] or r['dd'])[:26]:<27} {r['weight']:.0f}")
            mostradas += 1
            if mostradas >= args.top:
                break

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
            linhas = conn.execute(
                "SELECT platform, handle, platform_user_id, tier FROM actor "
                "WHERE tier IN ('A','B') ORDER BY platform, tier, handle, "
                "platform_user_id").fetchall()
            if not linhas:
                print("nenhuma semente registrada. Use: nabote seeds --file lista.txt")
                return 0
            print(f"{len(linhas)} sementes registradas\n")
            for r in linhas:
                print(f"  {r['platform']:<8} {r['tier']}  "
                      f"{(r['handle'] or '—'):<34} {r['platform_user_id']}")
            return 0

        entries = identity.parse_seed_file(Path(args.file).read_text(encoding="utf-8"))
        if not entries:
            print(f"{args.file} não tem nenhuma entrada útil.", file=sys.stderr)
            return 1

        if args.source == "x":
            carregar_env()
            chave = os.environ.get("NABOTE_X_API_KEY", "").strip()
            if not chave:
                print("NABOTE_X_API_KEY não está no ambiente nem no .env.\n"
                      "Veja docs/x-api-setup.md, parte 2.", file=sys.stderr)
                return 1
            # custa uma requisição por entrada, sob o mesmo 1 req/5 s da coleta
            segundos = len(entries) * x_api.INTERVALO_PADRAO
            quanto = f"{segundos:.0f}s" if segundos < 90 else f"{segundos / 60:.0f} min"
            print(f"resolvendo {len(entries)} handles no twitterapi.io\n"
                  f"custa uma requisição cada · ~{quanto} a "
                  f"{x_api.INTERVALO_PADRAO:g}s por requisição\n")
            transporte = x_api.Transporte(chave)
            antes = transporte.saldo()
            ok, falhas = identity.register_seeds_x(
                conn, entries, transporte, tier=args.tier)
        else:
            print(f"resolvendo {len(entries)} entradas...\n")
            antes = None
            ok, falhas = identity.register_seeds(conn, entries, tier=args.tier)
        for nome, did in ok:
            print(f"  ok      {nome:<34} {did}")
        for nome, motivo in falhas:
            print(f"  FALHOU  {nome:<34} {motivo}", file=sys.stderr)

        print(f"\n{len(ok)} registradas como tier {args.tier}"
              + (f", {len(falhas)} falharam" if falhas else ""))
        if antes is not None:
            gasto = antes - transporte.saldo()
            print(f"custo    US$ {gasto / x_api.CREDITOS_POR_USD:.5f} · "
                  f"{gasto} créditos")
        if falhas and not ok:
            return 1
        plat = x_api.PLATFORM if args.source == "x" else atproto.PLATFORM
        print(f"total de sementes de {plat} no banco: "
              f"{len(identity.seed_uids(conn, plat))}")
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
        ("aggregate", cmd_aggregate, {"window": None, "all": True, "scope": "all",
                                      "by_topic": False}),
        ("analyze", cmd_analyze, {"window": None, "all": True, "scope": "all",
                                  "view": args.view, "core": False,
                                  "partition": None, "by_topic": False}),
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


def _arquivo_de_entrada(caminho: str, sufixos: tuple[str, ...]) -> Path | None:
    """Valida o caminho ANTES de entregá-lo a uma biblioteca.

    `exists()` sozinho não basta: diretório existe. Uma variável de shell vazia
    vira `.`, passa no teste e explode lá dentro com traceback — que é o que
    aconteceu. Erro de digitação do usuário merece uma frase, não uma pilha de
    chamadas do zipfile.
    """
    if not caminho:
        print("caminho vazio. A variável do shell foi perdida? "
              "Redefina e tente de novo.", file=sys.stderr)
        return None
    alvo = Path(caminho).expanduser()
    if not alvo.exists():
        print(f"não encontrei {alvo}", file=sys.stderr)
        return None
    if alvo.is_dir():
        print(f"{alvo} é um diretório; esperava um arquivo "
              f"{' ou '.join(sufixos)}.", file=sys.stderr)
        return None
    if alvo.suffix not in sufixos:
        print(f"{alvo} não tem extensão {' nem '.join(sufixos)}.", file=sys.stderr)
        return None
    return alvo


def cmd_candidatos(args: argparse.Namespace) -> int:
    """Sugere sementes a partir do que a base já mediu. A escolha é sua."""
    conn = db.connect(args.db)
    try:
        _avisa_migracao(conn)
        ja = []
        if args.excluir:
            ja = identity.parse_seed_file(
                Path(args.excluir).read_text(encoding="utf-8"))

        print("varrendo interaction — leva alguns segundos numa base grande\n")
        linhas = identity.candidatos_a_semente(
            conn, args.platform, limite=args.top, desde=args.desde, excluir=ja)
        if not linhas:
            print("nenhum candidato. A base tem interações desta plataforma?",
                  file=sys.stderr)
            return 1

        print(f"{'handle':<24} {'alvos':>7} {'inter.':>8} {'sem.':>5} "
              f"{'recebe':>8} {'posts':>7}  razão")
        print("-" * 76)
        for r in linhas:
            print(f"{r['handle'][:24]:<24} {r['alvos']:>7,} {r['interacoes']:>8,} "
                  f"{r['semanas']:>5} {r['entradas']:>8,} {r['posts']:>7,}  "
                  f"{r['razao']}".replace(",", "."))

        print(f"\n{len(linhas)} candidatos" +
              (f", fora os {len(ja)} que já estão em {args.excluir}" if ja else ""))
        print("\nALVOS é a coluna que ordena: quantas contas DIFERENTES o perfil\n"
              "amplificou. Quinhentos retuítes na mesma conta são uma aresta de\n"
              "peso 500; duzentos em cento e cinquenta contas são 150 arestas.\n"
              "\nfábrica  produz aresta — só a coleta traz, é o que se paga\n"
              "voz      chega de graça como Tier C, mas o texto dela não\n"
              "ambos    as duas coisas\n"
              "pouco    nem uma nem outra — volume concentrado em poucas contas\n"
              "\nO arquivo é de 2023: conferir antes de adotar.")
        return 0
    finally:
        conn.close()


def cmd_inspect(args: argparse.Namespace) -> int:
    """Mostra esquema e amostra de uma base externa, antes de escrever adaptador."""
    alvo = _arquivo_de_entrada(args.path, (".zip", ".parquet"))
    if alvo is None:
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


def _semanas_vazias(janelas: list[str]) -> list[str]:
    """Segundas-feiras sem dado nenhum entre a primeira e a última com dado."""
    if len(janelas) < 2:
        return []
    from datetime import date, timedelta

    presentes = set(janelas)
    inicio = date.fromisoformat(janelas[0])
    fim = date.fromisoformat(janelas[-1])
    faltando = []
    atual = inicio + timedelta(days=7)
    while atual < fim:
        if atual.isoformat() not in presentes:
            faltando.append(atual.isoformat())
        atual += timedelta(days=7)
    return faltando


def _no_intervalo(data: str | None, de: str | None, ate: str | None) -> bool:
    """Data do nome do arquivo dentro do intervalo, inclusive nas duas pontas.

    Arquivo sem data reconhecível fica de fora de qualquer recorte: incluí-lo
    seria admitir no banco algo cuja posição no tempo ninguém sabe.
    """
    if data is None:
        return False
    return (de is None or data >= de) and (ate is None or data <= ate)


def _cobertura(selecionados, pendentes, feitos, todos, parse,
               recortado: bool = True) -> None:
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
    print("-" * 60)
    com_selecao = sorted(j for j, v in por_janela.items() if v["sel"])
    for janela in com_selecao:
        v = por_janela[janela]
        faltam = v["zip"] - v["sel"]
        if not recortado:
            # Sem recorte, TODA semana está "completa" por definição, e dizer
            # isso é tautologia disfarçada de aprovação. A coluna só informa
            # quando existe seleção para comparar.
            situacao = "(sem recorte: tudo)"
        elif faltam:
            situacao = (f"PARCIAL — faltam {len(faltam)} dia(s): "
                        + ", ".join(sorted(faltam)[:3]))
        else:
            situacao = "completa"
        print(f"{janela:<13}{len(v['sel']):>4}/{len(v['zip']):<5}"
              f"{len(v['termos']):>9}   {situacao}")

    # Buraco entre semanas é informação de primeira ordem: série temporal que
    # atravessa um vazio compara os dois lados dele como se fossem contíguos.
    vazias = _semanas_vazias(sorted(por_janela))
    if vazias:
        print(f"\n{len(vazias)} semana(s) SEM DADO no zip, entre a primeira e a "
              f"última: {', '.join(vazias[:6])}"
              + (f" … e mais {len(vazias) - 6}" if len(vazias) > 6 else ""))
        print("     série temporal que atravessa um vazio desses compara os dois "
              "lados como se fossem contíguos.")

    dias = sum(len(v["sel"]) for v in por_janela.values())
    print(f"\n{dias} dia(s) de coleta em {len(com_selecao)} semana(s)"
          + (f", cobrindo {len(por_janela)} semanas de calendário"
             if len(por_janela) != len(com_selecao) else ""))

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

    alvo = _arquivo_de_entrada(args.path, (".zip",))
    if alvo is None:
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

        _cobertura(membros, pendentes, feitos, members_of(alvo), parse_member_name,
                   recortado=bool(args.date_from or args.date_to or args.member))
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
    status = sub.add_parser(
        "status", help="mostra versão do schema, volume e custo acumulado")
    status.add_argument("--source", choices=["x", "bluesky"],
                        help="detalha o que UMA FONTE trouxe — 'x' é a coleta "
                             "por API, sem o arquivo de 2023 junto")
    status.add_argument("--platform", choices=["x", "bluesky"],
                        help="detalha tudo o que existe de UMA REDE, arquivo "
                             "histórico incluído; avisa quando junta fontes")

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
    fetch.add_argument("--source", default="bluesky", choices=["bluesky", "x"],
                       help="de onde coletar (padrão: bluesky, que é gratuito)")
    fetch.add_argument("--query", help="--source x: busca por termo em vez de "
                       "timeline das sementes; exige --kind campanha")
    fetch.add_argument("--teto-usd", dest="teto_usd", type=float, default=None,
                       help="--source x: teto de gasto do ciclo "
                            "(padrão: NABOTE_BUDGET_USD_PER_CYCLE do .env)")
    fetch.add_argument("--paginas", type=int, default=1,
                       help="--source x: páginas por conta (padrão: 1)")
    fetch.add_argument("--intervalo", type=float, default=x_api.INTERVALO_PADRAO,
                       help=f"--source x: segundos entre requisições "
                            f"(padrão: {x_api.INTERVALO_PADRAO}, limite do tier gratuito)")

    def _janela(p, com_view=False, com_core=False):
        p.add_argument("--window", help="janela YYYY-MM-DD (segunda-feira)")
        p.add_argument("--all", action="store_true", help="todas as janelas com dado")
        p.add_argument("--scope", default="all", help="'all' ou 'topic:<id>'")
        if com_view:
            p.add_argument("--view", default=graph.DEFAULT_VIEW, choices=sorted(graph.VIEWS),
                           help="amp = repost+citação (padrão) · reply = respostas")
        if com_core:
            p.add_argument("--partition", metavar="ESCOPO", default=None,
                           help="importa a partição deste escopo (ex.: amp:core, "
                                "ou 2023-01-16:amp:core para outra semana) "
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

    ex = _janela(sub.add_parser("export", help="passo 4: CSVs e manifesto"),
                 com_view=True, com_core=True)
    ex.add_argument("--out", default="export", metavar="DIR",
                    help="diretório de saída (padrão: export/)")

    rd = _janela(sub.add_parser("radar", help="números do Radar de Pautas, em JSON"),
                 com_view=True)
    rd.add_argument("--base", metavar="ESCOPO",
                    help="escopo das comunidades de referência (padrão: <visão>:core)")
    rd.add_argument("--out", metavar="ARQUIVO", help="grava em arquivo em vez da tela")
    rd.add_argument("--compact", action="store_true", help="JSON numa linha só")

    ds = _janela(sub.add_parser("dossie", help="aprofundamento de uma pauta, em JSON"),
                 com_view=True)
    ds.add_argument("--topic", required=True, metavar="PAUTA", action="append",
                    help="rótulo da pauta, como aparece em `runs`. Pode repetir: "
                         "a coleta por Trending Topic parte a mesma pauta em "
                         "etiquetas diferentes (CPMI e #CPMIdoGolpe), e juntá-las "
                         "é o que impede o grafo de ser dividido por acidente")
    ds.add_argument("--top", type=int, default=12, metavar="N",
                    help="atores na tabela de influência (padrão: 12)")
    ds.add_argument("--mapa", type=int, default=60, metavar="N",
                    help="nós no mapa da rede (padrão: 60)")
    ds.add_argument("--subpautas", type=int, default=dossie.SUBPAUTAS_POR_COMUNIDADE,
                    metavar="N",
                    help=f"termos distintivos por comunidade na tabela de "
                         f"sub-pautas (padrão: {dossie.SUBPAUTAS_POR_COMUNIDADE}). "
                         f"Alargar serve para seguir UM termo entre janelas: com "
                         f"o padrão, um termo que caia do top 3 numa semana some "
                         f"da série sem distinguir 'zero' de 'abaixo do corte'")
    ds.add_argument("--no-core", action="store_true",
                    help="usa o grafo cheio da pauta em vez do núcleo")
    ds.add_argument("--sem-delta", action="store_true",
                    help="não procura a janela anterior para o Δ do eixo")
    ds.add_argument("--out", metavar="ARQUIVO", help="grava em arquivo em vez da tela")
    ds.add_argument("--compact", action="store_true", help="JSON numa linha só")

    lb = sub.add_parser("label", help="nomeia comunidades de um escopo")
    lb.add_argument("--window", required=True, metavar="JANELA")
    lb.add_argument("--scope", required=True, metavar="ESCOPO",
                    help="escopo analítico, ex. amp:topic:CPMI:core")
    lb.add_argument("--set", action="append", metavar="ID=NOME",
                    help="troca o nome de uma comunidade. Pode repetir. "
                         "Sem --set, lista os nomes atuais")

    cp = sub.add_parser("compare", help="duas análises lado a lado")
    cp.add_argument("a", metavar="JANELA:ESCOPO",
                    help="ex.: 2023-01-23:amp:core")
    cp.add_argument("b", metavar="JANELA:ESCOPO",
                    help="ex.: 2023-01-23:reply@amp:core")
    cp.add_argument("--top", type=int, default=20)
    cp.add_argument("--min-community", type=int, default=1, metavar="N")

    runs = sub.add_parser("runs", help="de onde veio cada aresta: termo por janela")
    runs.add_argument("--top", type=int, default=10,
                      help="termos por janela (padrão: 10)")

    ag = _janela(sub.add_parser("aggregate", help="interaction → edge_window"))
    ag.add_argument("--by-topic", action="store_true",
                    help="agrega também um escopo por pauta (topic:<rótulo>), "
                         "recortando as interações àquela coleta")
    ag.add_argument("--topic", action="append", metavar="PAUTA",
                    help="agrega UMA pauta, montando o escopo a partir dos "
                         "rótulos. Pode repetir: a mesma pauta chega partida em "
                         "etiquetas diferentes (CPMI, #CPMIdoGolpe, CPMI do Golpe)")
    an = _janela(sub.add_parser("analyze", help="grafo, comunidades e métricas"),
                 com_view=True, com_core=True)
    an.add_argument("--topic", action="append", metavar="PAUTA",
                    help="monta o escopo de pauta a partir dos rótulos. Pode "
                         "repetir: a mesma pauta chega partida em etiquetas "
                         "diferentes (CPMI, #CPMIdoGolpe, CPMI do Golpe)")
    an.add_argument("--by-topic", action="store_true",
                    help="analisa também um grafo por pauta da janela "
                         "(amp:topic:<rótulo>), que é o que responde 'quem é "
                         "central NESTA pauta'. Espelha o --by-topic do aggregate")
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

    cand = sub.add_parser("candidatos",
                          help="sugere sementes a partir do que a base já mediu")
    cand.add_argument("--platform", default="x", help="plataforma (padrão: x)")
    cand.add_argument("--top", type=int, default=40, help="quantos mostrar")
    cand.add_argument("--desde", help="só interações a partir desta data (ISO)")
    cand.add_argument("--excluir", help="arquivo de sementes já registradas, "
                                        "para não repetir quem já está na lista")
    seeds = sub.add_parser("seeds", help="registra ou lista a lista curada de perfis")
    seeds.add_argument("--file", help="arquivo com um handle ou DID por linha")
    seeds.add_argument("--source", default="bluesky", choices=["bluesky", "x"],
                       help="plataforma da lista (x resolve handle→id no provedor, "
                            "e custa uma requisição por entrada)")
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
            "themes": cmd_themes, "export": cmd_export,
            "compare": cmd_compare, "radar": cmd_radar, "dossie": cmd_dossie,
            "label": cmd_label,
            "dump": cmd_dump, "seeds": cmd_seeds, "discover": cmd_discover,
            "candidatos": cmd_candidatos,
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
