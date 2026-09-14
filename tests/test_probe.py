"""Testes do inspetor de bases externas.

Constrói um zip com parquet no mesmo formato do dataset brasileiro (arquivos
nomeados por data e termo de busca) e verifica leitura sem descompactar.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nabote import probe  # noqa: E402


def _parquet_bytes(**colunas) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(pa.table(colunas), buf)
    return buf.getvalue()


class ProbeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.zip_path = Path(self._tmp.name) / "base.zip"
        pequeno = _parquet_bytes(id=[1], text=["curto"], created_at=["2023-01-01T00:00:00Z"])
        grande = _parquet_bytes(
            id=[1, 2, 3],
            text=["a" * 200, None, "com\nquebra"],
            created_at=["2023-01-08T10:00:00Z"] * 3,
        )
        with zipfile.ZipFile(self.zip_path, "w") as zf:
            zf.writestr("2023-01-08-#GolpeDeEstado.parquet", grande)
            zf.writestr("2023-01-01-Alckmin.parquet", pequeno)
            zf.writestr("leiame.txt", "não é parquet")

    def tearDown(self):
        self._tmp.cleanup()

    def test_lists_only_parquet_members_sorted(self):
        membros = probe.list_zip_members(self.zip_path)
        nomes = [n for n, _ in membros]
        self.assertEqual(nomes, ["2023-01-01-Alckmin.parquet",
                                 "2023-01-08-#GolpeDeEstado.parquet"])
        self.assertTrue(all(tam > 0 for _, tam in membros))

    def test_reads_member_without_extracting(self):
        tabela = probe.read_parquet_member(self.zip_path, "2023-01-01-Alckmin.parquet")
        self.assertEqual(tabela.num_rows, 1)
        self.assertIn("text", tabela.schema.names)

    def test_describe_reports_schema_nulls_and_sample(self):
        tabela = probe.read_parquet_member(self.zip_path, "2023-01-08-#GolpeDeEstado.parquet")
        saida = probe.describe(tabela, sample_rows=2)
        self.assertIn("linhas: 3", saida)
        self.assertIn("created_at", saida)
        self.assertIn("33%", saida)          # uma das três linhas de `text` é nula
        self.assertIn("linha 0", saida)

    def test_long_and_multiline_values_do_not_break_the_table(self):
        """Post real tem quebra de linha e é longo; a saída precisa continuar colável."""
        tabela = probe.read_parquet_member(self.zip_path, "2023-01-08-#GolpeDeEstado.parquet")
        saida = probe.describe(tabela, sample_rows=3)
        for linha in saida.splitlines():
            self.assertLess(len(linha), 140, f"linha larga demais: {linha[:60]}")
        self.assertIn("⏎", saida)            # quebra virou símbolo, não quebrou a linha
        self.assertIn("∅", saida)            # nulo tem marca visível


if __name__ == "__main__":
    unittest.main()


class TestDiagnose(unittest.TestCase):
    """O diagnóstico decide se dá para montar grafo. Falso positivo aqui manda
    escrever adaptador contra coluna que não existe — daí o casamento exato."""

    @staticmethod
    def _tabela(**colunas):
        import pyarrow as pa
        return pa.table({k: [v] for k, v in colunas.items()})

    def test_exact_match_avoids_substring_false_positives(self):
        # "id" não pode casar com referenced_tweets_author_id
        saida = probe.diagnose(self._tabela(
            id=1, author_id=1, text="t", created_at="2023",
            referenced_tweets="[]", referenced_tweets_author_id=9))
        linha_id = [l for l in saida.splitlines() if "id do post" in l][0]
        self.assertIn("id", linha_id)
        self.assertNotIn("referenced_tweets_author_id", linha_id)

    def test_direct_graph_when_referenced_author_present(self):
        saida = probe.diagnose(self._tabela(
            id=1, author_id=1, text="t", created_at="2023",
            referenced_tweets_author_id=9))
        self.assertIn("GRAFO DIRETO", saida)

    def test_mentions_fallback_when_no_referenced_author(self):
        saida = probe.diagnose(self._tabela(
            id=1, author_id=1, text="t", created_at="2023", entities="{}"))
        self.assertIn("GRAFO POR MENÇÃO", saida)

    def test_partial_when_only_referenced_tweet_id(self):
        saida = probe.diagnose(self._tabela(
            id=1, author_id=1, text="t", created_at="2023", referenced_tweets="[]"))
        self.assertIn("GRAFO PARCIAL", saida)
        self.assertIn("RT @usuario:", saida)

    def test_no_edges_at_all_is_stated_plainly(self):
        saida = probe.diagnose(self._tabela(
            id=1, author_id=1, text="t", created_at="2023"))
        self.assertIn("SEM ARESTAS", saida)

    def test_missing_essentials_blocks_before_anything_else(self):
        saida = probe.diagnose(self._tabela(text="t", created_at="2023"))
        self.assertIn("BLOQUEIA", saida)
        self.assertNotIn("GRAFO", saida)

    def test_warns_when_author_has_no_handle(self):
        """Sem handle, o relatório fica ilegível e não cruza com a lista curada."""
        sem = probe.diagnose(self._tabela(
            id=1, author_id=1, text="t", created_at="2023",
            referenced_tweets_author_id=9))
        com = probe.diagnose(self._tabela(
            id=1, author_id=1, username="x", text="t", created_at="2023",
            referenced_tweets_author_id=9))
        self.assertIn("ATENÇÃO", sem)
        self.assertNotIn("ATENÇÃO", com)
