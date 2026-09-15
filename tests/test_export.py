"""Testes da exportação.

O CSV é o que sai do prédio. Um erro aqui não fica no terminal de quem rodou:
vira planilha, vira slide, vira decisão.
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from nabote import db, export, graph, ingest  # noqa: E402
from nabote.events import NormalizedEvent, Target  # noqa: E402
from synthetic import ListSource, planted_communities, scattered_dyads  # noqa: E402


class Fonte:
    def __init__(self, nome, eventos):
        self.name, self._eventos, self.skipped = nome, eventos, 0

    def events(self, cursor=None):
        yield from self._eventos


class ExportTestCase(unittest.TestCase):
    DIAS = ["2026-09-15T12:00:00Z"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.destino = Path(self._tmp.name) / "saida"
        self.destino.mkdir()
        self.conn = db.connect(Path(self._tmp.name) / "e.db")
        db.migrate(self.conn, ROOT / "migrations")

        eventos, _ = planted_communities(n_communities=4, per_community=15)
        ingest.ingest(self.conn, ListSource(eventos, "nucleo"), author_tier="A")
        ingest.ingest(self.conn, ListSource(scattered_dyads(20), "diades"),
                      author_tier="C")
        # coleta rotulada, na MESMA plataforma dos eventos sintéticos: ator é
        # identificado por (plataforma, id), então divergir de plataforma criaria
        # atores paralelos e as pautas não encostariam nas comunidades
        for termo, alvo, n in [("posse", 0, 12), ("alckmin", 1, 8)]:
            rotulados = [NormalizedEvent(
                platform="bluesky", kind="post",
                actor_uid=f"did:plc:sint{alvo:02d}{i:03d}",
                occurred_at=self.DIAS[i % len(self.DIAS)],
                post_uid=f"rot{termo}{i}", post_type="repost",
                targets=[Target(kind="repost", uid=f"did:plc:sint{alvo:02d}000")])
                for i in range(1, n)]
            ingest.ingest(self.conn, Fonte(f"x:{termo}", rotulados), kind="campanha",
                          campaign_label=termo, author_tier="C", resume=False)

        self.window = graph.windows_present(self.conn)[0]
        graph.aggregate_window(self.conn, self.window)
        graph.analyze_window(self.conn, self.window, view="amp")
        graph.analyze_window(self.conn, self.window, view="amp", core=True)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _le(self, nome: str) -> list[dict]:
        with (self.destino / nome).open(encoding="utf-8") as arquivo:
            return list(csv.DictReader(arquivo))


class TestAtores(ExportTestCase):
    def test_colunas_em_ordem_fixa(self):
        """Diff de export entre duas semanas tem de mostrar mudança de DADO, não
        de ordem de coluna."""
        export.export_actors(self.conn, self.window, "amp", self.destino)
        with (self.destino / "actors.csv").open(encoding="utf-8") as arquivo:
            cabecalho = next(csv.reader(arquivo))
        self.assertEqual(cabecalho, export.ACTOR_COLUMNS)

    def test_ordenado_por_pagerank_decrescente(self):
        export.export_actors(self.conn, self.window, "amp", self.destino)
        valores = [float(l["pagerank"]) for l in self._le("actors.csv")]
        self.assertEqual(valores, sorted(valores, reverse=True))

    def test_in_degree_viaja_junto_do_pagerank(self):
        """PageRank sozinho engana: é herdado. Sem o in-degree na mesma linha,
        quem abre a planilha ordena por rank e conclui errado."""
        export.export_actors(self.conn, self.window, "amp", self.destino)
        for linha in self._le("actors.csv"):
            self.assertNotEqual(linha["in_degree_w"], "")

    def test_escopo_do_nucleo_exporta_menos(self):
        export.export_actors(self.conn, self.window, "amp", self.destino)
        cheio = len(self._le("actors.csv"))
        export.export_actors(self.conn, self.window, "amp:core", self.destino)
        self.assertLess(len(self._le("actors.csv")), cheio)


class TestComunidades(ExportTestCase):
    def test_traz_as_duas_medidas_de_ei(self):
        export.export_communities(self.conn, self.window, "amp", self.destino)
        linhas = self._le("communities.csv")
        self.assertTrue(linhas)
        com_escolha = [l for l in linhas if l["ei_choice"]]
        self.assertTrue(com_escolha, "nenhuma comunidade trouxe ei_choice")
        for linha in com_escolha:
            self.assertNotEqual(linha["ei_mean"], "")
            self.assertGreater(int(linha["choice_actors"]), 0)

    def test_pautas_saem_resolvidas_na_linha(self):
        """Quem abre o arquivo quer entender a comunidade, não fazer join."""
        export.export_communities(self.conn, self.window, "amp", self.destino)
        texto = " ".join(l["pautas"] for l in self._le("communities.csv"))
        self.assertIn("posse", texto)
        self.assertIn("%", texto)

    def test_ordenado_por_tamanho(self):
        export.export_communities(self.conn, self.window, "amp", self.destino)
        tamanhos = [int(l["size"]) for l in self._le("communities.csv")]
        self.assertEqual(tamanhos, sorted(tamanhos, reverse=True))


class TestArestas(ExportTestCase):
    def test_respeita_os_tipos_da_visao(self):
        """Exportar uma resposta sob o rótulo `amp` é a mesma mentira que o
        `dump` cometia: o grafo analisado não contém essa aresta."""
        export.export_edges(self.conn, self.window, "all", self.destino,
                            kinds=list(graph.VIEWS["amp"]))
        tipos = {l["kind"] for l in self._le("edges.csv")}
        self.assertTrue(tipos <= {"repost", "quote"}, f"vazou: {tipos}")

    def test_apenas_de_restringe_aos_atores_do_escopo(self):
        export.export_edges(self.conn, self.window, "all", self.destino,
                            kinds=list(graph.VIEWS["amp"]))
        cheio = len(self._le("edges.csv"))
        export.export_edges(self.conn, self.window, "all", self.destino,
                            kinds=list(graph.VIEWS["amp"]), apenas_de="amp:core")
        nucleo = self._le("edges.csv")
        self.assertLess(len(nucleo), cheio)
        self.assertFalse(any("solto" in l["src"] or "solto" in l["dst"]
                             for l in nucleo), "ator podado na exportação")


class TestManifesto(ExportTestCase):
    def _manifesto(self, scope="amp") -> dict:
        return export.manifest(self.conn, self.window, scope, {"actors": 1})

    def test_carrega_procedencia(self):
        """CSV solto não diz de onde veio. Esta POC gastou três mensagens
        interpretando dados de procedência não verificada."""
        m = self._manifesto()
        self.assertEqual(m["window_start"], self.window)
        self.assertTrue(m["dias_de_coleta"])
        self.assertIn("posse", m["termos"])
        self.assertIn("alckmin", m["termos"])

    def test_ressalvas_viajam_com_o_dado(self):
        """Número sem ressalva vira slide, e slide não tem rodapé."""
        texto = " ".join(self._manifesto()["ressalvas"])
        self.assertIn("ei_choice", texto)
        self.assertIn("PageRank", texto)
        self.assertIn("in_degree_w", texto)

    def test_explica_a_contagem_de_arestas(self):
        """Quem comparar o total do edges.csv com o número de arestas que o
        `analyze` reportou vai achar divergência. As duas contagens estão
        certas e contam coisas diferentes — sem isso escrito, parece bug."""
        texto = " ".join(self._manifesto()["ressalvas"])
        self.assertIn("edges.csv", texto)
        self.assertIn("MENOS", texto)

    def test_avisa_que_a_janela_e_curta(self):
        texto = " ".join(self._manifesto()["ressalvas"])
        self.assertIn("dia(s) de coleta", texto)

    def test_ressalva_do_nucleo_so_aparece_no_nucleo(self):
        """Ressalva que aparece sempre vira ruído e ninguém lê."""
        self.assertFalse(any("núcleo" in r for r in self._manifesto("amp")["ressalvas"]))
        self.assertTrue(any("núcleo" in r
                            for r in self._manifesto("amp:core")["ressalvas"]))

    def test_serializa_em_json(self):
        json.dumps(self._manifesto(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
