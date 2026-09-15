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
from contextlib import redirect_stderr, redirect_stdout
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
        ingest.ingest(conn, ListSource(events, "nucleo"), author_tier="A")
        ingest.ingest(conn, ListSource(scattered_dyads(30), "diades"),
                      author_tier="C")
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
            view="amp", top=10, min_community=1, community=None, core=False,
            partition=None, min_degree=0.0)
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


class TestNucleoNoDump(DumpTestCase):
    """Sob `--core`, cabeçalho e lista têm de falar do MESMO grafo.

    A lista de arestas lia o `edge_window` direto e mostrava as mesmas arestas
    do grafo cheio, inclusive de atores que a poda removeu. O cabeçalho anunciava
    o núcleo e a lista exibia outra coisa.
    """

    def setUp(self):
        super().setUp()
        conn = db.connect(self.path)
        window = graph.windows_present(conn)[0]
        graph.analyze_window(conn, window, view="amp", core=True)
        self.nucleo = {r["platform_user_id"] for r in conn.execute(
            "SELECT a.platform_user_id FROM actor_community c "
            "JOIN actor a ON a.actor_id = c.actor_id WHERE c.scope='amp:core'")}
        conn.close()

    def _arestas(self, saida: str) -> list[str]:
        trecho = saida.split("arestas mais pesadas")[1].split("peso total")[0]
        return [l for l in trecho.splitlines() if "->" in l]

    def test_cabecalho_nomeia_o_escopo_do_nucleo(self):
        self.assertIn("da visão amp:core", self.dump(core=True))

    def test_so_lista_arestas_entre_atores_do_nucleo(self):
        self.assertTrue(self.nucleo, "o núcleo ficou vazio; teste não vale")
        for linha in self._arestas(self.dump(core=True, top=30)):
            origem, destino = linha.split("-")[0].strip(), linha.split("->")[1].strip()
            destino = destino.rsplit(" ", 1)[0].strip()
            self.assertIn(origem, self.nucleo, f"ator podado na lista: {origem}")
            self.assertIn(destino, self.nucleo, f"ator podado na lista: {destino}")

    def test_lista_para_no_teto(self):
        """O teto tem de ser respeitado EXATAMENTE, porque é ele que permite
        parar cedo. Filtrar o núcleo em SQL fazia o planejador escolher produto
        cartesiano sobre o índice único — 72 mil × 72 mil no dado real — e o
        comando nunca terminava. Lendo em ordem de peso e parando no teto, o
        custo é proporcional ao que se mostra, não ao tamanho da janela."""
        for teto in (1, 3, 7):
            with self.subTest(teto=teto):
                arestas = self._arestas(self.dump(core=True, top=teto))
                self.assertEqual(len(arestas), teto)

    def test_a_lista_do_nucleo_e_subconjunto_estrito(self):
        """Com o teto alto o bastante para caber tudo: a lista do núcleo tem de
        ser exatamente a do grafo cheio menos as arestas de atores podados."""
        cheio = set(self._arestas(self.dump(top=500)))
        nucleo = set(self._arestas(self.dump(core=True, top=500)))
        self.assertTrue(nucleo, "o núcleo não listou aresta nenhuma")
        self.assertTrue(nucleo < cheio, "deveria ser subconjunto ESTRITO")
        podadas = cheio - nucleo
        self.assertTrue(any("solto" in linha for linha in podadas),
                        "as díades soltas deveriam ter sumido da lista")


class TestMinDegree(DumpTestCase):
    def test_filtra_quem_tem_rank_herdado(self):
        """PageRank é herdado: quem é repostado por um hub recebe quase todo o
        rank dele. No grafo de respostas real isso pôs seis contas de in-degree
        1 acima de contas com dezenas de arestas."""
        sem_filtro = self._linhas(self.dump(top=50))
        com_filtro = self._linhas(self.dump(top=50, min_degree=3.0))
        self.assertTrue(com_filtro, "o filtro removeu todo mundo")
        self.assertLess(len(com_filtro), len(sem_filtro))
        for linha in com_filtro:
            self.assertGreaterEqual(float(linha.split()[4]), 3.0)

    def _linhas(self, saida: str) -> list[str]:
        trecho = saida.split("-" * 72)[1].split("\ncomunidades")[0]
        return [l for l in trecho.splitlines() if l.strip()]


