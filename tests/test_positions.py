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
        # a escala é normalizada pelo MAIOR módulo; qual das duas pontas o
        # atinge depende do dado, não da mecânica
        self.assertAlmostEqual(max(abs(linhas[0]["score"]), abs(linhas[-1]["score"])),
                               1.0, places=6)

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
        escores, _, diag = positions._dimensao_1({})
        self.assertEqual(escores, {})
        self.assertFalse(diag["convergiu"])


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

        escores, _, diag = positions._dimensao_1(matriz)
        self.assertGreater(diag["inercia"], 0.0)
        self.assertTrue(diag["convergiu"], "não convergiu num caso trivial")
        esq = sum(escores[i] for i in range(1, 9)) / 8
        dir_ = sum(escores[i] for i in range(9, 17)) / 8
        self.assertLess(esq * dir_, 0, "as metades não ficaram em lados opostos")
        self.assertLess(abs(escores[99]), min(abs(esq), abs(dir_)),
                        "o atravessador devia cair entre os dois blocos")

    def test_tudo_igual_nao_produz_eixo(self):
        """Matriz sem estrutura: toda linha com o mesmo perfil de coluna. A
        primeira dimensão não trivial não tem o que separar."""
        matriz = {(i, j): 1.0 for i in range(1, 9) for j in (101, 102, 103)}
        _, _, diag = positions._dimensao_1(matriz)
        self.assertLess(diag["inercia"], 1e-6,
                        f"inventou estrutura onde não há: {diag['inercia']}")


if __name__ == "__main__":
    unittest.main()


class TestConvergencia(unittest.TestCase):
    """O diagnóstico que faltava: bater no teto e convergir davam o mesmo número.

    Na base real a S03 gastou as 300 iterações do teto e reportou "300
    iterações", exatamente como reportaria se tivesse convergido na 300ª. O
    relatório receberia um vetor não convergido sem nenhuma marca.
    """

    def _matriz_facil(self):
        m = {}
        for i in range(1, 9):
            m[(i, 101)] = 3.0
            m[(i, 102)] = 2.0
        for i in range(9, 17):
            m[(i, 201)] = 3.0
            m[(i, 202)] = 2.0
        m[(99, 101)] = 2.0
        m[(99, 201)] = 2.0
        return m

    def test_caso_facil_converge_e_diz_que_convergiu(self):
        _, _, diag = positions._dimensao_1(self._matriz_facil())
        self.assertTrue(diag["convergiu"])
        self.assertLess(diag["iteracoes"], positions.MAX_ITER)
        self.assertLess(diag["residuo"], positions.TOL)

    def test_teto_baixo_e_reportado_como_nao_convergido(self):
        original = positions.MAX_ITER
        positions.MAX_ITER = 2
        try:
            _, _, diag = positions._dimensao_1(self._matriz_facil())
        finally:
            positions.MAX_ITER = original
        self.assertFalse(diag["convergiu"], "bateu no teto e disse que convergiu")
        self.assertEqual(diag["iteracoes"], 2)

    def test_fatia_da_inercia_e_uma_fracao_da_inercia_total(self):
        """σ₁² sozinho não diz se a dimensão 1 explica muito ou pouco. A fatia
        precisa do denominador, que é χ²/N."""
        _, _, diag = positions._dimensao_1(self._matriz_facil())
        self.assertGreater(diag["inercia_total"], 0.0)
        self.assertLessEqual(diag["inercia"], diag["inercia_total"] + 1e-9)
        self.assertAlmostEqual(diag["fatia_inercia"],
                               diag["inercia"] / diag["inercia_total"], places=9)
        self.assertLessEqual(diag["fatia_inercia"], 1.0 + 1e-9)

    def test_inercia_total_bate_com_a_definicao_direta(self):
        """χ²/N calculado célula a célula, sem o atalho Σa²−1."""
        m = self._matriz_facil()
        total = sum(m.values())
        linhas = sorted({i for i, _ in m})
        colunas = sorted({j for _, j in m})
        r = {i: sum(w for (a, _), w in m.items() if a == i) / total for i in linhas}
        c = {j: sum(w for (_, b), w in m.items() if b == j) / total for j in colunas}
        direto = 0.0
        for i in linhas:
            for j in colunas:
                p = m.get((i, j), 0.0) / total
                esperado = r[i] * c[j]
                direto += (p - esperado) ** 2 / esperado
        _, _, diag = positions._dimensao_1(m)
        self.assertAlmostEqual(diag["inercia_total"], direto, places=9)


