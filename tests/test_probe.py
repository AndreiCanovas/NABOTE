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