class TestFiltroDeComunidade(DumpTestCase):
    def test_min_community_esconde_a_cauda_e_a_declara(self):
        saida = self.dump(min_community=5, top=50)
        self.assertNotIn("(E-I mecânico)  ", saida)
        self.assertIn("comunidades restantes", saida)
        self.assertIn("atores no total", saida)

    def test_cauda_resumida_bate_com_o_total(self):
        """O resumo tem de fechar com a contagem: mostradas + cauda = todas."""
        saida = self.dump(min_community=5, top=50)
        total = int(saida.split("comunidades  (")[1].split(" no total")[0])
        mostradas = saida.count(" atores   cru ")
        cauda = int(saida.split("… + ")[1].split(" comunidades")[0])
        self.assertEqual(mostradas + cauda, total)

    def test_min_community_nao_e_cortado_pelo_top(self):
        """Quem pede 'todas acima de 4' quer todas. `--top 1` limita atores e
        arestas, nunca o corte explícito de comunidade — filtro que engana
        silenciosamente é pior que filtro nenhum."""
        saida = self.dump(min_community=4, top=1)
        listadas = saida.count(" atores   cru ")
        self.assertEqual(listadas, 3, "as 3 comunidades plantadas têm 8 atores cada")

    def test_distribuicao_separa_rede_de_cacos(self):
        """Uma linha tem de responder 'isto é rede ou pilha de cacos?'."""
        saida = self.dump()
        linha = [l for l in saida.splitlines() if "distribuição" in l][0]
        self.assertIn("4-9: 3", linha)      # as comunidades plantadas
        self.assertIn("≤3: 30", linha)      # as díades soltas

    def test_community_recorta_a_lista_de_atores(self):
        saida = self.dump(community=0, top=50)
        self.assertIn("atores da comunidade #0", saida)
        linhas = [l for l in saida.splitlines() if l.startswith("did:plc:")]
        self.assertTrue(linhas, "nenhum ator listado")
        for linha in linhas:
            self.assertEqual(linha.split()[2], "0", f"ator de outra comunidade: {linha}")


class TestRuns(unittest.TestCase):
    """`runs` responde 'sobre o quê é este grafo?'.

    Um grafo montado com o termo "bbb23" e um montado com "impeachment" não são
    a mesma rede vista duas vezes. Sem a procedência, comunidade, centralidade e
    E-I são calculáveis e ininterpretáveis.
    """

    TERMOS = [("lula", "2022-12-28T10:00:00Z", 30),
              ("bolsonaro", "2022-12-29T10:00:00Z", 20),
              ("bbb23", "2023-01-04T10:00:00Z", 12)]

    def setUp(self):
        from nabote.events import NormalizedEvent, Target

        class Fonte:
            def __init__(self, nome, eventos):
                self.name, self._eventos, self.skipped = nome, eventos, 0

            def events(self, cursor=None):
                yield from self._eventos

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "r.db"
        conn = db.connect(self.path)
        db.migrate(conn, ROOT / "migrations")
        for termo, quando, n in self.TERMOS:
            eventos = [NormalizedEvent(
                platform="x", kind="post", actor_uid=f"u{termo}{i}",
                occurred_at=quando, post_uid=f"p{termo}{i}", post_type="repost",
                targets=[Target(kind="repost", uid=f"hub_{termo}")]) for i in range(n)]
            ingest.ingest(conn, Fonte(f"x_parquet:{termo}", eventos), kind="campanha",
                          campaign_label=termo, author_tier="C", resume=False)
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def runs(self) -> str:
        args = argparse.Namespace(db=str(self.path), top=10)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(cli.cmd_runs(args), 0)
        return buffer.getvalue()

    def test_lista_todo_termo_carregado(self):
        saida = self.runs()
        for termo, _, n in self.TERMOS:
            self.assertIn(termo, saida)
            self.assertIn(str(n), saida)

    def test_agrupa_termos_pela_janela_certa(self):
        """28 e 29 de dezembro caem na mesma semana; 4 de janeiro, na seguinte.
        Errar isto atribui a pauta à semana errada — e o relatório fica plausível
        e falso."""
        por_janela = self.runs().split("por janela")[1]
        dezembro = por_janela.split("2022-12-26")[1].split("2023-01-02")[0]
        janeiro = por_janela.split("2023-01-02")[1]
        self.assertIn("lula", dezembro)
        self.assertIn("bolsonaro", dezembro)
        self.assertNotIn("bbb23", dezembro)
        self.assertIn("bbb23", janeiro)
        self.assertNotIn("lula", janeiro)

    def test_soma_da_janela_bate_com_os_termos(self):
        janeiro = self.runs().split("2022-12-26")[1]
        self.assertIn("50 posts", janeiro)  # 30 do lula + 20 do bolsonaro


