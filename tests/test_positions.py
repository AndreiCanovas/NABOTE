"""Testes do eixo de posicionamento.

O risco deste módulo não é quebrar — é rodar e devolver um número plausível e
errado. Uma decomposição mal deflacionada converge para a dimensão trivial e
produz um eixo que só reflete o TAMANHO de cada ator; a tabela sai bonita e não
mede posição nenhuma. Por isso os testes plantam a separação e exigem que ela
seja recuperada, em vez de checar que a função devolve floats.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from nabote import db, graph, ingest, positions  # noqa: E402
from synthetic import ListSource, planted_communities  # noqa: E402


class EixoTestCase(unittest.TestCase):
    """Dois blocos plantados que amplificam quase só para dentro."""

    BLOCOS, POR_BLOCO = 2, 20

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "p.db")
        db.migrate(self.conn, ROOT / "migrations")
        eventos, self.truth = planted_communities(
            n_communities=self.BLOCOS, per_community=self.POR_BLOCO,
            within_edges=6, bridges=1, seed=5)
        ingest.ingest(self.conn, ListSource(eventos, "blocos"), author_tier="A")
        self.window = graph.windows_present(self.conn)[0]
        graph.aggregate_window(self.conn, self.window)
        graph.analyze_window(self.conn, self.window, view="amp", core=True)
        self.scope = "amp:core"

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _por_bloco(self, escores):
        """{bloco plantado: [escores dos seus atores]}."""
        uid = {r["actor_id"]: r["platform_user_id"] for r in self.conn.execute(
            "SELECT actor_id, platform_user_id FROM actor")}
        saida: dict[int, list[float]] = {}
        for actor_id, score in escores.items():
            bloco = self.truth.get(uid[actor_id])
            if bloco is not None:
                saida.setdefault(bloco, []).append(score)
        return saida


class TestSeparacao(EixoTestCase):
    def test_o_eixo_separa_os_dois_blocos_plantados(self):
        """O teste central: as médias dos dois blocos ficam em lados opostos."""
        positions.compute_positions(self.conn, self.window, self.scope)
        escores = positions.positions_of(self.conn, self.window, self.scope)
        self.assertGreater(len(escores), 10, "quase ninguém recebeu posição")
        por_bloco = self._por_bloco(escores)
        self.assertEqual(len(por_bloco), 2)
        medias = {b: sum(v) / len(v) for b, v in por_bloco.items()}
        a, z = medias[0], medias[1]
        self.assertLess(a * z, 0, f"os blocos não ficaram em lados opostos: {medias}")
        self.assertGreater(abs(a - z), 0.5, f"separação fraca demais: {medias}")

    def test_nao_e_so_tamanho_disfarcado(self):
        """A dimensão trivial da AC ordena por massa. Se a deflação falhar, o
        escore vira função do grau e a correlação com ele fica quase perfeita."""
        positions.compute_positions(self.conn, self.window, self.scope)
        escores = positions.positions_of(self.conn, self.window, self.scope)
        grau = {r["src_actor_id"]: 0 for r in self.conn.execute(
            "SELECT DISTINCT src_actor_id FROM edge_window WHERE window_start=?",
            (self.window,))}
        for r in self.conn.execute(
            "SELECT src_actor_id, SUM(weight) AS w FROM edge_window "
            "WHERE window_start=? AND scope='all' GROUP BY src_actor_id", (self.window,)):
            grau[r["src_actor_id"]] = r["w"]
        pares = [(escores[a], grau.get(a, 0.0)) for a in escores]
        n = len(pares)
        mx = sum(p[0] for p in pares) / n
        my = sum(p[1] for p in pares) / n
        cov = sum((x - mx) * (y - my) for x, y in pares)
        sx = sum((x - mx) ** 2 for x, _ in pares) ** 0.5
        sy = sum((y - my) ** 2 for _, y in pares) ** 0.5
        r = cov / (sx * sy) if sx and sy else 0.0
        self.assertLess(abs(r), 0.7, f"escore virou proxy de volume (r={r:.2f})")

    def test_ancoras_sao_os_dois_extremos(self):
        positions.compute_positions(self.conn, self.window, self.scope)
        linhas = self.conn.execute(
            "SELECT actor_id, score, is_anchor FROM actor_position "
            "WHERE window_start=? AND scope=? ORDER BY score",
            (self.window, self.scope)).fetchall()
        self.assertEqual(sum(r["is_anchor"] for r in linhas), 2)
        self.assertEqual(linhas[0]["is_anchor"], 1)
        self.assertEqual(linhas[-1]["is_anchor"], 1)
        self.assertAlmostEqual(abs(linhas[0]["score"]), 1.0, places=6)

    def test_escala_fica_dentro_de_menos_um_e_mais_um(self):
        positions.compute_positions(self.conn, self.window, self.scope)
        for r in self.conn.execute(
            "SELECT score FROM actor_position WHERE window_start=? AND scope=?",
            (self.window, self.scope)):
            self.assertLessEqual(abs(r["score"]), 1.0 + 1e-9)


class TestDeterminismo(EixoTestCase):
    def test_duas_execucoes_dao_o_mesmo_eixo(self):
        """Iteração de potência parte de um vetor aleatório. Sem semente fixa a
        mesma janela sai espelhada ou embaralhada entre execuções, e a coluna
        'Δ posição' vira ruído."""
        positions.compute_positions(self.conn, self.window, self.scope)
        primeira = positions.positions_of(self.conn, self.window, self.scope)
        positions.compute_positions(self.conn, self.window, self.scope)
        segunda = positions.positions_of(self.conn, self.window, self.scope)
        self.assertEqual(set(primeira), set(segunda))
        for a in primeira:
            self.assertAlmostEqual(primeira[a], segunda[a], places=9)

    def test_o_sinal_e_ancorado_na_comunidade_zero(self):
        """Regra de orientação: a #0 fica do lado negativo. É o que impede que
        duas rodadas da mesma janela saiam com o eixo invertido."""
        positions.compute_positions(self.conn, self.window, self.scope)
        escores = positions.positions_of(self.conn, self.window, self.scope)
        zero = [s for a, s in escores.items() if self.conn.execute(
            "SELECT community_id FROM actor_community WHERE actor_id=? AND "
            "window_start=? AND scope=?", (a, self.window, self.scope)
        ).fetchone()["community_id"] == 0]
        self.assertTrue(zero)
        self.assertLess(sum(zero) / len(zero), 0)


class TestQuemNaoTemPosicao(EixoTestCase):
    def test_quem_amplificou_uma_conta_so_fica_de_fora(self):
        """Um ator com um alvo único cai exatamente em cima dele: a coordenada
        existe e não significa nada. Mesmo raciocínio do E-I com escolha."""
        matriz = positions._matriz(self.conn, self.window, "all")
        podada = positions._podar(matriz)
        fontes: dict[int, int] = {}
        for (i, _) in podada:
            fontes[i] = fontes.get(i, 0) + 1
        self.assertTrue(podada)
        for i, n in fontes.items():
            self.assertGreaterEqual(n, positions.MIN_ALVOS)

    def test_a_poda_vai_ate_o_ponto_fixo(self):
        """Uma passada só não basta: tirar um alvo pode deixar um amplificador
        com um alvo só, e assim por diante."""
        podada = positions._podar(positions._matriz(self.conn, self.window, "all"))
        self.assertEqual(podada, positions._podar(podada))

    def test_grafo_sem_escolha_nenhuma_devolve_vazio_em_vez_de_inventar(self):
        matriz = {(1, 100): 3.0, (2, 200): 5.0, (3, 300): 1.0}
        self.assertEqual(positions._podar(matriz), {})
        saida = positions._dimensao_1({})
        self.assertEqual(saida[0], {})


class TestMatrizConhecida(unittest.TestCase):
    """Caso fechado: duas metades perfeitamente separadas mais um atravessador.

    Sem banco. Se a AC estiver certa, as metades saem em lados opostos e quem
    amplifica os dois lados fica no meio — é a propriedade que o dossiê usa.
    """

    def test_duas_metades_e_um_atravessador(self):
        matriz: dict[tuple[int, int], float] = {}
        for i in range(1, 9):          # esquerda: amplifica os alvos 101 e 102
            matriz[(i, 101)] = 3.0
            matriz[(i, 102)] = 2.0
        for i in range(9, 17):         # direita: amplifica os alvos 201 e 202
            matriz[(i, 201)] = 3.0
            matriz[(i, 202)] = 2.0
        matriz[(99, 101)] = 2.0        # atravessador: um pé em cada lado
        matriz[(99, 201)] = 2.0

        escores, inercia, _ = positions._dimensao_1(matriz)
        self.assertGreater(inercia, 0.0)
        esq = sum(escores[i] for i in range(1, 9)) / 8
        dir_ = sum(escores[i] for i in range(9, 17)) / 8
        self.assertLess(esq * dir_, 0, "as metades não ficaram em lados opostos")
        self.assertLess(abs(escores[99]), min(abs(esq), abs(dir_)),
                        "o atravessador devia cair entre os dois blocos")

    def test_tudo_igual_nao_produz_eixo(self):
        """Matriz sem estrutura: toda linha com o mesmo perfil de coluna. A
        primeira dimensão não trivial não tem o que separar."""
        matriz = {(i, j): 1.0 for i in range(1, 9) for j in (101, 102, 103)}
        _, inercia, _ = positions._dimensao_1(matriz)
        self.assertLess(inercia, 1e-6, f"inventou estrutura onde não há: {inercia}")


if __name__ == "__main__":
    unittest.main()