class TestFormaDoDiagnostico(unittest.TestCase):
    """Os três caminhos de saída precisam das mesmas chaves.

    O CLI lê eixo['residuo'] para avisar sobre não convergência; um caminho que
    omite a chave derruba o comando com KeyError em vez de reportar o problema
    — que foi exatamente o que aconteceu ao acrescentar o aviso.
    """

    ESPERADAS = (set(positions.DIAG_VAZIO) | set(positions.VAZIO_EXTRA)
                 | {"scope", "atores", "alvos", "descartados"})

    def _chaves(self, conn, window, scope):
        return set(positions.compute_positions(conn, window, scope, persist=False))

    def test_todos_os_retornos_tem_as_mesmas_chaves(self):
        import tempfile
        from nabote import db as dbmod, graph as gmod, ingest as imod
        from synthetic import ListSource, planted_communities

        with tempfile.TemporaryDirectory() as tmp:
            conn = dbmod.connect(Path(tmp) / "f.db")
            dbmod.migrate(conn, ROOT / "migrations")
            # caminho 1: escopo sem aresta nenhuma
            vazio = self._chaves(conn, "2026-09-14", "amp:core")
            self.assertEqual(vazio - {"ancoras"}, self.ESPERADAS)

            eventos, _ = planted_communities(n_communities=2, per_community=14, seed=5)
            imod.ingest(conn, ListSource(eventos, "b"), author_tier="A")
            w = gmod.windows_present(conn)[0]
            gmod.aggregate_window(conn, w)
            gmod.analyze_window(conn, w, view="amp", core=True)
            # caminho 2: solução de verdade
            cheio = self._chaves(conn, w, "amp:core")
            self.assertEqual(cheio - {"ancoras"}, self.ESPERADAS)
            conn.close()


class TestRedundanciaComAParticao(EixoTestCase):
    """A pergunta que decide se a seção do eixo vale existir.

    Se o sinal do escore prevê a comunidade com quase 100% de acerto, o eixo
    não acrescenta nada ao que o Leiden já disse, e apresentá-lo como medida
    independente venderia duas vezes o mesmo achado.
    """

    def test_em_blocos_plantados_o_eixo_concorda_com_a_particao(self):
        """Neste fixture os blocos SÃO a estrutura, então a concordância alta é
        o resultado correto — e é justamente por isso que ela precisa ser
        medida e reportada, não presumida baixa."""
        d = positions.compute_positions(self.conn, self.window, self.scope)
        self.assertIsNotNone(d["concordancia"])
        self.assertGreater(d["n_comparados"], 0)
        self.assertGreater(d["concordancia"], 0.8)
        self.assertEqual(len(d["maiores"]), 2)
        for m in d["maiores"]:
            self.assertEqual(m["n"], m["neg"] + m["pos"])

    def test_concordancia_nao_depende_do_sinal_escolhido(self):
        """O rótulo do lado é convenção. Se a medida dependesse dele, inverter
        o eixo transformaria 95% de acerto em 5%."""
        escores = {1: -0.9, 2: -0.8, 3: 0.7, 4: 0.9}
        com = {1: 0, 2: 0, 3: 1, 4: 1}
        a = positions._concordancia(escores, com)
        b = positions._concordancia({k: -v for k, v in escores.items()}, com)
        self.assertEqual(a["concordancia"], b["concordancia"])
        self.assertEqual(a["concordancia"], 1.0)

    def test_eixo_ortogonal_a_particao_da_concordancia_de_meio_a_meio(self):
        escores = {1: -0.9, 2: 0.8, 3: -0.7, 4: 0.9}
        com = {1: 0, 2: 0, 3: 1, 4: 1}
        d = positions._concordancia(escores, com)
        self.assertEqual(d["concordancia"], 0.5)

    def test_uma_comunidade_so_nao_da_para_comparar(self):
        d = positions._concordancia({1: -0.5, 2: 0.5}, {1: 0, 2: 0})
        self.assertIsNone(d["concordancia"])

    def test_cobertura_da_particao_e_declarada(self):
        """O eixo sai do grafo cheio da pauta; a partição, do núcleo. São
        filtros diferentes e a interseção precisa aparecer antes que alguém
        leia 'eixo médio da comunidade' como cobrindo todos."""
        d = positions.compute_positions(self.conn, self.window, self.scope)
        self.assertLessEqual(d["com_comunidade"], d["atores"])
        self.assertGreater(d["atores_na_particao"], 0)