class TestThemes(unittest.TestCase):
    """Nível 1 do plano: a pauta EMERGE da estrutura.

    A comunidade é descoberta pelo grafo, sem olhar texto nenhum; só depois se
    pergunta sobre o que ela falava. Nesta base o rótulo sai de graça porque a
    coleta foi por Trending Topic, e serve de gabarito para o clustering de
    texto do passo 3.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "t.db"
        self.conn = db.connect(self.path)
        db.migrate(self.conn, ROOT / "migrations")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def carrega(self, termo, hub, prefixo, n, quando="2022-12-28T10:00:00Z"):
        from nabote.events import NormalizedEvent, Target

        class Fonte:
            def __init__(self, nome, eventos):
                self.name, self._eventos, self.skipped = nome, eventos, 0

            def events(self, cursor=None):
                yield from self._eventos

        eventos = [NormalizedEvent(
            platform="x", kind="post", actor_uid=f"{prefixo}{i}",
            occurred_at=quando, post_uid=f"p{prefixo}{quando[:10]}{i}",
            post_type="repost", targets=[Target(kind="repost", uid=hub)])
            for i in range(n)]
        ingest.ingest(self.conn, Fonte(f"x:{termo}:{quando[:10]}", eventos),
                      kind="campanha", campaign_label=termo, author_tier="C",
                      resume=False)

    def themes(self, window, **kwargs) -> str:
        for w in graph.windows_present(self.conn):
            graph.aggregate_window(self.conn, w)
            graph.analyze_window(self.conn, w, view="amp")
        args = argparse.Namespace(
            db=str(self.path), window=window, all=False, scope="all",
            view="amp", top=20, terms=5, min_community=5, core=False,
            partition=None)
        for key, value in kwargs.items():
            setattr(args, key, value)
        buffer = io.StringIO()
        self.conn.commit()
        with redirect_stdout(buffer):
            self.assertEqual(cli.cmd_themes(args), 0)
        return buffer.getvalue()

    def test_uma_comunidade_com_duas_pautas_mostra_a_proporcao(self):
        """Todos reposta o MESMO hub, então o grafo faz UMA comunidade. Os
        termos vêm de runs diferentes — é a mistura de pauta dentro de um só
        grupo, que é o caso interessante e o mais fácil de errar."""
        self.carrega("posse", "hub", "a", 30)
        self.carrega("bbb23", "hub", "b", 10)
        saida = self.themes("2022-12-26")
        self.assertIn("posse", saida)
        self.assertIn("75%", saida)
        self.assertIn("bbb23", saida)
        self.assertIn("25%", saida)

    def test_post_de_outra_janela_nao_vaza(self):
        """Sem o filtro de janela, a pauta de uma semana é atribuída à outra —
        e o relatório fica plausível e falso."""
        self.carrega("posse", "hub", "a", 30, quando="2022-12-28T10:00:00Z")
        self.carrega("carnaval", "hub", "a", 30, quando="2023-02-15T10:00:00Z")
        dezembro = self.themes("2022-12-26")
        self.assertIn("posse", dezembro)
        self.assertNotIn("carnaval", dezembro)
        fevereiro = self.themes("2023-02-13")
        self.assertIn("carnaval", fevereiro)
        self.assertNotIn("posse", fevereiro)

    def test_alvo_sem_post_nao_vota(self):
        """Tier C é alvo, não voz: ele está no grafo sem ter escrito nada. Se
        contasse como autor, a pauta de quem foi citado viraria pauta dele."""
        self.carrega("posse", "hub", "a", 12)
        saida = self.themes("2022-12-26")
        self.assertIn("12 com voz", saida)   # 13 atores no grafo, 12 autores
        self.assertIn("13 atores", saida)

    def test_voz_nunca_passa_do_tamanho_da_comunidade(self):
        """Contar autores distintos por TERMO e somar conta duas vezes quem
        falou de dois assuntos — e o total estoura o tamanho da comunidade. Foi
        assim que este bug apareceu no dado real: 5.014 atores, 6.760 "com voz"."""
        from nabote.events import NormalizedEvent, Target

        class Fonte:
            def __init__(self, nome, eventos):
                self.name, self._eventos, self.skipped = nome, eventos, 0

            def events(self, cursor=None):
                yield from self._eventos

        # os MESMOS 20 atores postam sob dois termos diferentes
        for termo in ("posse", "alckmin"):
            eventos = [NormalizedEvent(
                platform="x", kind="post", actor_uid=f"a{i}",
                occurred_at="2022-12-28T10:00:00Z", post_uid=f"p{termo}{i}",
                post_type="repost", targets=[Target(kind="repost", uid="hub")])
                for i in range(20)]
            ingest.ingest(self.conn, Fonte(f"x:{termo}", eventos), kind="campanha",
                          campaign_label=termo, author_tier="C", resume=False)

        saida = self.themes("2022-12-26")
        tamanho = int(saida.split(" atores")[0].split()[-1])
        voz = int(saida.split(" com voz")[0].split()[-1])
        self.assertEqual(tamanho, 21, "20 autores + o hub")
        self.assertEqual(voz, 20, "cada autor conta UMA vez, não uma por termo")
        self.assertLessEqual(voz, tamanho)

    def test_min_community_esconde_as_pequenas(self):
        self.carrega("posse", "hub", "a", 30)
        self.assertNotIn("posse", self.themes("2022-12-26", min_community=500))


class TestAnalyzeReporta(unittest.TestCase):
    """Computação de 17 segundos que não deixa rastro na tela é indistinguível
    de computação que não rodou. Aconteceu: o `analyze` saiu idêntico ao de
    antes do modelo nulo existir, e não havia como saber se tinha rodado."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "a.db"
        conn = db.connect(self.path)
        db.migrate(conn, ROOT / "migrations")
        events, _ = planted_communities(n_communities=4, per_community=12)
        ingest.ingest(conn, ListSource(events, "nucleo"), author_tier="A")
        self.window = graph.windows_present(conn)[0]
        graph.aggregate_window(conn, self.window)
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def analyze(self, null: int = 0) -> str:
        args = argparse.Namespace(db=str(self.path), window=None, all=True,
                                  scope="all", view="amp", core=False, partition=None)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(cli.cmd_analyze(args), 0)
        return buffer.getvalue()

    def test_diz_quantos_atores_sustentam_o_ei(self):
        """Comunidade onde quase ninguém teve escolha tem E-I frágil. Se o
        comando não disser quantos atores entraram na conta, ninguém sabe se o
        número vale."""
        saida = self.analyze()
        self.assertIn("E-I com escolha:", saida)
        self.assertIn("têm mais de uma aresta", saida)


