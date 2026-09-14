"""Testes da camada de grafo.

A verificação central: plantamos comunidades conhecidas, rodamos o pipeline
inteiro (ingest → aggregate → analyze) e conferimos se o Leiden as recupera.
Grafo aleatório não provaria nada; estrutura plantada prova que agregação,
pesos e detecção funcionam em conjunto.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from nabote import db, graph, ingest  # noqa: E402
from synthetic import ListSource, did, planted_communities  # noqa: E402


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
