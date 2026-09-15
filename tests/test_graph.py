"""Testes da camada de grafo.

A verificação central: plantamos comunidades conhecidas, rodamos o pipeline
inteiro (ingest → aggregate → analyze) e conferimos se o Leiden as recupera.
Grafo aleatório não provaria nada; estrutura plantada prova que agregação,
pesos e detecção funcionam em conjunto.
"""

from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from nabote import db, graph, ingest  # noqa: E402
from synthetic import (  # noqa: E402
    ListSource, ambiguous_graph, block_graph, did, hub_audience_graph,
    planted_communities, scattered_dyads)


class TestWindow(unittest.TestCase):
    def test_window_starts_on_monday(self):
        # 2026-09-17 é uma quinta; a janela dela começa na segunda, dia 14
        self.assertEqual(graph.window_start_for("2026-09-17T13:45:00Z"), "2026-09-14")
        self.assertEqual(graph.window_start_for("2026-09-14T00:00:00Z"), "2026-09-14")
        self.assertEqual(graph.window_start_for("2026-09-20T23:59:59Z"), "2026-09-14")
        # domingo pertence à janela que começou na segunda anterior
        self.assertEqual(graph.window_start_for("2026-09-21T00:00:01Z"), "2026-09-21")

    def test_accepts_offsets_and_missing_zone(self):
        self.assertEqual(graph.window_start_for("2026-09-17T13:45:00+00:00"), "2026-09-14")
        self.assertEqual(graph.window_start_for("2026-09-17T13:45:00"), "2026-09-14")

    def test_survives_every_fractional_second_shape(self):
        """No Python 3.10 o fromisoformat só aceita 3 ou 6 casas decimais e recusa
        o sufixo 'Z'. A API do X devolve as duas coisas, então o parser precisa
        aguentar todas as formas — senão o projeto quebra em Ubuntu 22.04."""
        for bruto in ["2026-09-17T13:45:00Z",
                      "2026-09-17T13:45:00.000Z",
                      "2026-09-17T13:45:00.123456Z",
                      "2026-09-17T13:45:00.0000000Z",   # 7 casas: cai no fallback
                      "2026-09-17T13:45:00.12Z"]:        # 2 casas: idem
            with self.subTest(bruto=bruto):
                self.assertEqual(graph.window_start_for(bruto), "2026-09-14")


class GraphTestCase(unittest.TestCase):
    N_COMMUNITIES = 3
    PER_COMMUNITY = 8

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "g.db")
        db.migrate(self.conn, ROOT / "migrations")

        events, self.truth = planted_communities(
            n_communities=self.N_COMMUNITIES, per_community=self.PER_COMMUNITY)
        ingest.ingest(self.conn, ListSource(events), author_tier="A")

        self.window = graph.windows_present(self.conn)[0]
        graph.aggregate_window(self.conn, self.window)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _actor_id(self, d: str) -> int:
        return self.conn.execute(
            "SELECT actor_id FROM actor WHERE platform_user_id = ?", (d,)).fetchone()[0]

    def _metric(self, d: str, name: str, scope="amp") -> float:
        row = self.conn.execute(
            "SELECT value FROM actor_metric WHERE actor_id = ? AND metric = ? AND scope = ?",
            (self._actor_id(d), name, scope)).fetchone()
        return row["value"] if row else None


class TestAggregate(GraphTestCase):
    def test_edges_are_grouped_by_pair_and_kind(self):
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n, SUM(weight) AS w FROM edge_window "
            "WHERE window_start = ?", (self.window,)).fetchone()
        interactions = self.conn.execute(
            "SELECT COUNT(*) AS n FROM interaction").fetchone()["n"]
        # peso total preserva a contagem; nº de arestas é menor por agrupamento
        self.assertEqual(rows["w"], interactions)
        self.assertLess(rows["n"], interactions)

    def test_reaggregating_is_idempotent(self):
        before = self.conn.execute(
            "SELECT COUNT(*) AS n, SUM(weight) AS w FROM edge_window").fetchone()
        graph.aggregate_window(self.conn, self.window)
        after = self.conn.execute(
            "SELECT COUNT(*) AS n, SUM(weight) AS w FROM edge_window").fetchone()
        self.assertEqual((before["n"], before["w"]), (after["n"], after["w"]))