class TestQuemRecebePosicao(EixoTestCase):
    """Numa rede de amplificação, quem tem PageRank alto é o AMPLIFICADO.

    A primeira versão devolvia só coordenadas de linha — de quem amplifica — e
    a tabela de atores do dossiê saiu com a posição vazia em 11 dos 12 perfis.
    O eixo existia, convergia, tinha σ₁ = 0,99, e cobria a população errada.
    """

    def test_os_amplificados_tambem_recebem_posicao(self):
        d = positions.compute_positions(self.conn, self.window, self.scope)
        self.assertGreater(d["amplificados"], 0, "nenhum alvo recebeu posição")
        self.assertGreater(d["amplificadores"], 0)

    def test_os_mais_centrais_tem_posicao(self):
        """O teste que a versão quebrada teria reprovado: os atores que o
        relatório mostra são os de maior PageRank, e eles precisam de posição."""
        positions.compute_positions(self.conn, self.window, self.scope)
        escores = positions.positions_of(self.conn, self.window, self.scope)
        topo = [r["actor_id"] for r in self.conn.execute(
            "SELECT actor_id FROM actor_metric WHERE window_start=? AND scope=? "
            "AND metric='pagerank' ORDER BY value DESC LIMIT 12",
            (self.window, self.scope))]
        self.assertTrue(topo)
        com_posicao = [a for a in topo if a in escores]
        self.assertGreaterEqual(
            len(com_posicao), len(topo) * 0.7,
            f"só {len(com_posicao)} de {len(topo)} dos mais centrais têm posição")

    def test_o_papel_fica_gravado_no_metodo(self):
        """Linha e coluna são estimativas de qualidade diferente: a coluna vem
        de muitos amplificadores, a linha de poucos alvos. Quem lê precisa
        saber qual das duas está vendo."""
        positions.compute_positions(self.conn, self.window, self.scope)
        metodos = {r["method"] for r in self.conn.execute(
            "SELECT DISTINCT method FROM actor_position WHERE window_start=? AND scope=?",
            (self.window, self.scope))}
        self.assertTrue(metodos)
        for m in metodos:
            self.assertTrue(m.startswith(positions.METHOD + "/"), m)
        self.assertIn(positions.METHOD + "/amplificado", metodos)

    def test_linha_e_coluna_ficam_na_mesma_escala(self):
        """Numa AC as duas coordenadas principais têm a mesma variância ao
        longo da dimensão. Se não tivessem, misturá-las num eixo só seria
        sobrepor dois gráficos de escalas diferentes e chamar de biplot."""
        matriz = {}
        for i in range(1, 9):
            matriz[(i, 101)] = 3.0
            matriz[(i, 102)] = 2.0
        for i in range(9, 17):
            matriz[(i, 201)] = 3.0
            matriz[(i, 202)] = 2.0
        linha, coluna, diag = positions._dimensao_1(matriz)
        def var(d):
            vals = list(d.values())
            m = sum(vals) / len(vals)
            return sum((x - m) ** 2 for x in vals) / len(vals)
        self.assertAlmostEqual(var(linha), var(coluna), places=6)
        self.assertAlmostEqual(var(linha), diag["inercia"], places=6)
