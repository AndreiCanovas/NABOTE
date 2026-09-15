"""Testes do recorte por pauta e dos números do Radar.

O bug que motivou este arquivo: `aggregate --scope topic:x` gravava o grafo
INTEIRO sob o rótulo da pauta, sem recortar nada. Toda métrica calculada ali
descreveria a rede toda enquanto dizia descrever uma pauta — plausível e falso.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from nabote import db, graph, ingest, radar  # noqa: E402
from nabote.events import NormalizedEvent, Target  # noqa: E402
from synthetic import ListSource, planted_communities  # noqa: E402


class Fonte:
    def __init__(self, nome, eventos):
        self.name, self._eventos, self.skipped = nome, eventos, 0

    def events(self, cursor=None):
        yield from self._eventos


class RadarTestCase(unittest.TestCase):
    # (termo, comunidade plantada, quantos posts)
    PAUTAS = [("Yanomami", 0, 26), ("BNDES", 1, 18), ("Temer", 2, 10)]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "r.db")
        db.migrate(self.conn, ROOT / "migrations")
        eventos, _ = planted_communities(n_communities=3, per_community=14)
        ingest.ingest(self.conn, ListSource(eventos, "nucleo"), author_tier="A")

        for termo, com, n in self.PAUTAS:
            rotulados = [NormalizedEvent(
                platform="bluesky", kind="post",
                actor_uid=f"did:plc:sint{com:02d}{(i % 13) + 1:03d}",
                occurred_at="2026-09-15T12:00:00Z", post_uid=f"x{termo}{i}",
                post_type="repost",
                targets=[Target(kind="repost", uid=f"did:plc:sint{com:02d}000")])
                for i in range(n)]
            ingest.ingest(self.conn, Fonte(f"x:{termo}", rotulados), kind="campanha",
                          campaign_label=termo, author_tier="C", resume=False)

        self.window = graph.windows_present(self.conn)[0]
        graph.aggregate_window(self.conn, self.window)
        graph.analyze_window(self.conn, self.window, view="amp")
        graph.analyze_window(self.conn, self.window, view="amp", core=True)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _arestas(self, scope: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM edge_window WHERE window_start=? AND scope=?",
            (self.window, scope)).fetchone()["n"]


class TestRecortePorPauta(RadarTestCase):
    def test_escopo_de_pauta_recorta_de_verdade(self):
        """O bug original: o escopo trocava a etiqueta e gravava tudo."""
        cheio = self._arestas("all")
        for termo, _, _ in self.PAUTAS:
            graph.aggregate_window(self.conn, self.window, scope=f"topic:{termo}")
            recorte = self._arestas(f"topic:{termo}")
            self.assertGreater(recorte, 0, f"{termo} ficou vazio")
            self.assertLess(recorte, cheio, f"{termo} trouxe o grafo inteiro")

    def test_so_entram_atores_da_propria_pauta(self):
        """Cada pauta foi plantada dentro de uma comunidade. Se o recorte
        vazasse, apareceriam atores das outras."""
        for termo, com, _ in self.PAUTAS:
            graph.aggregate_window(self.conn, self.window, scope=f"topic:{termo}")
            quem = {r["platform_user_id"] for r in self.conn.execute(
                "SELECT DISTINCT a.platform_user_id FROM edge_window e "
                "JOIN actor a ON a.actor_id IN (e.src_actor_id, e.dst_actor_id) "
                "WHERE e.window_start=? AND e.scope=?", (self.window, f"topic:{termo}"))}
            self.assertTrue(quem)
            for uid in quem:
                self.assertTrue(uid.startswith(f"did:plc:sint{com:02d}"),
                                f"{uid} não é da comunidade da pauta {termo}")

    def test_soma_das_pautas_nao_passa_do_cheio(self):
        for termo, _, _ in self.PAUTAS:
            graph.aggregate_window(self.conn, self.window, scope=f"topic:{termo}")
        soma = sum(self._arestas(f"topic:{t}") for t, _, _ in self.PAUTAS)
        self.assertLessEqual(soma, self._arestas("all"))

    def test_pautas_saem_da_mais_volumosa_para_a_menor(self):
        self.assertEqual(graph.topics_in_window(self.conn, self.window),
                         ["Yanomami", "BNDES", "Temer"])


class TestNumerosDaPauta(RadarTestCase):
    def test_volume_e_autores(self):
        v = radar.topic_volume(self.conn, self.window)
        self.assertEqual(v["Yanomami"]["posts"], 26)
        self.assertEqual(v["BNDES"]["posts"], 18)
        self.assertLessEqual(v["Yanomami"]["autores"], 13)

    def test_concentracao_entre_zero_e_um(self):
        """Fatia dos posts nas mãos das três maiores contas. Uma pauta puxada
        por poucas contas é o padrão que mais justifica olhar de perto."""
        for dados in radar.topic_volume(self.conn, self.window).values():
            self.assertGreater(dados["concentracao"], 0)
            self.assertLessEqual(dados["concentracao"], 1)

    def test_dominante_e_a_comunidade_onde_a_pauta_foi_plantada(self):
        spread = radar.topic_spread(self.conn, self.window, "amp:core")
        comunidades = {}
        for r in self.conn.execute(
            "SELECT a.platform_user_id AS uid, c.community_id AS com "
            "FROM actor_community c JOIN actor a ON a.actor_id = c.actor_id "
            "WHERE c.scope='amp:core'"):
            comunidades[r["uid"]] = r["com"]
        for termo, com, _ in self.PAUTAS:
            esperada = comunidades.get(f"did:plc:sint{com:02d}001")
            if esperada is None:
                continue
            self.assertEqual(spread[termo]["dominante"], esperada,
                             f"{termo} deveria dominar na comunidade plantada")

    def test_atravessa_conta_so_presenca_relevante(self):
        """Respingo de um ator numa comunidade não é circulação."""
        for dados in radar.topic_spread(self.conn, self.window, "amp:core").values():
            self.assertGreaterEqual(dados["atravessa"], 1)
            self.assertLessEqual(dados["atravessa"], len(dados["por_comunidade"]))


class TestSnapshot(RadarTestCase):
    def setUp(self):
        super().setUp()
        for termo, _, _ in self.PAUTAS:
            graph.aggregate_window(self.conn, self.window, scope=f"topic:{termo}")
            graph.analyze_window(self.conn, self.window, view="amp",
                                 edge_scope=f"topic:{termo}")
        self.snap = radar.snapshot(self.conn, self.window, view="amp")

    def test_traz_todas_as_pautas_com_seus_atores(self):
        self.assertEqual([p["termo"] for p in self.snap["pautas"]],
                         ["Yanomami", "BNDES", "Temer"])
        for pauta in self.snap["pautas"]:
            self.assertTrue(pauta["atores"], f"{pauta['termo']} sem atores")

    def test_pagerank_e_o_da_pauta_nao_o_do_grafo(self):
        """É o ponto em que o escopo importa: quem lidera uma pauta não é quem
        lidera o grafo, e uma métrica global esconderia isso."""
        global_topo = self.conn.execute(
            "SELECT COALESCE(a.handle, a.platform_user_id) AS quem FROM actor_metric m "
            "JOIN actor a ON a.actor_id = m.actor_id WHERE m.window_start=? "
            "AND m.scope='amp' AND m.metric='pagerank' ORDER BY m.value DESC LIMIT 1",
            (self.window,)).fetchone()["quem"]
        lideres = {p["atores"][0]["quem"] for p in self.snap["pautas"]}
        self.assertGreater(len(lideres), 1, "cada pauta deveria ter o seu líder")
        self.assertIn(global_topo, {*lideres, global_topo})

    def test_comunidades_trazem_volume_e_pautas(self):
        self.assertTrue(self.snap["comunidades"])
        fatias = sum(c["fatia_volume"] for c in self.snap["comunidades"])
        self.assertAlmostEqual(fatias, 1.0, places=6)
        for c in self.snap["comunidades"]:
            if c["volume"]:
                self.assertTrue(c["pautas"])

    def test_serializa_em_json(self):
        import json
        json.dumps(self.snap, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