class TestViewsAreSeparate(GraphTestCase):
    """As respostas foram plantadas CRUZANDO comunidades, de propósito."""

    def test_amp_view_carries_only_amplification_weight(self):
        """`all` funde tipos diferentes entre o MESMO par numa aresta só, então
        contar arestas não serve. O invariante é o peso: a visão `amp` tem de
        somar exatamente repost×1,0 + citação×0,8 do edge_window."""
        esperado = self.conn.execute(
            "SELECT COALESCE(SUM(CASE kind WHEN 'repost' THEN weight*1.0 "
            "WHEN 'quote' THEN weight*0.8 ELSE 0 END), 0) AS w "
            "FROM edge_window WHERE window_start = ?", (self.window,)).fetchone()["w"]
        g_amp, _ = graph.load_graph(self.conn, self.window, view="amp")
        self.assertGreater(g_amp.ecount(), 0)
        self.assertAlmostEqual(sum(g_amp.es["weight"]), esperado, places=6)

    def test_reply_view_carries_only_reply_weight(self):
        esperado = self.conn.execute(
            "SELECT COALESCE(SUM(weight), 0) AS w FROM edge_window "
            "WHERE window_start = ? AND kind = 'reply'", (self.window,)).fetchone()["w"]
        g_reply, _ = graph.load_graph(self.conn, self.window, view="reply")
        self.assertGreater(g_reply.ecount(), 0)
        self.assertAlmostEqual(sum(g_reply.es["weight"]), esperado, places=6)

    def test_pair_with_both_kinds_collapses_into_one_edge_in_all(self):
        g_amp, _ = graph.load_graph(self.conn, self.window, view="amp")
        g_reply, _ = graph.load_graph(self.conn, self.window, view="reply")
        g_all, _ = graph.load_graph(self.conn, self.window, view="all")
        self.assertLessEqual(g_all.ecount(), g_amp.ecount() + g_reply.ecount())
        self.assertGreaterEqual(g_all.ecount(), max(g_amp.ecount(), g_reply.ecount()))

    def test_reply_edges_cross_communities(self):
        g_reply, ids = graph.load_graph(self.conn, self.window, view="reply")
        by_id = {self._actor_id(d): c for d, c in self.truth.items()}
        crossing = sum(
            1 for e in g_reply.es
            if by_id[ids[e.tuple[0]]] != by_id[ids[e.tuple[1]]])
        self.assertEqual(crossing, g_reply.ecount())


class TestCommunityRecovery(GraphTestCase):
    def test_leiden_recovers_the_planted_structure(self):
        result = graph.analyze_window(self.conn, self.window, view="amp")
        self.assertEqual(result["nodes"], self.N_COMMUNITIES * self.PER_COMMUNITY)
        self.assertEqual(result["communities"], self.N_COMMUNITIES)

        rows = self.conn.execute(
            "SELECT a.platform_user_id AS did, ac.community_id FROM actor_community ac "
            "JOIN actor a ON a.actor_id = ac.actor_id WHERE ac.scope = 'amp'").fetchall()
        found = {r["did"]: r["community_id"] for r in rows}

        # rótulos são arbitrários; o que importa é o particionamento coincidir
        mapping: dict[int, int] = {}
        for d, real in self.truth.items():
            mapping.setdefault(found[d], real)
        correct = sum(1 for d, real in self.truth.items() if mapping[found[d]] == real)
        self.assertEqual(correct, len(self.truth),
                         "Leiden não recuperou a partição plantada")

    def _partition(self, scope: str) -> frozenset[frozenset[str]]:
        """Partição como conjunto de grupos — comparável sem depender do rótulo."""
        rows = self.conn.execute(
            "SELECT a.platform_user_id AS did, ac.community_id FROM actor_community ac "
            "JOIN actor a ON a.actor_id = ac.actor_id WHERE ac.scope = ?", (scope,))
        grupos: dict[int, set[str]] = {}
        for row in rows:
            grupos.setdefault(row["community_id"], set()).add(row["did"])
        return frozenset(frozenset(g) for g in grupos.values())

    def test_reply_view_yields_a_different_partition(self):
        """Contar comunidades não basta: duas visões podem dar 3 grupos cada e
        agrupar gente diferente. O que se compara é a partição."""
        graph.analyze_window(self.conn, self.window, view="amp")
        graph.analyze_window(self.conn, self.window, view="reply")
        self.assertNotEqual(self._partition("amp"), self._partition("reply"),
                            "visões distintas produziram a mesma partição — "
                            "sinal de que a separação não está valendo")

    def test_amp_partition_matches_the_planted_truth(self):
        graph.analyze_window(self.conn, self.window, view="amp")
        plantada = frozenset(
            frozenset(d for d, c in self.truth.items() if c == community)
            for community in set(self.truth.values()))
        self.assertEqual(self._partition("amp"), plantada)


