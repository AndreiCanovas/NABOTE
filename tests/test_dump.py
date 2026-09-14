"""Testes do `dump`.

O `dump` é instrumento de depuração — é por ele que se decide se a coleta
prestou. Um dump que mente custa mais caro que dump nenhum, porque a conclusão
errada parece fundamentada.
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from nabote import cli, db, graph, ingest  # noqa: E402
from synthetic import ListSource, planted_communities, scattered_dyads  # noqa: E402


class DumpTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "d.db"
        conn = db.connect(self.path)
        db.migrate(conn, ROOT / "migrations")
        events, _ = planted_communities(n_communities=3, per_community=8)
        ingest.ingest(conn, ListSource(events), author_tier="A")
        ingest.ingest(conn, ListSource(scattered_dyads(30)), author_tier="C")
        self.window = graph.windows_present(conn)[0]
        graph.aggregate_window(conn, self.window)
        for view in ("amp", "reply"):
            graph.analyze_window(conn, self.window, view=view)
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def dump(self, **kwargs) -> str:
        args = argparse.Namespace(
            db=str(self.path), window=None, all=False, scope="all",
            view="amp", top=10, min_community=1, community=None)
        for key, value in kwargs.items():
            setattr(args, key, value)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.cmd_dump(args)
        self.assertEqual(code, 0)
        return buffer.getvalue()


class TestArestasRespeitamAVisao(DumpTestCase):
    """As respostas foram plantadas CRUZANDO comunidades. Se elas vazarem para a
    lista de arestas da visão `amp`, quem lê o dump atribui as comunidades a um
    sinal que não as produziu."""

    def _secao(self, saida: str) -> str:
        return saida.split("arestas mais pesadas")[1].split("peso total")[0]

    def test_amp_nao_lista_resposta(self):
        arestas = self._secao(self.dump(view="amp"))
        self.assertIn("-  repost->", arestas)
        self.assertNotIn("-   reply->", arestas)

    def test_reply_nao_lista_repost(self):
        arestas = self._secao(self.dump(view="reply"))
        self.assertIn("-   reply->", arestas)
        self.assertNotIn("-  repost->", arestas)

    def test_o_que_a_visao_exclui_continua_visivel(self):
        """Filtrar não pode virar esconder: o peso fora da visão é declarado."""
        saida = self.dump(view="amp")
        total = saida.split("peso total por tipo na janela inteira")[1]
        self.assertIn("reply", total)
        self.assertIn("(fora da visão)", total)


class TestFiltroDeComunidade(DumpTestCase):
    def test_min_community_esconde_a_cauda_e_a_declara(self):
        saida = self.dump(min_community=5, top=50)
        self.assertNotIn("(E-I mecânico)", saida)
        self.assertIn("comunidades de até", saida)
        self.assertIn("atores no total)", saida)

    def test_cauda_resumida_bate_com_o_total(self):
        """O resumo tem de fechar com a contagem: mostradas + cauda = todas."""
        saida = self.dump(min_community=5, top=50)
        total = int(saida.split("comunidades  (")[1].split(" no total")[0])
        mostradas = saida.count(" atores   E-I médio ")
        cauda = int(saida.split("… + ")[1].split(" comunidades")[0])
        self.assertEqual(mostradas + cauda, total)

    def test_community_recorta_a_lista_de_atores(self):
        saida = self.dump(community=0, top=50)
        self.assertIn("atores da comunidade #0", saida)
        linhas = [l for l in saida.splitlines() if l.startswith("did:plc:")]
        self.assertTrue(linhas, "nenhum ator listado")
        for linha in linhas:
            self.assertEqual(linha.split()[2], "0", f"ator de outra comunidade: {linha}")


if __name__ == "__main__":
    unittest.main()
