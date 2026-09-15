"""Testes do dossiê.

Cada teste aqui existe por uma armadilha concreta:

  o mapa    pode devolver 60 nós de 34 mil e parecer o grafo inteiro;
  as pontes podem confundir "fala com os dois lados" com "é o caminho";
  a coamp.  pode explodir num post viral e chamar viralidade de coordenação;
  o n-grama pode devolver o próprio termo da coleta como "enquadramento".
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from nabote import db, dossie, graph, ingest  # noqa: E402
from nabote.events import NormalizedEvent, Target  # noqa: E402
from synthetic import ListSource, planted_communities  # noqa: E402


class Fonte:
    def __init__(self, nome, eventos):
        self.name, self._eventos, self.skipped = nome, eventos, 0

    def events(self, cursor=None):
        yield from self._eventos


# Vocabulário próprio de cada comunidade, combinado em frases DIFERENTES.
# O fixture antigo repetia um texto só por comunidade — que é exatamente a
# patologia do retuíte que `min_textos` existe para barrar, e com ela o teste
# media a capacidade de contar um texto repetido, não de achar enquadramento.
VOCAB = {
    0: "desnutricao garimpo ilegal invasao terra demarcacao mineracao".split(),
    1: "voluntarios solidariedade doacao mutirao logistica atendimento".split(),
}
NEUTRO = "roraima norte regiao area local semana relato registro imagem".split()


def _texto(com, i):
    v = VOCAB[com]
    n = NEUTRO
    return " ".join([v[i % len(v)], n[i % len(n)], v[(i + 2) % len(v)],
                     n[(i + 3) % len(n)], v[(i + 4) % len(v)],
                     n[(i + 5) % len(n)], v[(i + 1) % len(v)]])


def _post(uid, alvo, pid, quando, texto=None, tipo="repost"):
    return NormalizedEvent(
        platform="bluesky", kind="post", actor_uid=uid, occurred_at=quando,
        post_uid=pid, post_type=tipo, text=texto,
        targets=[Target(kind="repost", uid=alvo)])


class DossieTestCase(unittest.TestCase):
    PAUTA = "Yanomami"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "d.db")
        db.migrate(self.conn, ROOT / "migrations")
        eventos, self.truth = planted_communities(
            n_communities=2, per_community=18, within_edges=6, bridges=2, seed=9)
        ingest.ingest(self.conn, ListSource(eventos, "nucleo"), author_tier="A")

        # a coleta da pauta: cada comunidade fala do tema com vocabulário próprio
        # Espalhados no tempo de propósito: 80 posts no mesmo segundo produzem
        # um cluster de "coamplificação" perfeito que é puro artefato do
        # fixture, e testes de coordenação passariam em cima dele sem tocar no
        # que plantaram.
        inicio = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
        rotulados = []
        for com in (0, 1):
            for i in range(40):
                # Três alvos por comunidade, não um. Com um alvo só, todo
                # amplificador tem escolha zero e o eixo é incomputável por
                # construção — o que é o comportamento certo do código e o
                # fixture errado para testar posicionamento.
                # 14 e 3 são coprimos de propósito: com 15 e 3 (que têm fator
                # comum) cada amplificador cai SEMPRE no mesmo alvo, todo mundo
                # fica com escolha zero e o eixo volta vazio.
                rotulados.append(_post(
                    f"did:plc:sint{com:02d}{(i % 14) + 1:03d}",
                    f"did:plc:sint{com:02d}{(i % 3):03d}",
                    f"p{com}-{i}",
                    (inicio + timedelta(minutes=7 * i + com)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    texto=f"{self.PAUTA} {_texto(com, i)}"))
        ingest.ingest(self.conn, Fonte("x:pauta", rotulados), kind="campanha",
                      campaign_label=self.PAUTA, author_tier="C", resume=False)

        self.window = graph.windows_present(self.conn)[0]
        self.edge_scope = f"topic:{self.PAUTA}"
        graph.aggregate_window(self.conn, self.window)
        graph.aggregate_window(self.conn, self.window, scope=self.edge_scope)
        graph.analyze_window(self.conn, self.window, view="amp",
                             edge_scope=self.edge_scope)
        self.scope = dossie.topic_scope("amp", self.PAUTA, core=False)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestEscopo(DossieTestCase):
    def test_o_nome_do_escopo_bate_com_o_que_analyze_gravou(self):
        """Se estes dois divergirem, todo o dossiê lê um escopo vazio e
        devolve zeros sem erro nenhum."""
        gravados = {r["scope"] for r in self.conn.execute(
            "SELECT DISTINCT scope FROM actor_community WHERE window_start = ?",
            (self.window,))}
        self.assertIn(self.scope, gravados)

    def test_ida_e_volta_do_escopo(self):
        self.assertEqual(graph.edge_scope_of(self.scope), self.edge_scope)
        self.assertEqual(
            graph.edge_scope_of(dossie.topic_scope("amp", self.PAUTA, core=True)),
            self.edge_scope)


class TestResumo(DossieTestCase):
    def test_conta_atores_e_posts_da_pauta_e_nao_da_semana(self):
        r = dossie.window_summary(self.conn, self.window, self.scope)
        self.assertEqual(r["posts"], 80)
        self.assertGreater(r["atores"], 0)
        cheio = self.conn.execute(
            "SELECT COUNT(*) AS n FROM post WHERE created_at >= ?", (self.window,)
        ).fetchone()["n"]
        self.assertLess(r["posts"], cheio, "trouxe a semana inteira")

    def test_mistura_de_arestas_soma_um(self):
        r = dossie.window_summary(self.conn, self.window, self.scope)
        self.assertAlmostEqual(sum(r["mistura"].values()), 1.0, places=9)

    def test_custo_nao_e_multiplicado_pelo_numero_de_posts(self):
        """Somar cost_usd na junção com post multiplica o custo do run pela
        quantidade de posts dele — erro que infla o custo em 80x aqui."""
        self.conn.execute("UPDATE collection_run SET cost_usd = 0.25 WHERE campaign_label = ?",
                          (self.PAUTA,))
        r = dossie.window_summary(self.conn, self.window, self.scope)
        self.assertAlmostEqual(r["custo_usd"], 0.25, places=6)


class TestMapa(DossieTestCase):
    def test_diz_quantos_ficaram_de_fora(self):
        m = dossie.network_map(self.conn, self.window, self.scope, limit=5)
        self.assertEqual(len(m["nos"]), 5)
        self.assertGreater(m["de"], 5, "não registrou o tamanho real do grafo")
        self.assertLessEqual(m["cobertura_peso"], 1.0)

    def test_coordenadas_normalizadas(self):
        m = dossie.network_map(self.conn, self.window, self.scope, limit=12)
        for n in m["nos"]:
            self.assertGreaterEqual(n["x"], 0.0)
            self.assertLessEqual(n["x"], 1.0)
            self.assertGreaterEqual(n["y"], 0.0)
            self.assertLessEqual(n["y"], 1.0)

    def test_layout_e_deterministico(self):
        """Fruchterman-Reingold é estocástico. Sem semente, duas execuções da
        mesma janela produzem mapas diferentes e incomparáveis."""
        a = dossie.network_map(self.conn, self.window, self.scope, limit=20)
        b = dossie.network_map(self.conn, self.window, self.scope, limit=20)
        for na, nb in zip(a["nos"], b["nos"]):
            self.assertEqual(na["actor_id"], nb["actor_id"])
            self.assertAlmostEqual(na["x"], nb["x"], places=9)
            self.assertAlmostEqual(na["y"], nb["y"], places=9)

    def test_so_entram_arestas_entre_nos_desenhados(self):
        m = dossie.network_map(self.conn, self.window, self.scope, limit=8)
        n = len(m["nos"])
        for e in m["arestas"]:
            self.assertLess(e["de"], n)
            self.assertLess(e["para"], n)


class TestPontes(DossieTestCase):
    def test_ponte_precisa_de_peso_atravessando(self):
        m = dossie.network_map(self.conn, self.window, self.scope, limit=40)
        for p in dossie.bridges(m):
            self.assertGreater(p["peso_externo"], 0.0)

    def test_articulacao_vem_antes_de_peso(self):
        """Ser o caminho é afirmação mais forte que falar com os dois lados.

        Mapa montado à mão porque o caso precisa existir para ser testado: X
        atravessa com peso alto mas o grafo aguenta perdê-lo; P atravessa com
        peso baixo e é o único caminho entre os dois lados."""
        mapa = {"nos": [
            {"actor_id": 1, "handle": "a1", "comunidade": 0, "ei": 0, "pagerank": 0.1},
            {"actor_id": 2, "handle": "a2", "comunidade": 0, "ei": 0, "pagerank": 0.1},
            {"actor_id": 3, "handle": "X",  "comunidade": 0, "ei": 0, "pagerank": 0.1},
            {"actor_id": 4, "handle": "b1", "comunidade": 1, "ei": 0, "pagerank": 0.1},
            {"actor_id": 5, "handle": "b2", "comunidade": 1, "ei": 0, "pagerank": 0.1},
            {"actor_id": 6, "handle": "P",  "comunidade": 1, "ei": 0, "pagerank": 0.1},
            {"actor_id": 7, "handle": "c1", "comunidade": 2, "ei": 0, "pagerank": 0.1},
        ], "arestas": [
            {"de": 0, "para": 1, "peso": 5.0}, {"de": 1, "para": 2, "peso": 5.0},
            {"de": 2, "para": 3, "peso": 40.0},   # X → b1: peso alto, redundante
            {"de": 0, "para": 4, "peso": 9.0},    # a1 → b2: segundo caminho
            {"de": 3, "para": 4, "peso": 5.0},
            {"de": 4, "para": 5, "peso": 5.0},
            {"de": 5, "para": 6, "peso": 2.0},    # P → c1: único caminho para a #2
        ]}
        pontes = dossie.bridges(mapa, top=10)
        nomes = [p["handle"] for p in pontes]
        self.assertIn("P", nomes)
        self.assertIn("X", nomes)
        self.assertTrue([p for p in pontes if p["handle"] == "P"][0]["articulacao"])
        self.assertGreater(
            [p for p in pontes if p["handle"] == "X"][0]["peso_externo"],
            [p for p in pontes if p["handle"] == "P"][0]["peso_externo"],
            "o caso só testa a ordenação se X tiver mais peso que P")
        self.assertLess(nomes.index("P"), nomes.index("X"),
                        "peso ganhou de articulação")

    def test_grafo_vazio_nao_quebra(self):
        self.assertEqual(dossie.bridges({"nos": [], "arestas": []}), [])


class TestCoamplificacao(DossieTestCase):
    def _plantar(self, alvo, contas, base, passo):
        inicio = datetime(2026, 9, 15, 13, 0, 0, tzinfo=timezone.utc)
        eventos = [_post(c, alvo, f"co{alvo}-{c}-{base}",
                         (inicio + timedelta(seconds=base + i * passo))
                         .strftime("%Y-%m-%dT%H:%M:%SZ"))
                   for i, c in enumerate(contas)]
        ingest.ingest(self.conn, Fonte(f"co{alvo}{base}", eventos), kind="campanha",
                      campaign_label=self.PAUTA, author_tier="C", resume=False)

    def test_acha_quem_amplifica_junto(self):
        """Exige AS CONTAS PLANTADAS, não um cluster qualquer: um artefato do
        fixture satisfaria 'achou algum cluster' sem provar nada."""
        dids = [f"did:plc:sint00{i:03d}" for i in (1, 2, 3)]
        for base in (0, 600, 1200, 1800):
            self._plantar("did:plc:alvo0001", dids, base, 1)
        esperado = {r["actor_id"] for r in self.conn.execute(
            "SELECT actor_id FROM actor WHERE platform_user_id IN (?,?,?)", dids)}
        r = dossie.coamplification(self.conn, self.window, self.scope, min_pares=3)
        achou = [c for c in r["clusters"] if esperado <= set(c["contas"])]
        self.assertTrue(achou, f"as 3 contas plantadas não saíram juntas: {r['clusters']}")
        self.assertLessEqual(achou[0]["mediana_s"], dossie.COAMP_SEGUNDOS)

    def test_viralidade_em_massa_fica_de_fora_e_e_declarada(self):
        """5.000 contas no mesmo minuto geram 12 milhões de pares e zero
        informação. O corte existe; o que ele cortou tem de aparecer."""
        contas = [f"did:plc:viral{i:04d}" for i in range(80)]
        self._plantar("did:plc:sint01000", contas, 0, 0)
        r = dossie.coamplification(self.conn, self.window, self.scope, grupo_max=50)
        self.assertGreaterEqual(r["grupos_ignorados"], 1)
        for c in r["clusters"]:
            self.assertLess(c["n_contas"], 80)

    def test_fora_da_janela_de_tempo_nao_conta(self):
        """Os mesmos eventos, duas janelas: com 30s o grupo aparece, com 5s não.

        Só olhar a janela estreita passaria vazio — zero clusters satisfaz
        qualquer asserção sobre clusters. O par de execuções é o que prova que
        é o limite de tempo agindo, e não o dado estar ausente."""
        contas = [f"did:plc:sint00{i:03d}" for i in (4, 5, 6)]
        for base in (0, 120, 240, 360):
            self._plantar("did:plc:alvo0002", contas, base, 10)  # 10s entre vizinhos
        larga = dossie.coamplification(self.conn, self.window, self.scope,
                                       segundos=30, min_pares=3)
        self.assertTrue(larga["clusters"], "janela larga devia ter achado o grupo")
        estreita = dossie.coamplification(self.conn, self.window, self.scope,
                                          segundos=5, min_pares=3)
        self.assertEqual(estreita["clusters"], [],
                         "10s de intervalo não pode passar numa janela de 5s")

    def test_declara_que_a_chave_e_o_ator_e_nao_o_post(self):
        r = dossie.coamplification(self.conn, self.window, self.scope)
        self.assertEqual(r["chave"], "ator-alvo")


class TestSubPautas(DossieTestCase):
    def test_separa_o_vocabulario_de_cada_comunidade(self):
        r = dossie.subtopics(self.conn, self.window, self.scope,
                             min_posts=5, min_textos=3)
        por_com = {}
        for linha in r["linhas"]:
            por_com.setdefault(linha["comunidade"], set()).add(linha["termo"])
        self.assertGreaterEqual(len(por_com), 2, "não separou as comunidades")
        # O que importa não é o termo ser globalmente único — duas
        # sub-comunidades do mesmo lado podem partilhar vocabulário — e sim os
        # DOIS vocabulários plantados não se misturarem numa comunidade só.
        for cid, termos in por_com.items():
            lados = {lado for lado, vocab in VOCAB.items()
                     for t in termos if set(t.split()) & set(vocab)}
            self.assertLessEqual(len(lados), 1,
                                 f"comunidade {cid} misturou os dois vocabulários: {termos}")

    def test_nao_devolve_o_proprio_termo_da_coleta(self):
        """Yanomami está em 100% dos posts por construção da coleta. Se ele
        aparecer como 'enquadramento distintivo', a seção inteira é tautologia."""
        r = dossie.subtopics(self.conn, self.window, self.scope,
                             min_posts=5, min_textos=3)
        for linha in r["linhas"]:
            self.assertNotIn("yanomami", linha["termo"])

    def test_lift_acima_de_um_significa_desproporcional(self):
        r = dossie.subtopics(self.conn, self.window, self.scope,
                             min_posts=5, min_textos=3)
        self.assertTrue(r["linhas"])
        for linha in r["linhas"]:
            self.assertGreaterEqual(linha["lift"], dossie.NGRAM_MIN_LIFT)
            self.assertLessEqual(linha["share"], 1.0)

    def test_nao_repete_pedaco_de_termo_maior(self):
        r = dossie.subtopics(self.conn, self.window, self.scope,
                             min_posts=5, min_textos=3)
        por_com = {}
        for linha in r["linhas"]:
            por_com.setdefault(linha["comunidade"], []).append(linha["termo"])
        for termos in por_com.values():
            for a in termos:
                for b in termos:
                    if a is not b:
                        self.assertNotIn(a, b)

    def test_corpus_sem_texto_devolve_vazio_em_vez_de_quebrar(self):
        self.conn.execute("UPDATE post SET text = NULL")
        r = dossie.subtopics(self.conn, self.window, self.scope,
                             min_posts=1, min_textos=1)
        self.assertEqual(r["linhas"], [])
        self.assertEqual(r["posts_com_texto"], 0)
        self.assertGreater(r["posts"], 0, "precisa dizer quantos posts examinou")


if __name__ == "__main__":
    unittest.main()


class TestAtoresEComunidades(DossieTestCase):
    def test_a_tabela_de_atores_traz_as_metricas_que_existem(self):
        """`in_degree_w` é o nome real da métrica. Um nome errado aqui devolve
        None em toda a coluna e a tabela sai vazia sem erro nenhum."""
        linhas = dossie.top_actors(self.conn, self.window, self.scope, limit=5)
        self.assertTrue(linhas)
        for r in linhas:
            self.assertIsNotNone(r["pagerank"])
            self.assertIsNotNone(r["in_degree"], "in_degree veio vazio: nome da métrica")
            self.assertIsNotNone(r["comunidade"])

    def test_atores_vem_ordenado_por_pagerank(self):
        linhas = dossie.top_actors(self.conn, self.window, self.scope, limit=8)
        valores = [r["pagerank"] for r in linhas]
        self.assertEqual(valores, sorted(valores, reverse=True))

    def test_delta_e_de_POSTO_e_nao_de_escore(self):
        """O Δ do escore cru não é comparável entre janelas: cada uma
        renormaliza pelo próprio extremo e ancora o sinal na própria #0. Na S04
        isso deu Δ ≈ +0,287 idêntico para seis perfis de comunidades
        diferentes — reescala, não movimento. O posto sobrevive a isso."""
        from nabote import positions
        positions.compute_positions(self.conn, self.window, self.scope)
        linhas = dossie.top_actors(self.conn, self.window, self.scope, limit=5)
        for r in linhas:
            self.assertNotIn("delta_eixo", r)
            self.assertIsNone(r["delta_posto"], "sem janela anterior não há Δ")
            if r["posto"] is not None:
                self.assertGreaterEqual(r["posto"], 0.0)
                self.assertLessEqual(r["posto"], 1.0)

    def test_o_posto_e_imune_a_reescala(self):
        """Duplicar todos os escores muda todo Δ de escore e nenhum posto."""
        a = dossie._postos({1: -0.9, 2: 0.1, 3: 0.5})
        b = dossie._postos({1: -1.8, 2: 0.2, 3: 1.0})
        self.assertEqual(a, b)
        self.assertEqual(sorted(a.values()), [0.0, 0.5, 1.0])

    def test_a_segunda_dimensao_chega_na_tabela(self):
        """Quando a dimensão 1 degenera em indicador de bloco, é a 2 que
        carrega posicionamento. Se ela não chegar à tabela, a seção fica sem
        nada para mostrar."""
        from nabote import positions
        positions.compute_positions(self.conn, self.window, self.scope)
        linhas = dossie.top_actors(self.conn, self.window, self.scope, limit=5)
        self.assertTrue(linhas)
        for r in linhas:
            self.assertIn("eixo2", r)
        self.assertTrue(any(r["eixo2"] is not None for r in linhas),
                        "nenhum ator recebeu a dimensão 2")

    def test_cartoes_de_comunidade_juntam_eixo_e_termos(self):
        from nabote import positions
        positions.compute_positions(self.conn, self.window, self.scope)
        sub = dossie.subtopics(self.conn, self.window, self.scope, min_posts=5)
        cards = dossie.community_rows(self.conn, self.window, self.scope, sub)
        self.assertTrue(cards)
        self.assertEqual([c["atores"] for c in cards],
                         sorted([c["atores"] for c in cards], reverse=True))
        self.assertTrue(any(c["termos"] for c in cards), "nenhum cartão recebeu termo")


class TestSnapshot(DossieTestCase):
    def test_monta_todas_as_secoes_do_dossie(self):
        from nabote import positions
        positions.compute_positions(self.conn, self.window, self.scope)
        s = dossie.snapshot(self.conn, self.window, self.PAUTA, core=False, mapa_top=20)
        for chave in ("resumo", "mapa", "pontes", "atores", "comunidades",
                      "subpautas", "coamplificacao"):
            self.assertIn(chave, s)
        self.assertEqual(s["pauta"], self.PAUTA)
        self.assertEqual(s["escopo"], self.scope)
        self.assertTrue(s["atores"])
        self.assertTrue(s["comunidades"])

    def test_snapshot_serializa_em_json(self):
        """É o insumo do relatório: se não serializa, não serve para nada."""
        import json
        s = dossie.snapshot(self.conn, self.window, self.PAUTA, core=False, mapa_top=10)
        texto = json.dumps(s, ensure_ascii=False)
        self.assertIn(self.PAUTA, texto)


class TestComandoDossie(DossieTestCase):
    """O caminho inteiro pela CLI. O risco que isto cobre: cada peça passar
    isolada e o comando ler um escopo que ninguém gravou."""

    def _caminho(self):
        return str(self.conn.execute("PRAGMA database_list").fetchone()["file"])

    def _rodar(self, *extra):
        import io
        import contextlib
        from nabote import cli
        argv = ["--db", self._caminho(), "dossie",
                "--window", self.window, "--topic", self.PAUTA,
                "--no-core", "--compact", *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            codigo = cli.main(argv)
        return codigo, out.getvalue(), err.getvalue()

    def test_roda_e_devolve_json(self):
        import json
        codigo, saida, _ = self._rodar()
        self.assertEqual(codigo, 0, saida)
        dados = json.loads(saida)
        self.assertEqual(dados["pauta"], self.PAUTA)
        self.assertTrue(dados["atores"])
        self.assertTrue(dados["comunidades"])

    def test_relata_o_eixo_em_stderr(self):
        """Sem esse relato, uma execução que posicionou 3 de 600 atores sai
        igual a uma que posicionou 600 — e o silêncio vira aprovação."""
        _, _, err = self._rodar()
        self.assertIn("eixo:", err)
        self.assertIn("inércia", err)

    def test_pauta_inexistente_falha_dizendo_o_que_rodar(self):
        codigo, _, err = self._rodar_pauta("NaoExiste")
        self.assertEqual(codigo, 1)
        self.assertIn("aggregate", err)

    def _rodar_pauta(self, pauta):
        import io
        import contextlib
        from nabote import cli
        argv = ["--db", self._caminho(), "dossie",
                "--window", self.window, "--topic", pauta, "--no-core", "--compact"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            codigo = cli.main(argv)
        return codigo, out.getvalue(), err.getvalue()


class TestMensagemDeErro(DossieTestCase):
    """A mensagem de erro do dossiê dita um comando para o usuário rodar. Se
    ela inventar uma flag, o usuário cola, o argparse recusa, e a culpa parece
    ser dele. Este teste passa a mensagem pelo parser de verdade."""

    def _erro_de(self, pauta):
        import io
        import contextlib
        from nabote import cli
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            cli.main(["--db", self._caminho(), "dossie", "--window", self.window,
                      "--topic", pauta, "--compact"])
        return err.getvalue()

    def _caminho(self):
        return str(self.conn.execute("PRAGMA database_list").fetchone()["file"])

    def test_o_comando_sugerido_e_aceito_pelo_parser(self):
        import re
        from nabote import cli
        texto = self._erro_de(self.PAUTA)
        sugestoes = re.findall(r"`([^`]+)`", texto)
        self.assertTrue(sugestoes, f"nenhum comando sugerido em: {texto!r}")
        for sug in sugestoes:
            argv = sug.split()
            with self.subTest(comando=sug):
                try:
                    cli.build_parser().parse_args(argv)
                except SystemExit:
                    self.fail(f"o dossiê sugere um comando que não existe: {sug}")

    def test_sugere_aggregate_quando_nao_ha_arestas(self):
        texto = self._erro_de("NaoExiste")
        self.assertIn("aggregate", texto)


class TestContaminacaoPorViral(DossieTestCase):
    """Os dois defeitos que o dado real expôs na primeira versão das sub-pautas.

    Na janela 2023-01-23 saíram, numa comunidade, três "enquadramentos" com
    posts=87, lift=89,4 e concentração idênticos — eram três fragmentos da mesma
    frase de UM post retuitado 87 vezes. E noutra, três trigramas deslizantes da
    mesma sentença ("estamos falando genocidio" / "falando genocidio marreco" /
    "genocidio marreco inferno"), que a regra de substring deixava passar porque
    nenhum é substring do outro.
    """

    def _variado(self, com, n, marca):
        """Posts de texto distinto na OUTRA comunidade, para baixar a taxa
        global do termo viral. Sem isso o termo é filtrado pelo lift e o caso
        de contaminação nem chega a se reproduzir."""
        inicio = datetime(2026, 9, 16, 8, 0, 0, tzinfo=timezone.utc)
        eventos = [_post(f"did:plc:sint{com:02d}{(i % 15) + 1:03d}",
                         f"did:plc:sint{com:02d}000", f"{marca}v{i}",
                         (inicio + timedelta(minutes=2 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         texto=f"{self.PAUTA} {_texto(com, i)} item {i}")
                   for i in range(n)]
        ingest.ingest(self.conn, Fonte(marca + "v", eventos), kind="campanha",
                      campaign_label=self.PAUTA, author_tier="C", resume=False)

    def _viral(self, com, texto, n, marca):
        inicio = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)
        eventos = [_post(f"did:plc:sint{com:02d}{(i % 15) + 1:03d}",
                         f"did:plc:sint{com:02d}000", f"{marca}{i}",
                         (inicio + timedelta(minutes=3 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         texto=texto)
                   for i in range(n)]
        ingest.ingest(self.conn, Fonte(marca, eventos), kind="campanha",
                      campaign_label=self.PAUTA, author_tier="C", resume=False)

    def test_um_post_viral_nao_vira_enquadramento(self):
        """87 cópias do mesmo texto são um post amplificado, não 87 evidências
        de que a comunidade usa aquele enquadramento."""
        frase = (f"{self.PAUTA} estamos falando de um escandalo absurdo "
                 f"inaceitavel escandaloso vergonhoso")
        self._variado(1, 200, "base1")
        self._viral(0, frase, 60, "viral")
        r = dossie.subtopics(self.conn, self.window, self.scope,
                             min_posts=5, min_textos=5)
        for linha in r["linhas"]:
            self.assertNotIn("escandalo", linha["termo"],
                             f"o texto viral virou enquadramento: {linha}")

    def test_com_o_limite_de_textos_desligado_a_contaminacao_reaparece(self):
        """Prova que é o limite agindo, e não o dado estar ausente: sem ele, o
        mesmo corpus devolve o termo do post viral."""
        frase = (f"{self.PAUTA} estamos falando de um escandalo absurdo "
                 f"inaceitavel escandaloso vergonhoso")
        self._variado(1, 200, "base2")
        self._viral(0, frase, 60, "viral2")
        solto = dossie.subtopics(self.conn, self.window, self.scope,
                                 min_posts=5, min_textos=1)
        self.assertTrue(any("escandalo" in l["termo"] for l in solto["linhas"]),
                        "o caso de contaminação não se reproduz; o teste não prova nada")

    def test_fragmentos_da_mesma_frase_nao_viram_tres_linhas(self):
        """Regra de rejeição por palavra em comum, não por substring."""
        for k in range(8):
            self._viral(1, f"{self.PAUTA} estamos falando genocidio marreco "
                           f"inferno relato numero {k}", 9, f"frag{k}")
        r = dossie.subtopics(self.conn, self.window, self.scope,
                             min_posts=5, min_textos=3)
        for cid in {l["comunidade"] for l in r["linhas"]}:
            termos = [l["termo"] for l in r["linhas"] if l["comunidade"] == cid]
            palavras = [set(t.split()) for t in termos]
            for i, a in enumerate(palavras):
                for b in palavras[i + 1:]:
                    self.assertFalse(a & b, f"termos partilham palavra: {termos}")

    def test_reporta_textos_distintos_ao_lado_dos_posts(self):
        """A diferença entre os dois números é o que diz se a pauta é muita
        gente dizendo coisas parecidas ou um post amplificado."""
        r = dossie.subtopics(self.conn, self.window, self.scope,
                             min_posts=5, min_textos=3)
        self.assertGreater(r["textos_distintos"], 0)
        self.assertLessEqual(r["textos_distintos"], r["posts_com_texto"])
        for linha in r["linhas"]:
            self.assertIn("textos", linha)
            self.assertLessEqual(linha["textos"], linha["posts"])


class TestTextosPorComunidade(DossieTestCase):
    """`textos` tem de ser da MESMA comunidade que `posts`.

    Na S04 saiu uma linha com posts=218 e textos=426 — impossível de ler, e o
    motivo era que `posts` contava a comunidade e `textos` contava o corpus
    inteiro. Dois recortes diferentes lado a lado na mesma linha.
    """

    def test_textos_nunca_passa_de_posts(self):
        for com in (0, 1):
            inicio = datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc)
            eventos = [_post(f"did:plc:sint{com:02d}{(i % 15) + 1:03d}",
                             f"did:plc:sint{com:02d}000", f"tc{com}-{i}",
                             (inicio + timedelta(minutes=4 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             texto=f"{self.PAUTA} crise {_texto(com, i)}")
                       for i in range(50)]
            ingest.ingest(self.conn, Fonte(f"tc{com}", eventos), kind="campanha",
                          campaign_label=self.PAUTA, author_tier="C", resume=False)
        r = dossie.subtopics(self.conn, self.window, self.scope,
                             min_posts=5, min_textos=3)
        self.assertTrue(r["linhas"])
        for linha in r["linhas"]:
            self.assertLessEqual(
                linha["textos"], linha["posts"],
                f"textos > posts na mesma linha: {linha}")

    def test_um_termo_compartilhado_conta_separado_em_cada_comunidade(self):
        """'crise' aparece nas duas: cada linha precisa do próprio número."""
        for com in (0, 1):
            inicio = datetime(2026, 9, 17, 11, 0, 0, tzinfo=timezone.utc)
            n = 60 if com == 0 else 12
            eventos = [_post(f"did:plc:sint{com:02d}{(i % 15) + 1:03d}",
                             f"did:plc:sint{com:02d}000", f"sh{com}-{i}",
                             (inicio + timedelta(minutes=4 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             texto=f"{self.PAUTA} crise {_texto(com, i)} n{i}")
                       for i in range(n)]
            ingest.ingest(self.conn, Fonte(f"sh{com}", eventos), kind="campanha",
                          campaign_label=self.PAUTA, author_tier="C", resume=False)
        r = dossie.subtopics(self.conn, self.window, self.scope,
                             min_posts=5, min_textos=3, por_comunidade=8)
        linhas = {(l["comunidade"], l["termo"]): l for l in r["linhas"]
                  if l["termo"] == "crise"}
        if len(linhas) >= 2:
            valores = {k[0]: v["textos"] for k, v in linhas.items()}
            self.assertNotEqual(len(set(valores.values())), 1,
                                f"as duas comunidades receberam o mesmo número "
                                f"global de textos: {valores}")