class TestMetrics(GraphTestCase):
    def setUp(self):
        super().setUp()
        self.result = graph.analyze_window(self.conn, self.window, view="amp")

    def test_hub_leads_pagerank_in_its_community(self):
        for community in range(self.N_COMMUNITIES):
            hub = self._metric(did(community, 0), "pagerank")
            others = [self._metric(did(community, i), "pagerank")
                      for i in range(1, self.PER_COMMUNITY)]
            self.assertGreater(hub, max(others),
                               f"hub da comunidade {community} não lidera")

    def test_ei_index_is_negative_for_closed_clusters(self):
        """Estrutura plantada é fechada: quase toda aresta é interna."""
        values = [r["value"] for r in self.conn.execute(
            "SELECT value FROM actor_metric WHERE metric='ei_index' AND scope='amp'")]
        self.assertTrue(all(-1.0 <= v <= 1.0 for v in values))
        media = sum(values) / len(values)
        self.assertLess(media, -0.5, "comunidades plantadas deveriam ser fechadas")

    def test_all_mvp_metrics_were_written(self):
        self.assertEqual(
            set(self.result["metrics"]),
            {"in_degree_w", "out_degree_w", "pagerank", "ei_index", "betweenness"})

    def test_community_rows_carry_size_and_mean_ei(self):
        rows = self.conn.execute(
            "SELECT size, ei_mean FROM community WHERE scope='amp'").fetchall()
        self.assertEqual(sum(r["size"] for r in rows),
                         self.N_COMMUNITIES * self.PER_COMMUNITY)
        self.assertTrue(all(r["ei_mean"] is not None for r in rows))

    def test_reanalyzing_replaces_instead_of_duplicating(self):
        before = self.conn.execute(
            "SELECT COUNT(*) AS n FROM actor_metric WHERE scope='amp'").fetchone()["n"]
        graph.analyze_window(self.conn, self.window, view="amp")
        after = self.conn.execute(
            "SELECT COUNT(*) AS n FROM actor_metric WHERE scope='amp'").fetchone()["n"]
        self.assertEqual(before, after)

    def test_scope_isolates_views(self):
        graph.analyze_window(self.conn, self.window, view="reply")
        scopes = {r["scope"] for r in self.conn.execute(
            "SELECT DISTINCT scope FROM actor_metric")}
        self.assertEqual(scopes, {"amp", "reply"})