class TestCaminhoDeEntrada(unittest.TestCase):
    """Erro de digitação merece uma frase, não um traceback do zipfile.

    Aconteceu de verdade: uma variável de shell vazia virou `.`, passou no
    `exists()` — diretório existe — e explodiu dentro da biblioteca.
    """

    def _tenta(self, caminho, sufixos=(".zip",)):
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            resultado = cli._arquivo_de_entrada(caminho, sufixos)
        return resultado, buffer.getvalue()

    def test_diretorio_e_recusado_com_mensagem(self):
        resultado, erro = self._tenta(".")
        self.assertIsNone(resultado)
        self.assertIn("diretório", erro)

    def test_variavel_vazia_sugere_a_causa(self):
        resultado, erro = self._tenta("")
        self.assertIsNone(resultado)
        self.assertIn("vazio", erro)
        self.assertIn("shell", erro)

    def test_extensao_errada_e_recusada(self):
        with tempfile.TemporaryDirectory() as tmp:
            arquivo = Path(tmp) / "base.txt"
            arquivo.write_text("x", encoding="utf-8")
            resultado, erro = self._tenta(str(arquivo))
        self.assertIsNone(resultado)
        self.assertIn(".zip", erro)

    def test_arquivo_certo_passa(self):
        with tempfile.TemporaryDirectory() as tmp:
            arquivo = Path(tmp) / "base.zip"
            arquivo.write_bytes(b"PK")
            resultado, _ = self._tenta(str(arquivo))
        self.assertEqual(resultado, arquivo)

    def test_expande_o_til(self):
        """`~/Downloads/...` entre aspas não é expandido pelo shell em toda
        situação, e Path não expande sozinho."""
        resultado, _ = self._tenta("~/nao-existe-12345.zip")
        self.assertIsNone(resultado)