class TestWindowSQL(unittest.TestCase):
    def test_sql_e_python_concordam_todo_dia(self):
        """`WINDOW_SQL` duplica a regra de `window_start_for` em SQL. Duplicata
        que diverge é o pior tipo de bug: as duas metades do sistema passam a
        discordar sobre qual semana é qual, e nada reclama."""
        import sqlite3
        from datetime import date, timedelta

        conn = sqlite3.connect(":memory:")
        consulta = "SELECT " + graph.WINDOW_SQL.format(col="?")
        dia, fim, divergentes = date(2022, 11, 1), date(2024, 3, 1), []
        while dia <= fim:
            for hora in ("00:00:00", "13:45:06", "23:59:59"):
                bruto = f"{dia.isoformat()}T{hora}Z"
                # o SQL usa o mesmo placeholder duas vezes
                sql = conn.execute(consulta, (bruto, bruto)).fetchone()[0]
                if sql != graph.window_start_for(bruto):
                    divergentes.append((bruto, sql, graph.window_start_for(bruto)))
            dia += timedelta(days=1)
        conn.close()
        self.assertEqual(divergentes, [])


class TestComponentStats(unittest.TestCase):
    """Fragmentação: a diferença entre uma rede e uma pilha de cacos.

    A base histórica do X, amostrada por termo, produziu 578 comunidades — quase
    todas com dois ou três atores. Contar comunidades não denuncia isso; contar
    componentes sim.
    """

    def _grafo(self, n: int, arestas: list[tuple[int, int]]):
        import igraph
        g = igraph.Graph(directed=True)
        g.add_vertices(n)
        g.add_edges(arestas)
        g.es["weight"] = [1.0] * len(arestas)
        return g

    def test_grafo_vazio_nao_quebra(self):
        self.assertEqual(graph.component_stats(self._grafo(0, [])),
                         {"components": 0, "largest": 0, "core_share": 0.0,
                          "trivial": 0})

    def test_separa_nucleo_de_cauda(self):
        # ciclo de 5 + duas díades soltas
        g = self._grafo(9, [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0), (5, 6), (7, 8)])
        s = graph.component_stats(g)
        self.assertEqual(s["components"], 3)
        self.assertEqual(s["largest"], 5)
        self.assertAlmostEqual(s["core_share"], 5 / 9)
        self.assertEqual(s["trivial"], 2)

    def test_componente_e_fraco_nao_forte(self):
        """a→b ←c é UM componente. Se fosse forte seriam três, e toda coleta
        real — onde ninguém retuíta de volta — pareceria totalmente fragmentada."""
        self.assertEqual(
            graph.component_stats(self._grafo(3, [(0, 1), (2, 1)]))["components"], 1)

    def test_ei_da_diade_e_mecanico(self):
        """−1,00 numa díade não é câmara de eco: é aritmética. Não existe aresta
        externa possível quando a comunidade é o componente inteiro."""
        g = self._grafo(2, [(0, 1)])
        self.assertEqual(graph.ei_index(g, [0, 0]), [-1.0, -1.0])


class TestFragmentacaoNoPipeline(GraphTestCase):
    """O mesmo padrão da base real: núcleo pequeno afogado em díades soltas."""

    DIADES = 40

    def setUp(self):
        super().setUp()
        ingest.ingest(self.conn, ListSource(scattered_dyads(self.DIADES), "diades"),
                      author_tier="C")
        graph.aggregate_window(self.conn, self.window)
        self.resultado = graph.analyze_window(self.conn, self.window, view="amp")

    def test_cada_diade_vira_um_componente(self):
        """As 40 díades são 40 componentes. O núcleo plantado acrescenta entre 1
        (se as pontes ligarem tudo) e N_COMMUNITIES (se não ligarem nada)."""
        nucleo = self.N_COMMUNITIES * self.PER_COMMUNITY
        self.assertGreaterEqual(self.resultado["components"], self.DIADES + 1)
        self.assertLessEqual(self.resultado["components"],
                             self.DIADES + self.N_COMMUNITIES)
        self.assertGreaterEqual(self.resultado["trivial"], self.DIADES)
        self.assertLessEqual(self.resultado["largest"], nucleo)

    def test_core_share_denuncia_o_que_a_contagem_de_comunidades_esconde(self):
        """Comunidades demais pode ser estrutura rica ou lixo de amostragem.
        `core_share` distingue: aqui o núcleo é minoria do grafo."""
        self.assertGreater(self.resultado["communities"], self.N_COMMUNITIES)
        self.assertLess(self.resultado["core_share"], 0.5)

    def test_diades_nao_contaminam_o_nucleo(self):
        """O ruído não pode mudar a comunidade de quem está no núcleo — ele vive
        em outro componente. Se mudar, a agregação está juntando o que não deve."""
        membros = {}
        for d, verdade in self.truth.items():
            row = self.conn.execute(
                "SELECT community_id FROM actor_community c JOIN actor a "
                "ON a.actor_id = c.actor_id WHERE a.platform_user_id = ? "
                "AND c.scope = 'amp'", (d,)).fetchone()
            membros.setdefault(verdade, set()).add(row["community_id"])
        for verdade, encontrados in membros.items():
            self.assertEqual(len(encontrados), 1,
                             f"comunidade plantada {verdade} rachou em {encontrados}")


class TestNumeracaoDeComunidade(unittest.TestCase):
    """O número da comunidade precisa significar alguma coisa.

    O Leiden é heurístico e aleatório: sem semente fixa, a mesma janela
    analisada duas vezes devolve números diferentes. Qualquer relatório que cite
    "comunidade #197" vira ficção, e a série temporal compara coisas distintas
    sem avisar.
    """

    N_COMMUNITIES = 3
    PER_COMMUNITY = 8
    DIADES = 15

    def setUp(self):
        """As díades entram ANTES do núcleo, de propósito.

        O Leiden rotula as comunidades mais ou menos na ordem em que os nós
        aparecem. Ingerindo o núcleo primeiro, os rótulos crus já sairiam
        ordenados por tamanho e o teste passaria mesmo sem renumeração alguma —
        que foi exatamente o que aconteceu na primeira versão deste teste.
        """
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "n.db")
        db.migrate(self.conn, ROOT / "migrations")
        ingest.ingest(self.conn, ListSource(scattered_dyads(self.DIADES), "diades"),
                      author_tier="C")
        events, _ = planted_communities(n_communities=self.N_COMMUNITIES,
                                        per_community=self.PER_COMMUNITY)
        ingest.ingest(self.conn, ListSource(events, "nucleo"), author_tier="A")
        self.window = graph.windows_present(self.conn)[0]
        graph.aggregate_window(self.conn, self.window)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _particao(self) -> dict[str, int]:
        graph.analyze_window(self.conn, self.window, view="amp")
        return {r["platform_user_id"]: r["community_id"] for r in self.conn.execute(
            "SELECT a.platform_user_id, c.community_id FROM actor_community c "
            "JOIN actor a ON a.actor_id = c.actor_id WHERE c.scope = 'amp'")}

    def test_reanalisar_devolve_a_mesma_numeracao(self):
        primeira = self._particao()
        for _ in range(3):
            self.assertEqual(self._particao(), primeira)

    def test_numeracao_segue_o_tamanho(self):
        """#0 é a maior, e o tamanho nunca volta a subir conforme o ID cresce.
        Assim "#3" carrega informação — é a quarta maior — em vez de ser um
        rótulo interno do algoritmo vazando para o relatório."""
        self._particao()
        tamanhos = [r["size"] for r in self.conn.execute(
            "SELECT size FROM community WHERE scope = 'amp' ORDER BY community_id")]
        self.assertEqual(tamanhos[0], max(tamanhos))
        self.assertEqual(tamanhos, sorted(tamanhos, reverse=True))
        self.assertGreater(tamanhos[0], tamanhos[-1], "tamanhos todos iguais: "
                           "o teste não distingue numeração ordenada de acaso")

    def test_ids_sao_contiguos_a_partir_de_zero(self):
        """Buraco na numeração denuncia renumeração mal feita — e vira
        `--community 5` devolvendo lista vazia sem explicação."""
        self._particao()
        ids = [r["community_id"] for r in self.conn.execute(
            "SELECT community_id FROM community WHERE scope = 'amp' "
            "ORDER BY community_id")]
        self.assertEqual(ids, list(range(len(ids))))