class TestRecortePorData(unittest.TestCase):
    """Recorte por contagem corta semana no meio; por data, não.

    A análise agrega por janela semanal. Carregar quatro dos sete dias de uma
    semana produz uma janela cujo volume foi decidido pelo recorte, não pelo
    mundo — e a série temporal mente sem avisar. Foi o que `--files 5` fez na
    primeira carga: pegou um prefixo cronológico e partiu a segunda semana.
    """

    def test_intervalo_inclui_as_duas_pontas(self):
        self.assertTrue(cli._no_intervalo("2022-12-26", "2022-12-26", "2023-01-01"))
        self.assertTrue(cli._no_intervalo("2023-01-01", "2022-12-26", "2023-01-01"))
        self.assertFalse(cli._no_intervalo("2022-12-25", "2022-12-26", "2023-01-01"))
        self.assertFalse(cli._no_intervalo("2023-01-02", "2022-12-26", "2023-01-01"))

    def test_ponta_aberta(self):
        self.assertTrue(cli._no_intervalo("1999-01-01", None, "2023-01-01"))
        self.assertTrue(cli._no_intervalo("2099-01-01", "2022-12-26", None))

    def test_arquivo_sem_data_fica_de_fora(self):
        """Admitir no banco algo cuja posição no tempo ninguém sabe é pior que
        deixar de fora."""
        self.assertFalse(cli._no_intervalo(None, "2022-12-26", "2023-01-01"))
        self.assertFalse(cli._no_intervalo(None, None, None))


class TestCobertura(unittest.TestCase):
    ZIP = [f"{d}-{t}.parquet"
           for d in ("2022-12-26", "2022-12-27", "2022-12-29", "2022-12-31",
                     "2023-01-02", "2023-01-03")
           for t in ("Alckmin", "Posse")]

    def _parse(self, nome: str):
        base = nome.removesuffix(".parquet")
        return base[:10], base[11:]

    def _relatorio(self, selecionados, recortado=True) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._cobertura(selecionados, selecionados, set(), self.ZIP, self._parse,
                           recortado=recortado)
        return buffer.getvalue()

    def test_sem_recorte_nao_diz_completa(self):
        """Sem seleção, toda semana está "completa" por definição — dizer isso é
        tautologia disfarçada de aprovação, e quem lê entende que conferiu algo."""
        saida = self._relatorio(self.ZIP, recortado=False)
        self.assertNotIn("completa", saida)
        self.assertIn("sem recorte", saida)

    def test_com_recorte_volta_a_julgar(self):
        self.assertIn("completa",
                      self._relatorio([m for m in self.ZIP if m < "2022-12-32"]))

    def test_semana_inteira_sai_como_completa(self):
        semana = [m for m in self.ZIP if m < "2022-12-32"]
        saida = self._relatorio(semana)
        self.assertIn("2022-12-26", saida)
        self.assertIn("completa", saida)
        self.assertNotIn("PARCIAL", saida)

    def test_semana_cortada_e_denunciada_com_os_dias_que_faltam(self):
        cortada = [m for m in self.ZIP if m.startswith(("2022-12-26", "2022-12-27"))]
        saida = self._relatorio(cortada)
        self.assertIn("PARCIAL", saida)
        self.assertIn("2022-12-29", saida)
        self.assertIn("2022-12-31", saida)

    def test_so_lista_semanas_com_selecao(self):
        """Listar semanas vazias enterraria a informação útil em ruído."""
        saida = self._relatorio([m for m in self.ZIP if m.startswith("2023-01")])
        self.assertIn("2023-01-02", saida)
        self.assertNotIn("2022-12-26", saida)