class TestDeterminismo(unittest.TestCase):
    """Num grafo AMBÍGUO — o único onde a semente importa.

    Sem semente, este grafo devolve uma partição diferente a cada execução; o
    teste falha imediatamente se alguém remover o `_rng`. No grafo plantado
    limpo o Leiden acerta sempre, então lá o mesmo teste passaria vazio.
    """

    RODADAS = 12

    def test_leiden_sem_semente_de_fato_oscila(self):
        """Valida o próprio teste: se este grafo parar de ser ambíguo, o teste
        de determinismo abaixo vira decoração e ninguém percebe."""
        g = ambiguous_graph().as_undirected(combine_edges="sum")
        vistas = {tuple(g.community_leiden(objective_function="modularity",
                                           weights="weight").membership)
                  for _ in range(self.RODADAS)}
        self.assertGreater(len(vistas), 1,
                           "grafo deixou de ser ambíguo; o teste de determinismo "
                           "não prova mais nada")

    def test_detect_communities_e_estavel(self):
        g = ambiguous_graph()
        vistas = {tuple(graph.detect_communities(g)) for _ in range(self.RODADAS)}
        self.assertEqual(len(vistas), 1)

    def test_rng_devolve_o_gerador_de_antes(self):
        """Mexer no `random` global e não devolver contamina o resto do processo
        — inclusive qualquer amostragem que venha depois."""
        import igraph
        marcador = random.Random(99)
        igraph.set_random_number_generator(marcador)
        with graph._rng():
            pass
        igraph.set_random_number_generator(marcador)  # não deve explodir
        g = ambiguous_graph(blocks=3, per_block=5)
        antes = tuple(graph.detect_communities(g))
        random.seed(12345)
        self.assertEqual(tuple(graph.detect_communities(g)), antes,
                         "detect_communities passou a depender do random global")


class TestCursorPorFonte(unittest.TestCase):
    def test_fontes_homonimas_se_canibalizam(self):
        """O cursor é por NOME de fonte. Duas fontes com o mesmo nome fazem a
        segunda retomar de onde a primeira parou — e sumir com os eventos mais
        antigos sem erro nenhum. Documentado aqui porque custou um teste que
        parecia verde e estava vazio."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "c.db")
            db.migrate(conn, ROOT / "migrations")
            tardios = scattered_dyads(5)
            cedo, _ = planted_communities(n_communities=2, per_community=4)
            self.assertGreater(tardios[0]["time_us"], cedo[-1]["time_us"],
                               "o cenário exige que os tardios venham depois")

            ingest.ingest(conn, ListSource(tardios, "mesma"), author_tier="C")
            ingest.ingest(conn, ListSource(cedo, "mesma"), author_tier="A")
            engolidos = conn.execute("SELECT COUNT(*) AS n FROM interaction").fetchone()["n"]

            ingest.ingest(conn, ListSource(cedo, "outra"), author_tier="A")
            completos = conn.execute("SELECT COUNT(*) AS n FROM interaction").fetchone()["n"]
            conn.close()

        self.assertEqual(engolidos, len(tardios), "os eventos antigos deveriam ter sumido")
        self.assertGreater(completos, engolidos, "com nome próprio, entram todos")


def _corr(x: list[float], y: list[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sx = sum((a - mx) ** 2 for a in x) ** 0.5
    sy = sum((b - my) ** 2 for b in y) ** 0.5
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy) if sx and sy else 0.0


class TestEICompravel(unittest.TestCase):
    """O E-I só é comparável entre comunidades se restrito a quem teve ESCOLHA.

    Numa rede de audiência-em-torno-de-hub a maioria amplificou uma vez só:
    grau 1, E-I −1 por aritmética. Essa gente não escolheu ficar dentro do
    grupo — não houve segunda chance de atravessar. A média sobre todo mundo
    tende a −1 conforme a audiência cresce, e foi daí que veio a correlação de
    −0,73 entre tamanho e E-I no dado real.
    """

    TAMANHOS = [4000, 3000, 2000, 1500, 1000, 700, 500, 300, 200, 100]

    def setUp(self):
        self.g = hub_audience_graph(self.TAMANHOS)
        self.membership = graph.detect_communities(self.g)
        self.tam: dict[int, int] = {}
        for c in self.membership:
            self.tam[c] = self.tam.get(c, 0) + 1

    def _serie(self, min_strength):
        valores = graph.community_ei(self.g, self.membership,
                                     min_strength=min_strength)
        ordem = [c for c in sorted(self.tam, key=lambda c: -self.tam[c])
                 if c in valores]
        return ([self.tam[c] for c in ordem], [valores[c][0] for c in ordem])

    def test_ei_cru_confunde_tamanho_com_fechamento(self):
        """O problema, medido. Comportamento relativo idêntico em todas as
        audiências, e mesmo assim o E-I cru anda com o tamanho — e satura em
        −1,00, onde nenhuma diferença é mais visível."""
        tamanhos, valores = self._serie(0.0)
        self.assertLess(_corr(tamanhos, valores), -0.4)
        self.assertLess(max(valores), -0.99, "o E-I cru satura perto de −1")

    def test_com_escolha_deixa_de_seguir_o_tamanho(self):
        tamanhos, valores = self._serie(graph.CHOICE_STRENGTH)
        self.assertGreater(_corr(tamanhos, valores), -0.4)
        # e os valores passam a se agrupar, em vez de saturar
        self.assertLess(max(valores) - min(valores), 0.2)

    def test_conta_quantos_atores_sustentam_a_media(self):
        """Comunidade onde quase ninguém teve escolha tem E-I frágil; o número
        de atores precisa viajar junto com a média."""
        valores = graph.community_ei(self.g, self.membership,
                                     min_strength=graph.CHOICE_STRENGTH)
        for community, (_, atores) in valores.items():
            self.assertGreater(atores, 0)
            self.assertLessEqual(atores, self.tam[community])

    def test_grava_as_duas_medidas(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "e.db")
            db.migrate(conn, ROOT / "migrations")
            events, _ = planted_communities(n_communities=4, per_community=12)
            ingest.ingest(conn, ListSource(events, "nucleo"), author_tier="A")
            window = graph.windows_present(conn)[0]
            graph.aggregate_window(conn, window)
            graph.analyze_window(conn, window, view="amp")
            linhas = conn.execute(
                "SELECT size, ei_mean, ei_choice, choice_actors FROM community "
                "WHERE scope='amp'").fetchall()
            conn.close()
        self.assertTrue(linhas)
        com_escolha = [r for r in linhas if r["ei_choice"] is not None]
        self.assertTrue(com_escolha, "nenhuma comunidade gravou ei_choice")
        for r in com_escolha:
            self.assertIsNotNone(r["ei_mean"])
            self.assertGreater(r["choice_actors"], 0)
            self.assertLessEqual(r["choice_actors"], r["size"])


class TestRelabel(unittest.TestCase):
    def test_ordena_por_tamanho_decrescente(self):
        # rótulos originais: 7 aparece 1×, 3 aparece 3×, 5 aparece 2×
        novo = graph._relabel_by_size([7, 3, 3, 5, 3, 5])
        self.assertEqual(novo, [2, 0, 0, 1, 0, 1])

    def test_empate_e_desfeito_pelo_primeiro_no(self):
        """Empate resolvido por acaso reintroduz exatamente o problema que a
        renumeração existe para eliminar."""
        self.assertEqual(graph._relabel_by_size([9, 9, 4, 4]), [0, 0, 1, 1])
        self.assertEqual(graph._relabel_by_size([4, 4, 9, 9]), [0, 0, 1, 1])


class TestEmptyWindow(unittest.TestCase):
    def test_window_without_edges_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "e.db")
            db.migrate(conn, ROOT / "migrations")
            graph.aggregate_window(conn, "2026-01-05")
            result = graph.analyze_window(conn, "2026-01-05")
            self.assertEqual(result["nodes"], 0)
            self.assertEqual(result["communities"], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