class TestSemanasVazias(unittest.TestCase):
    """Buraco entre semanas é informação de primeira ordem.

    A base real tem três semanas sem dado nenhum entre 30/01 e 27/02 de 2023.
    Uma série temporal que atravessa esse vazio compara os dois lados dele como
    se fossem contíguos — e o gráfico não mostra o buraco.
    """

    def test_encontra_o_vazio_no_meio(self):
        self.assertEqual(
            cli._semanas_vazias(["2023-01-30", "2023-02-27"]),
            ["2023-02-06", "2023-02-13", "2023-02-20"])

    def test_semanas_seguidas_nao_tem_vazio(self):
        self.assertEqual(
            cli._semanas_vazias(["2023-01-02", "2023-01-09", "2023-01-16"]), [])

    def test_nao_inventa_vazio_nas_bordas(self):
        """Antes da primeira e depois da última não é buraco: é fora do período
        coletado, e chamar de ausência sugeriria um problema que não existe."""
        self.assertEqual(cli._semanas_vazias(["2023-01-02"]), [])
        self.assertEqual(cli._semanas_vazias([]), [])


class TestArquivoCru(unittest.TestCase):
    """O payload cru é seguro contra bug de parser: reprocessar é grátis,
    recoletar de uma API não é. Carregando de um zip local essa razão some — o
    zip é o arquivo — e guardar de novo duplicaria gigabytes."""

    def _carrega(self, store_raw: bool) -> tuple[int, int]:
        from nabote.events import NormalizedEvent, Target

        class Fonte:
            def __init__(self, eventos):
                self.name, self._eventos, self.skipped = "zip", eventos, 0

            def events(self, cursor=None):
                yield from self._eventos

        eventos = [NormalizedEvent(
            platform="x", kind="post", actor_uid=f"a{i}",
            occurred_at="2022-12-28T10:00:00Z", post_uid=f"p{i}",
            post_type="repost", text="texto qualquer",
            targets=[Target(kind="repost", uid="hub")],
            raw={"campo": "x" * 500}) for i in range(50)]

        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "r.db")
            db.migrate(conn, ROOT / "migrations")
            ingest.ingest(conn, Fonte(eventos), kind="campanha",
                          campaign_label="t", resume=False, store_raw=store_raw)
            crus = conn.execute("SELECT COUNT(*) n FROM raw_payload").fetchone()["n"]
            posts = conn.execute("SELECT COUNT(*) n FROM post").fetchone()["n"]
            conn.close()
        return crus, posts

    def test_por_padrao_arquiva(self):
        crus, posts = self._carrega(store_raw=True)
        self.assertEqual(crus, posts)
        self.assertEqual(posts, 50)

    def test_sem_raw_o_grafo_continua_completo(self):
        """O que não pode acontecer é o atalho custar aresta: post e interaction
        têm de sair idênticos, só sem a cópia do payload."""
        crus, posts = self._carrega(store_raw=False)
        self.assertEqual(crus, 0)
        self.assertEqual(posts, 50)


class TestMigracaoPendente(unittest.TestCase):
    def test_detecta_schema_atrasado(self):
        """Coluna nova que só é LIDA some em silêncio num schema velho, e o
        comando parece ter funcionado."""
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            parcial = Path(tmp) / "migracoes"
            parcial.mkdir()
            todas = sorted((ROOT / "migrations").glob("*.sql"))
            for arquivo in todas[:-1]:
                shutil.copy(arquivo, parcial / arquivo.name)

            conn = db.connect(Path(tmp) / "velho.db")
            db.migrate(conn, parcial)
            pendentes = db.pending_migrations(conn, ROOT / "migrations")
            self.assertEqual(len(pendentes), 1)
            self.assertEqual(db.pending_migrations(conn, parcial), [])

            db.migrate(conn, ROOT / "migrations")
            self.assertEqual(db.pending_migrations(conn, ROOT / "migrations"), [])
            conn.close()


if __name__ == "__main__":
    unittest.main()
