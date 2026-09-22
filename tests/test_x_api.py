"""Testes da fonte twitterapi.io, contra a captura real de 20/09/2026.

O foco é o que o tradutor DEDUZ, não o que ele copia — e principalmente as três
armadilhas de formato documentadas no topo de `sources/x_api.py`, que são o tipo
de coisa que passa em silêncio e só aparece num relatório errado seis meses
depois.

Rodar:  python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nabote.sources import x_api  # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "twitterapi_io.json").read_text("utf-8"))


class TestDuasFormasDeData(unittest.TestCase):
    """Armadilha 1: `createdAt` tem dois formatos no mesmo nome de campo."""

    def test_formato_legado_do_autor_embutido(self):
        autor = FIXTURE["last_tweets"]["data"]["tweets"][0]["author"]
        self.assertEqual(autor["createdAt"], "Wed Aug 15 01:22:19 +0000 2012")
        self.assertEqual(x_api.parse_data(autor["createdAt"]),
                         "2012-08-15T01:22:19Z")

    def test_formato_iso_do_endpoint_de_perfil(self):
        perfil = FIXTURE["user_info"]["data"]
        self.assertEqual(perfil["createdAt"], "2012-08-15T01:22:19.000000Z")
        self.assertEqual(x_api.parse_data(perfil["createdAt"]),
                         "2012-08-15T01:22:19Z")

    def test_os_dois_formatos_dao_o_mesmo_instante(self):
        """Mesma conta, mesma criação, dois endpoints. Se divergirem, o mesmo
        ator entra no banco com duas datas de nascimento."""
        self.assertEqual(
            x_api.parse_data(FIXTURE["user_info"]["data"]["createdAt"]),
            x_api.parse_data(
                FIXTURE["last_tweets"]["data"]["tweets"][0]["author"]["createdAt"]))

    def test_data_torta_nao_derruba_a_coleta(self):
        for ruim in ("", None, "ontem", "2026-13-45", 42):
            with self.subTest(repr(ruim)):
                self.assertIsNone(x_api.parse_data(ruim))

    def test_fuso_nao_utc_e_convertido(self):
        self.assertEqual(x_api.parse_data("Sun Sep 20 16:41:37 -0300 2026"),
                         "2026-09-20T19:41:37Z")


class TestBioEmDoisLugares(unittest.TestCase):
    """Armadilha 2: no autor embutido, `description` vem vazio."""

    def test_autor_embutido_tem_a_bio_em_profile_bio(self):
        autor = FIXTURE["last_tweets"]["data"]["tweets"][0]["author"]
        self.assertEqual(autor["description"], "")
        self.assertTrue(x_api.bio_do_autor(autor).startswith("- Dep. Federal"))

    def test_perfil_direto_tem_a_bio_em_description(self):
        perfil = FIXTURE["user_info"]["data"]
        self.assertTrue(x_api.bio_do_autor(perfil).startswith("- Dep. Federal"))

    def test_bio_ausente_nos_dois_lugares_e_none(self):
        self.assertIsNone(x_api.bio_do_autor(
            {"description": "", "profile_bio": {"description": ""}}))
        self.assertIsNone(x_api.bio_do_autor({}))


class TestArestas(unittest.TestCase):
    def test_post_original_nao_gera_aresta(self):
        ev = x_api.normalize_tweet(FIXTURE["last_tweets"]["data"]["tweets"][0])
        self.assertEqual(ev.post_type, "original")
        self.assertEqual(ev.targets, [])
        self.assertEqual(ev.actor_uid, "758264276")
        self.assertEqual(ev.actor_handle, "nikolas_dm")
        self.assertEqual(ev.occurred_at, "2026-09-20T16:41:37Z")

    def test_citacao_vira_aresta_com_id_e_handle(self):
        """O tweet citado traz o autor inteiro embutido: o alvo nasce com id
        estável E nome legível, sem uma chamada a mais e sem custo a mais."""
        ev = x_api.normalize_tweet(FIXTURE["advanced_search"]["tweets"][0])
        self.assertEqual(ev.post_type, "quote")
        (alvo,) = ev.targets
        self.assertEqual(alvo.kind, "quote")
        self.assertEqual(alvo.uid, "14594813")
        self.assertEqual(alvo.handle, "ptbrasil")
        self.assertEqual(alvo.post_uid, "2101678966746534151")

    def test_resposta_vira_aresta_pelo_campo_de_topo(self):
        ev = x_api.normalize_tweet(FIXTURE["resposta_com_alvo"])
        self.assertEqual(ev.post_type, "reply")
        (alvo,) = ev.targets
        self.assertEqual((alvo.kind, alvo.uid, alvo.handle),
                         ("reply", "758264276", "nikolas_dm"))

    def test_retuite_vira_aresta(self):
        """Confirmado contra a API: `retweeted_tweet` é um tweet aninhado
        completo, com o autor dentro — a mesma forma de `quoted_tweet`."""
        ev = x_api.normalize_tweet(FIXTURE["retuite_real"])
        self.assertEqual(ev.post_type, "repost")
        self.assertEqual(ev.actor_uid, "39859804")
        repost = [a for a in ev.targets if a.kind == "repost"]
        self.assertEqual([(a.uid, a.handle) for a in repost],
                         [("1494658207", "KimKataguiri")])


class TestMencao(unittest.TestCase):
    """`entities.user_mentions` traz id estável — mas num retuíte ela contém o
    autor retuitado, e numa resposta contém quem foi respondido."""

    def test_o_retuitado_nao_vira_aresta_duas_vezes(self):
        """O prefixo "RT @fulano:" põe o autor retuitado em user_mentions. Sem
        descontar, a mesma relação entra como repost E como menção, e o peso de
        amplificação sai inflado."""
        rt = FIXTURE["retuite_real"]
        mencionados = {m["id_str"] for m in rt["entities"]["user_mentions"]}
        self.assertIn("1494658207", mencionados)   # o retuitado está lá

        ev = x_api.normalize_tweet(rt)
        self.assertEqual([a.kind for a in ev.targets], ["repost"])
        self.assertEqual(len({a.uid for a in ev.targets}), len(ev.targets))

    def test_o_respondido_nao_vira_aresta_duas_vezes(self):
        ev = x_api.normalize_tweet(FIXTURE["resposta_com_mencao_extra"])
        por_uid = {a.uid: a.kind for a in ev.targets}
        self.assertEqual(por_uid["758264276"], "reply")

    def test_terceiro_mencionado_vira_aresta_de_mencao(self):
        """Quem é mencionado e não é alvo por outra via é aresta de menção —
        com id, que é o que permite seguir a conta depois de trocar de nome."""
        ev = x_api.normalize_tweet(FIXTURE["resposta_com_mencao_extra"])
        mencao = [a for a in ev.targets if a.kind == "mention"]
        self.assertEqual([(a.uid, a.handle) for a in mencao],
                         [("39522911", "ptbrasil")])

    def test_mencionar_nao_muda_o_tipo_do_post(self):
        """Quem só menciona escreveu um original. `post_type` é uma coluna só,
        e 'mention' não é um dos valores que ela aceita."""
        so_mencao = dict(FIXTURE["last_tweets"]["data"]["tweets"][0])
        so_mencao["entities"] = {"user_mentions": [
            {"id_str": "39522911", "screen_name": "ptbrasil"}]}
        ev = x_api.normalize_tweet(so_mencao)
        self.assertEqual(ev.post_type, "original")
        self.assertEqual([a.kind for a in ev.targets], ["mention"])

    def test_entities_vazio_nao_quebra(self):
        """Foi este caso que me fez concluir errado que o campo não existia:
        tweet sem menção traz `entities` como `{}`."""
        ev = x_api.normalize_tweet(FIXTURE["last_tweets"]["data"]["tweets"][0])
        self.assertEqual(ev.targets, [])

    def test_mencao_sem_id_e_ignorada(self):
        torto = dict(FIXTURE["last_tweets"]["data"]["tweets"][0])
        torto["entities"] = {"user_mentions": [{"screen_name": "sem_id"}]}
        self.assertEqual(x_api.normalize_tweet(torto).targets, [])


class TestDescarte(unittest.TestCase):
    def test_tweet_sem_o_minimo_e_descartado(self):
        base = FIXTURE["last_tweets"]["data"]["tweets"][0]
        for falta in ("id", "author", "createdAt"):
            with self.subTest(falta):
                torto = dict(base)
                torto.pop(falta)
                self.assertIsNone(x_api.normalize_tweet(torto))

    def test_autor_sem_id_e_descartado(self):
        torto = dict(FIXTURE["last_tweets"]["data"]["tweets"][0])
        torto["author"] = {"userName": "alguem"}
        self.assertIsNone(x_api.normalize_tweet(torto))


class TestEnvelopes(unittest.TestCase):
    """O provedor tem quatro envelopes e nenhuma regra."""

    def test_envelope_com_data(self):
        self.assertEqual(len(x_api.tweets_da_resposta(FIXTURE["last_tweets"])), 1)

    def test_envelope_sem_nada(self):
        self.assertEqual(len(x_api.tweets_da_resposta(FIXTURE["advanced_search"])), 1)

    def test_corpo_inesperado_devolve_lista_vazia(self):
        for ruim in ({}, {"data": {}}, {"tweets": None}, [], None):
            with self.subTest(repr(ruim)):
                self.assertEqual(x_api.tweets_da_resposta(ruim), [])


class TestPaginacao(unittest.TestCase):
    def test_cursor_da_proxima_pagina_nos_dois_envelopes(self):
        self.assertEqual(x_api.proxima_pagina(FIXTURE["last_tweets"]), "DAABCgABGxxxxx")
        self.assertEqual(x_api.proxima_pagina(FIXTURE["advanced_search"]), "DAABCgABGyyyyy")

    def test_termina_por_has_next_page_e_nao_por_cursor_vazio(self):
        """O catálogo do provedor avisa: o cursor pode vir preenchido na última
        página. Um laço que confia nele não termina."""
        fim = {"tweets": [], "has_next_page": False, "next_cursor": "aindaTemTexto"}
        self.assertIsNone(x_api.proxima_pagina(fim))


class TestErro(unittest.TestCase):
    def test_403_de_chave_ausente(self):
        self.assertIn("API key required", x_api.erro_do_corpo(FIXTURE["erro_403"]))

    def test_429_de_limite_de_taxa(self):
        self.assertIn("QPS", x_api.erro_do_corpo(FIXTURE["erro_429"]))

    def test_erro_semantico_com_http_200(self):
        """O caso que engole em silêncio: 200 no transporte, erro no corpo.
        Uma coleta vazia pareceria uma semana sem assunto."""
        self.assertEqual(
            x_api.erro_do_corpo({"status": "error", "msg": "sem assinatura ativa"}),
            "sem assinatura ativa")

    def test_resposta_boa_nao_e_erro(self):
        self.assertIsNone(x_api.erro_do_corpo(FIXTURE["last_tweets"]))
        self.assertIsNone(x_api.erro_do_corpo(FIXTURE["advanced_search"]))


# --------------------------------------------------------------------------
# o laço da fonte, com a rede injetada
# --------------------------------------------------------------------------

class _Resposta:
    def __init__(self, corpo): self._b = json.dumps(corpo).encode("utf-8")
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Rede:
    """urlopen falso. Guarda as URLs pedidas — a ordem delas É o teste."""

    def __init__(self, paginas=None, saldos=None):
        self.paginas = paginas or {}
        self.saldos = list(saldos or [10000])
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if "/oapi/my/info" in url:
            saldo = self.saldos[0] if len(self.saldos) == 1 else self.saldos.pop(0)
            return _Resposta({"recharge_credits": 0, "total_bonus_credits": saldo})
        for marca, corpo in self.paginas.items():
            if marca in url:
                return _Resposta(corpo)
        return _Resposta({"tweets": [], "has_next_page": False})


def _tweet(tid, autor_id, quando="Sun Sep 20 16:41:37 +0000 2026"):
    return {"id": tid, "text": "oi", "createdAt": quando, "lang": "pt",
            "isReply": False, "inReplyToUserId": None, "entities": {},
            "quoted_tweet": None, "retweeted_tweet": None,
            "author": {"id": autor_id, "userName": f"u{autor_id}",
                       "description": "", "profile_bio": {"description": ""}}}


def _pagina(tweets, cursor=None):
    return {"tweets": tweets, "has_next_page": bool(cursor),
            "next_cursor": cursor or ""}


class TestLacoDaFonte(unittest.TestCase):
    def test_contas_e_query_sao_exclusivos(self):
        for kw in ({}, {"contas": ["a"], "query": "x"}):
            with self.subTest(kw):
                with self.assertRaises(ValueError):
                    x_api.XApiSource("k", **kw)

    def test_cada_conta_marca_os_eventos_dela(self):
        """Sem isto, a última conta paginada sobrescreve o cursor das outras —
        que é exatamente o que a migração 006 existe para impedir."""
        rede = _Rede({"userName=ua": _pagina([_tweet("1", "100")], None),
                      "userName=ub": _pagina([_tweet("2", "200")], None)})
        fonte = x_api.XApiSource("k", contas=["ua", "ub"], intervalo=0, abrir=rede)
        evs = list(fonte.events())
        self.assertEqual([(e.post_uid, e.cursor_account) for e in evs],
                         [("1", "ua"), ("2", "ub")])

    def test_pagina_ate_has_next_page_virar_falso(self):
        rede = _Rede({"cursor=p2": _pagina([_tweet("2", "100")], None),
                      "userName=ua": _pagina([_tweet("1", "100")], "p2")})
        fonte = x_api.XApiSource("k", contas=["ua"], paginas_por_conta=5,
                                 intervalo=0, abrir=rede)
        self.assertEqual([e.post_uid for e in fonte.events()], ["1", "2"])
        self.assertEqual(fonte.frame["pages"], 2)

    def test_retoma_do_cursor_guardado_daquela_conta(self):
        rede = _Rede({"userName=ua": _pagina([_tweet("9", "100")], None)})
        fonte = x_api.XApiSource("k", contas=["ua"], cursores={"ua": "onde_parei"},
                                 intervalo=0, abrir=rede)
        list(fonte.events())
        pedido = [u for u in rede.urls if "last_tweets" in u][0]
        self.assertIn("cursor=onde_parei", pedido)

    def test_teto_de_gasto_para_e_registra_quem_ficou_de_fora(self):
        """Parar sem registrar o que faltou faria a janela seguinte mentir:
        número de cobertura desconhecida não é número medido."""
        rede = _Rede({"userName=": _pagina([_tweet("1", "100")], None)},
                     saldos=[10000, 1000, 1000, 1000])
        fonte = x_api.XApiSource("k", contas=["ua", "ub", "uc"], teto_usd=0.05,
                                 conferir_saldo_a_cada=1, intervalo=0, abrir=rede)
        list(fonte.events())
        self.assertEqual(fonte.frame["stopped_by"], "budget")
        self.assertEqual(fonte.frame["left_out"], ["ub", "uc"])
        self.assertEqual(fonte.frame["reached"], 1)

    def test_gasto_sai_do_saldo_do_provedor_e_nao_de_tabela(self):
        rede = _Rede({"userName=ua": _pagina([_tweet("1", "100")], None)},
                     saldos=[10000, 9982])
        fonte = x_api.XApiSource("k", contas=["ua"], intervalo=0, abrir=rede)
        list(fonte.events())
        self.assertAlmostEqual(fonte.gasto_usd, 18 / 100_000, places=8)

    def test_busca_usa_cursor_unico(self):
        rede = _Rede({"advanced_search": _pagina([_tweet("1", "100")], None)})
        fonte = x_api.XApiSource("k", query="CPMI", intervalo=0, abrir=rede)
        evs = list(fonte.events())
        self.assertEqual([e.cursor_account for e in evs], [""])
        self.assertEqual(fonte.frame["kind"], "search")

    def test_tweet_impossivel_e_contado_e_nao_derruba(self):
        rede = _Rede({"userName=ua": _pagina(
            [_tweet("1", "100"), _tweet("2", "200", quando="ontem")], None)})
        fonte = x_api.XApiSource("k", contas=["ua"], intervalo=0, abrir=rede)
        self.assertEqual([e.post_uid for e in fonte.events()], ["1"])
        self.assertEqual(fonte.skipped, 1)

    def test_erro_semantico_com_200_vira_excecao(self):
        rede = _Rede({"userName=ua": {"status": "error", "msg": "sem assinatura"}})
        fonte = x_api.XApiSource("k", contas=["ua"], intervalo=0, abrir=rede)
        with self.assertRaises(x_api.ErroDoProvedor) as ctx:
            list(fonte.events())
        self.assertIn("sem assinatura", str(ctx.exception))

    def test_a_chave_vai_no_cabecalho_e_nao_na_url(self):
        """Chave em query string entra em log de servidor e em histórico de
        proxy. No cabeçalho, não."""
        rede = _Rede({"userName=ua": _pagina([], None)})
        fonte = x_api.XApiSource("SEGREDO", contas=["ua"], intervalo=0, abrir=rede)
        list(fonte.events())
        for url in rede.urls:
            self.assertNotIn("SEGREDO", url)


class TestQuantoCustaSaberOCusto(unittest.TestCase):
    """Ler o saldo é uma requisição — então conferi-lo a cada página faria
    metade do que se paga ser para saber quanto se está pagando, e dobraria o
    tempo de parede a 1 req/5 s."""

    def _saldos_pedidos(self, rede):
        return sum(1 for u in rede.urls if "/oapi/my/info" in u)

    def test_sem_teto_o_saldo_e_lido_so_no_inicio_e_no_fim(self):
        rede = _Rede({"userName=": _pagina([_tweet("1", "100")], None)})
        # `conferir_saldo_a_cada=1` de propósito: sem teto, nem assim o saldo
        # pode ser lido no meio. Com N alto o teste passaria pelo motivo errado.
        fonte = x_api.XApiSource("k", contas=["ua", "ub", "uc"],
                                 conferir_saldo_a_cada=1, intervalo=0, abrir=rede)
        list(fonte.events())
        self.assertEqual(self._saldos_pedidos(rede), 2)

    def test_com_teto_o_saldo_e_lido_a_cada_N_e_nao_a_cada_pagina(self):
        rede = _Rede({"userName=": _pagina([_tweet("1", "100")], None)})
        fonte = x_api.XApiSource("k", contas=[f"u{i}" for i in range(6)],
                                 teto_usd=99.0, conferir_saldo_a_cada=3,
                                 intervalo=0, abrir=rede)
        list(fonte.events())
        # 6 páginas -> 2 conferências periódicas, + inicial + final = 4
        self.assertEqual(self._saldos_pedidos(rede), 4)

    def test_o_saldo_final_e_lido_sempre_para_o_custo_ficar_medido(self):
        """`cost_usd` no `collection_run` é a diferença de dois saldos reais.
        Sem a leitura final ele sairia zerado, e um número de custo estimado
        não cumpre a regra do projeto."""
        rede = _Rede({"userName=": _pagina([_tweet("1", "100")], None)},
                     saldos=[10000, 9900])
        fonte = x_api.XApiSource("k", contas=["ua"], intervalo=0, abrir=rede)
        list(fonte.events())
        self.assertAlmostEqual(fonte.gasto_usd, 100 / 100_000, places=8)

    def test_run_que_morre_no_meio_tem_o_custo_gravado_igual(self):
        """Falhar não devolve o dinheiro: o saldo final vai num `finally`."""
        class Explode(_Rede):
            def __call__(self, req, timeout=None):
                if "last_tweets" in req.full_url:
                    self.urls.append(req.full_url)
                    raise RuntimeError("a rede caiu")
                return super().__call__(req, timeout)

        rede = Explode({}, saldos=[10000, 9950])
        fonte = x_api.XApiSource("k", contas=["ua"], intervalo=0, abrir=rede)
        with self.assertRaises(RuntimeError):
            list(fonte.events())
        self.assertAlmostEqual(fonte.gasto_usd, 50 / 100_000, places=8)


class TestLimiteDeTaxa(unittest.TestCase):
    def test_espera_entre_requisicoes(self):
        """1 req/5 s no tier gratuito. Sem a espera, a segunda chamada volta
        429 — e 429 gasta a requisição sem trazer dado."""
        rede = _Rede({"userName=": _pagina([], None)})
        fonte = x_api.XApiSource("k", contas=["ua", "ub"], intervalo=0.05, abrir=rede)
        import time as _t
        inicio = _t.monotonic()
        list(fonte.events())
        # saldo inicial + 2 timelines + saldo final = 4 requisições, 3 esperas
        self.assertGreaterEqual(_t.monotonic() - inicio, 0.05 * 3)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# a ponta do CLI
# --------------------------------------------------------------------------

class TestCarregarEnv(unittest.TestCase):
    """O runbook manda a chave para o .env; sem isto o `fetch --source x` só
    funcionaria depois de um `source .env` que ninguém lembra de dar."""

    def setUp(self):
        import tempfile
        from nabote import cli
        self.cli = cli
        self._tmp = tempfile.TemporaryDirectory()
        self.env = Path(self._tmp.name) / ".env"

    def tearDown(self):
        import os
        for k in ("NABOTE_X_API_KEY", "NABOTE_X_PROVIDER", "VAZIA"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_le_chave_e_ignora_comentario_e_linha_vazia(self):
        self.env.write_text(
            "# comentário\n\nNABOTE_X_PROVIDER=twitterapi_io\n"
            "NABOTE_X_API_KEY=abc123\n", encoding="utf-8")   # guarda:permitido
        lidas = self.cli.carregar_env(self.env)
        self.assertEqual(lidas["NABOTE_X_PROVIDER"], "twitterapi_io")
        self.assertEqual(lidas["NABOTE_X_API_KEY"], "abc123")

    def test_variavel_ja_exportada_vence(self):
        """Quem exportou foi explícito; o arquivo é o padrão, não a ordem."""
        import os
        os.environ["NABOTE_X_API_KEY"] = "da_sessao"
        self.env.write_text("NABOTE_X_API_KEY=do_arquivo\n", encoding="utf-8")
        self.cli.carregar_env(self.env)
        self.assertEqual(os.environ["NABOTE_X_API_KEY"], "da_sessao")

    def test_tira_aspas_do_valor(self):
        self.env.write_text('NABOTE_X_API_KEY="entre_aspas"\n', encoding="utf-8")
        self.assertEqual(self.cli.carregar_env(self.env)["NABOTE_X_API_KEY"],
                         "entre_aspas")

    def test_arquivo_ausente_nao_quebra(self):
        self.assertEqual(self.cli.carregar_env(Path("/nao/existe/.env")), {})


class TestSementesDoX(unittest.TestCase):
    """A API do X pede `userName`, não id — então a semente é o handle."""

    def setUp(self):
        import tempfile
        from nabote import db, identity
        self.db, self.identity = db, identity
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "t.db")
        db.migrate(self.conn, ROOT / "migrations")
        agora = db.utcnow()
        for plat, uid, handle, tier in [
                ("x", "100", "semente_a", "A"), ("x", "200", "semente_b", "A"),
                ("x", "300", "mensal", "B"), ("x", "400", "nunca_coletado", "C"),
                ("x", "500", None, "A"), ("bluesky", "did:plc:x", "outra_rede", "A")]:
            self.conn.execute(
                "INSERT INTO actor (platform, platform_user_id, handle, tier, "
                "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
                (plat, uid, handle, tier, agora, agora))

    def tearDown(self):
        self.conn.close(); self._tmp.cleanup()

    def test_so_tier_a_do_x(self):
        """Tier B é coleta mensal e tier C nunca é coletado — incluí-los aqui
        faria o ciclo semanal pagar por quem não devia entrar nele."""
        self.assertEqual(self.identity.seed_handles(self.conn, "x"),
                         ["semente_a", "semente_b"])

    def test_nao_vaza_semente_de_outra_plataforma(self):
        self.assertNotIn("outra_rede", self.identity.seed_handles(self.conn, "x"))

    def test_ator_sem_handle_fica_de_fora(self):
        """Ator conhecido só pelo id não dá para pedir por `userName`. Entrar
        na lista viraria uma requisição paga com resposta vazia."""
        self.assertNotIn(None, self.identity.seed_handles(self.conn, "x"))
        self.assertEqual(len(self.identity.seed_handles(self.conn, "x")), 2)

    def test_tier_b_entra_quando_pedido(self):
        self.assertEqual(self.identity.seed_handles(self.conn, "x", ("A", "B")),
                         ["mensal", "semente_a", "semente_b"])


class TestRegistroDeSementes(unittest.TestCase):
    """Cada entrada custa uma requisição — então o lote não pode ser tudo ou
    nada, e o que já foi resolvido tem de ficar gravado."""

    def setUp(self):
        import tempfile
        from nabote import db, identity
        self.db, self.identity = db, identity
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "t.db")
        db.migrate(self.conn, ROOT / "migrations")

    def tearDown(self):
        self.conn.close(); self._tmp.cleanup()

    def _transporte(self, perfis, saldos=None):
        rede = _Rede(saldos=saldos)
        rede.perfis = perfis

        def chamar(req, timeout=None):
            url = req.full_url
            rede.urls.append(url)
            if "/oapi/my/info" in url:
                return _Resposta({"recharge_credits": 0,
                                  "total_bonus_credits": rede.saldos.pop(0)
                                  if len(rede.saldos) > 1 else rede.saldos[0]})
            nome = url.split("userName=")[1].split("&")[0]
            corpo = perfis.get(nome)
            if corpo is None:
                return _Resposta({"status": "error", "msg": f"user not found: {nome}"})
            return _Resposta({"status": "success", "data": corpo})

        t = x_api.Transporte("k", intervalo=0, abrir=chamar)
        t.rede = rede
        return t

    def _perfil(self, uid, nome, seguidores=100):
        return {"id": uid, "userName": nome, "name": nome.upper(),
                "description": f"bio de {nome}", "followers": seguidores,
                "following": 10, "statusesCount": 900,
                "createdAt": "2012-08-15T01:22:19.000000Z"}

    def test_grava_o_id_e_nao_so_o_handle(self):
        """Handle muda, id não. Resolver no registro é o que impede a mesma
        conta virar dois atores depois de uma troca de nome."""
        tr = self._transporte({"nikolas_dm": self._perfil("758264276", "nikolas_dm")})
        ok, falhas = self.identity.register_seeds_x(self.conn, ["nikolas_dm"], tr)
        self.assertEqual(ok, [("nikolas_dm", "758264276")])
        self.assertEqual(falhas, [])

        row = self.conn.execute(
            "SELECT platform, platform_user_id, handle, tier, display_name, bio,"
            " account_created_at FROM actor").fetchone()
        self.assertEqual(row["platform"], "x")
        self.assertEqual(row["platform_user_id"], "758264276")
        self.assertEqual(row["tier"], "A")
        self.assertEqual(row["bio"], "bio de nikolas_dm")
        self.assertEqual(row["account_created_at"], "2012-08-15T01:22:19Z")

    def test_uma_falha_nao_derruba_o_lote(self):
        """Falhar no meio e perder o que já foi pago seria cobrar duas vezes
        pela mesma resolução."""
        tr = self._transporte({"boa": self._perfil("1", "boa"),
                               "outra": self._perfil("2", "outra")})
        ok, falhas = self.identity.register_seeds_x(
            self.conn, ["boa", "nao_existe", "outra"], tr)
        self.assertEqual([h for h, _ in ok], ["boa", "outra"])
        self.assertEqual([h for h, _ in falhas], ["nao_existe"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM actor").fetchone()["n"], 2)

    def test_arroba_no_comeco_e_tolerado(self):
        tr = self._transporte({"fulano": self._perfil("9", "fulano")})
        ok, _ = self.identity.register_seeds_x(self.conn, ["@fulano"], tr)
        self.assertEqual(ok, [("fulano", "9")])

    def test_rodar_de_novo_nao_duplica_nem_rebaixa(self):
        """`upsert_actor` é idempotente, e é o que faz uma segunda rodada
        pagar só pelo que faltou."""
        perfis = {"a": self._perfil("1", "a")}
        self.identity.register_seeds_x(self.conn, ["a"], self._transporte(perfis))
        self.identity.register_seeds_x(self.conn, ["a"], self._transporte(perfis),
                                       tier="B")
        linhas = self.conn.execute("SELECT tier FROM actor").fetchall()
        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0]["tier"], "A")   # tier sobe, nunca desce

    def test_snapshot_do_dia_atualiza_em_vez_de_duplicar(self):
        self.identity.register_seeds_x(
            self.conn, ["a"], self._transporte({"a": self._perfil("1", "a", 100)}))
        self.identity.register_seeds_x(
            self.conn, ["a"], self._transporte({"a": self._perfil("1", "a", 250)}))
        linhas = self.conn.execute(
            "SELECT followers_count FROM actor_snapshot").fetchall()
        self.assertEqual([r["followers_count"] for r in linhas], [250])

    def test_sem_credito_para_o_lote(self):
        """Insistir sem crédito só gasta 5 segundos de espera por entrada."""
        rede = _Rede()
        def chamar(req, timeout=None):
            rede.urls.append(req.full_url)
            if "/oapi/my/info" in req.full_url:
                return _Resposta({"recharge_credits": 0, "total_bonus_credits": 0})
            return _Resposta({"status": "error", "msg": "insufficient credit balance"})
        tr = x_api.Transporte("k", intervalo=0, abrir=chamar)
        ok, falhas = self.identity.register_seeds_x(
            self.conn, ["a", "b", "c", "d"], tr)
        self.assertEqual(ok, [])
        self.assertEqual(len(falhas), 1)   # parou na primeira, não tentou as outras

    def test_a_semente_registrada_volta_em_seed_handles(self):
        """O elo que fecha o ciclo: o que `seeds` grava é o que `fetch` lê."""
        tr = self._transporte({"a": self._perfil("1", "a"),
                               "b": self._perfil("2", "b")})
        self.identity.register_seeds_x(self.conn, ["a", "b"], tr)
        self.assertEqual(self.identity.seed_handles(self.conn, "x"), ["a", "b"])


class TestCandidatosASemente(unittest.TestCase):
    """A resposta óbvia — "os de maior PageRank" — dá justamente quem NÃO
    precisa entrar na lista, porque ator muito amplificado chega de graça."""

    def setUp(self):
        import tempfile
        from nabote import db, identity
        self.db, self.identity = db, identity
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "t.db")
        db.migrate(self.conn, ROOT / "migrations")
        self.run_id = self.conn.execute(
            "INSERT INTO collection_run (source, kind, started_at) "
            "VALUES ('x_parquet','baseline',?)", (db.utcnow(),)).lastrowid
        self._ator = {}

    def tearDown(self):
        self.conn.close(); self._tmp.cleanup()

    def ator(self, handle):
        if handle not in self._ator:
            agora = db.utcnow() if False else self.db.utcnow()
            self._ator[handle] = self.conn.execute(
                "INSERT INTO actor (platform, platform_user_id, handle, tier,"
                " first_seen_at, last_seen_at) VALUES ('x',?,?,'C',?,?)",
                (f"id_{handle}", handle, agora, agora)).lastrowid
        return self._ator[handle]

    def amplifica(self, origem, alvos, quando="2023-06-19T12:00:00Z"):
        """Cada par (origem, alvo) vira um post e uma interação."""
        src = self.ator(origem)
        for i, alvo in enumerate(alvos):
            pid = self.conn.execute(
                "INSERT INTO post (platform, platform_post_id, actor_id,"
                " created_at, post_type, collected_at, run_id)"
                " VALUES ('x',?,?,?,'repost',?,?)",
                (f"{origem}_{alvo}_{i}_{quando}", src, quando, quando,
                 self.run_id)).lastrowid
            self.conn.execute(
                "INSERT INTO interaction (post_id, src_actor_id, dst_actor_id,"
                " kind, occurred_at) VALUES (?,?,?,'repost',?)",
                (pid, src, self.ator(alvo), quando))

    def test_ordena_por_alvos_distintos_e_nao_por_volume(self):
        """Quinhentos retuítes na mesma conta são UMA aresta de peso 500;
        duzentos em contas diferentes são duzentas arestas."""
        self.amplifica("martelo", ["famoso"] * 40)          # 40 interações, 1 alvo
        self.amplifica("espalhador", [f"a{i}" for i in range(12)])   # 12 alvos

        top = self.identity.candidatos_a_semente(self.conn, "x", limite=5)
        self.assertEqual(top[0]["handle"], "espalhador")
        self.assertEqual(top[0]["alvos"], 12)
        martelo = [r for r in top if r["handle"] == "martelo"][0]
        self.assertEqual((martelo["alvos"], martelo["interacoes"]), (1, 40))

    def test_o_mais_amplificado_nao_lidera(self):
        """@famoso recebe de todo mundo e não amplifica ninguém: chega de graça
        como Tier C. Pagar pela timeline dele é pagar por um nó que já viria."""
        for i in range(20):
            self.amplifica(f"fa{i}", ["famoso"])
        self.amplifica("espalhador", [f"a{i}" for i in range(9)])

        top = self.identity.candidatos_a_semente(self.conn, "x", limite=5)
        self.assertEqual(top[0]["handle"], "espalhador")
        self.assertNotIn("famoso", [r["handle"] for r in top])

    def test_classifica_as_quatro_situacoes(self):
        self.amplifica("espalhador", [f"a{i}" for i in range(10)])
        for i in range(30):
            self.amplifica(f"fa{i}", ["meio_famoso"])
        self.amplifica("meio_famoso", ["a1", "a2"])   # amplifica pouco, recebe muito
        self.amplifica("martelo", ["a1"] * 40)        # volume numa conta só
        for i in range(60):
            self.amplifica(f"fb{i}", ["hub"])
        self.amplifica("hub", [f"c{i}" for i in range(12)])  # amplifica E recebe

        por = {r["handle"]: r for r in
               self.identity.candidatos_a_semente(self.conn, "x", limite=99)}
        self.assertEqual(por["espalhador"]["razao"], "fábrica")
        self.assertEqual(por["meio_famoso"]["razao"], "voz")
        self.assertEqual(por["hub"]["razao"], "ambos")

    def test_volume_numa_conta_so_nao_e_ambos(self):
        """Quarenta retuítes na mesma conta produzem UMA aresta de peso 40:
        não entrega estrutura nem texto. Chamar de `ambos` numa tabela que se
        lê para decidir induz a pagar por quem não devia entrar."""
        self.amplifica("martelo", ["famoso"] * 40)
        por = {r["handle"]: r for r in
               self.identity.candidatos_a_semente(self.conn, "x", limite=99)}
        self.assertEqual(por["martelo"]["razao"], "pouco")
        self.assertEqual((por["martelo"]["alvos"], por["martelo"]["interacoes"]),
                         (1, 40))

    def test_quem_ja_esta_na_lista_nao_volta(self):
        self.amplifica("espalhador", [f"a{i}" for i in range(9)])
        self.amplifica("outro", [f"b{i}" for i in range(8)])
        top = self.identity.candidatos_a_semente(
            self.conn, "x", limite=10, excluir=["@Espalhador"])
        self.assertNotIn("espalhador", [r["handle"] for r in top])
        self.assertIn("outro", [r["handle"] for r in top])

    def test_conta_em_quantas_semanas_apareceu(self):
        """Quem só apareceu num pico não é boa semente permanente."""
        self.amplifica("constante", ["a1", "a2"], "2023-06-05T12:00:00Z")
        self.amplifica("constante", ["a3", "a4"], "2023-06-19T12:00:00Z")
        self.amplifica("pico", ["b1", "b2", "b3", "b4"], "2023-06-19T12:00:00Z")

        por = {r["handle"]: r for r in
               self.identity.candidatos_a_semente(self.conn, "x", limite=10)}
        self.assertEqual(por["constante"]["semanas"], 2)
        self.assertEqual(por["pico"]["semanas"], 1)

    def test_corte_por_data(self):
        self.amplifica("velho", [f"v{i}" for i in range(9)], "2023-01-10T12:00:00Z")
        self.amplifica("novo", [f"n{i}" for i in range(5)], "2023-06-19T12:00:00Z")
        top = self.identity.candidatos_a_semente(
            self.conn, "x", limite=10, desde="2023-06-01")
        self.assertEqual([r["handle"] for r in top], ["novo"])

    def test_nao_vaza_outra_plataforma(self):
        self.amplifica("do_x", [f"a{i}" for i in range(5)])
        agora = self.db.utcnow()
        bsky = self.conn.execute(
            "INSERT INTO actor (platform, platform_user_id, handle, tier,"
            " first_seen_at, last_seen_at) VALUES ('bluesky','did:plc:z','do_bsky',"
            "'C',?,?)", (agora, agora)).lastrowid
        pid = self.conn.execute(
            "INSERT INTO post (platform, platform_post_id, actor_id, created_at,"
            " post_type, collected_at, run_id) VALUES ('bluesky','p1',?,?,"
            "'repost',?,?)", (bsky, agora, agora, self.run_id)).lastrowid
        self.conn.execute(
            "INSERT INTO interaction (post_id, src_actor_id, dst_actor_id, kind,"
            " occurred_at) VALUES (?,?,?,'repost',?)",
            (pid, bsky, self.ator("do_x"), agora))

        handles = [r["handle"] for r in
                   self.identity.candidatos_a_semente(self.conn, "x", limite=10)]
        self.assertIn("do_x", handles)
        self.assertNotIn("do_bsky", handles)


class TestDespachoDoCLI(unittest.TestCase):
    """O `main` despacha por um dicionário de nomes, não pelo `set_defaults`
    do argparse. Registrar o subparser não basta — e um `--help` passa, porque
    nunca chega ao despacho. Foi assim que `candidatos` foi entregue quebrado."""

    def test_todo_subcomando_registrado_tem_funcao(self):
        from nabote import cli
        import inspect

        parser = cli.build_parser()
        sub = [a for a in parser._subparsers._group_actions][0]
        registrados = set(sub.choices)

        fonte = inspect.getsource(cli.main)
        sem_funcao = {n for n in registrados if f'"{n}":' not in fonte}
        self.assertEqual(sem_funcao, set(),
                         f"subcomando sem entrada no despacho: {sem_funcao}")

    def test_o_despacho_nao_tem_nome_que_o_parser_desconhece(self):
        """Entrada órfã no dicionário é comando que ninguém consegue chamar."""
        from nabote import cli
        import inspect, re

        parser = cli.build_parser()
        sub = [a for a in parser._subparsers._group_actions][0]
        fonte = inspect.getsource(cli.main)
        nomes = set(re.findall(r'"([a-z-]+)": cmd_\w+', fonte))
        self.assertEqual(nomes - set(sub.choices), set())


# --------------------------------------------------------------------------
# o 429, que a primeira execução real transformou em cinco sementes perdidas
# --------------------------------------------------------------------------

class TestRetentativaNo429(unittest.TestCase):
    """O 429 do tier gratuito não é o pedido estar errado: é o relógio do
    servidor discordando do nosso por uma fração de segundo.

    No primeiro registro de sementes de verdade, 5 dos 31 handles morreram
    assim — espalhados pelo lote, não em rajada, que é a assinatura de
    oscilação de rede contra uma margem apertada. Desistir num 429 obriga a
    refazer o lote, e refazer custa mais do que esperar.
    """

    @staticmethod
    def _rede_que_estoura(vezes: int):
        import io, urllib.error
        estado = {"restam": vezes}

        def abrir(req, timeout=None):
            if estado["restam"]:
                estado["restam"] -= 1
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests", {},
                    io.BytesIO(json.dumps(FIXTURE["erro_429"]).encode("utf-8")))
            return _Resposta({"data": {"id": "1", "userName": "ok"}})
        return abrir

    def test_espera_e_repete(self):
        dormidas: list[float] = []
        t = x_api.Transporte("k", intervalo=1, abrir=self._rede_que_estoura(1),
                             dormir=dormidas.append)
        self.assertEqual(t.get("/x")["data"]["id"], "1")
        self.assertTrue(any(d > 0 for d in dormidas),
                        "repetiu sem esperar — o 429 seguinte é certo")

    def test_a_espera_cresce_a_cada_tentativa(self):
        """Repetir com a mesma margem que acabou de falhar falha de novo."""
        dormidas: list[float] = []
        t = x_api.Transporte("k", intervalo=1, abrir=self._rede_que_estoura(2),
                             dormir=dormidas.append)
        t.get("/x")
        # o ritmo normal dorme `intervalo`; só o recuo do 429 dorme mais
        recuos = [d for d in dormidas if d > t.intervalo]
        self.assertEqual(len(recuos), 2, f"recuos: {dormidas}")
        self.assertGreater(recuos[1], recuos[0])

    def test_desiste_e_diz_por_que(self):
        t = x_api.Transporte("k", intervalo=0, abrir=self._rede_que_estoura(99),
                             dormir=lambda s: None)
        with self.assertRaises(x_api.ErroDoProvedor) as ctx:
            t.get("/x")
        self.assertTrue(ctx.exception.excesso)

    def test_erro_que_nao_e_de_taxa_nao_repete(self):
        """Repetir um 403 de chave errada gasta tempo e talvez crédito para
        receber exatamente a mesma resposta."""
        import io, urllib.error
        chamadas = {"n": 0}

        def abrir(req, timeout=None):
            chamadas["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {},
                io.BytesIO(json.dumps(FIXTURE["erro_403"]).encode("utf-8")))

        t = x_api.Transporte("k", intervalo=0, abrir=abrir, dormir=lambda s: None)
        with self.assertRaises(x_api.ErroDoProvedor):
            t.get("/x")
        self.assertEqual(chamadas["n"], 1)

    def test_a_margem_padrao_nao_e_de_quatro_por_cento(self):
        """5,2 s contra um limite de 5 s deixou 16% do lote real cair. A
        margem tem que cobrir oscilação de rede, não empatar com ela."""
        self.assertGreaterEqual(x_api.INTERVALO_PADRAO, 5.5)


# --------------------------------------------------------------------------
# a última linha do `seeds`, que é a que a pessoa lê para saber se funcionou
# --------------------------------------------------------------------------

class TestTotalDeSementes(unittest.TestCase):
    """`seeds --source x` gravou 26 atores e imprimiu "total de sementes no
    banco: 0". As duas afirmações estavam no mesmo parágrafo de saída.

    A contagem vinha de `seed_dids`, que filtra pela plataforma do Bluesky
    porque nasceu para montar o filtro do Jetstream. Número que contradiz a
    linha de cima destrói a confiança em toda a saída — inclusive na parte
    que estava certa.
    """

    def setUp(self):
        import tempfile
        from nabote import cli, db, identity
        self.cli, self.identity = cli, identity
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name)
        self.conn = db.connect(self.raiz / "s.db")
        db.migrate(self.conn, ROOT / "migrations")
        self.conn.close()
        (self.raiz / "lista.txt").write_text("?Fulano\nCiclano\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _rodar(self):
        import argparse, contextlib, io as _io, os
        perfis = {"Fulano": "111", "Ciclano": "222"}

        def abrir(req, timeout=None):
            if "/oapi/my/info" in req.full_url:
                return _Resposta({"recharge_credits": 0,
                                  "total_bonus_credits": 10000})
            nome = req.full_url.rsplit("userName=", 1)[-1]
            return _Resposta({"data": {"id": perfis[nome], "userName": nome,
                                       "name": nome, "description": "",
                                       "createdAt": "2012-08-15T01:22:19.000000Z"}})

        real = x_api.Transporte
        os.environ["NABOTE_X_API_KEY"] = "chave_falsa_de_teste"
        x_api.Transporte = lambda k, **kw: real(k, intervalo=0, abrir=abrir,
                                                dormir=lambda s: None)
        args = argparse.Namespace(db=self.raiz / "s.db",
                                  file=str(self.raiz / "lista.txt"),
                                  source="x", tier="A")
        saida = _io.StringIO()
        try:
            with contextlib.redirect_stdout(saida):
                self.cli.cmd_seeds(args)
        finally:
            x_api.Transporte = real
        return saida.getvalue()

    def test_o_total_conta_o_que_acabou_de_ser_gravado(self):
        saida = self._rodar()
        self.assertIn("2 registradas", saida)
        self.assertIn("total de sementes de x no banco: 2", saida)


# --------------------------------------------------------------------------
# o perfil que vem de carona — a economia central desta fonte
# --------------------------------------------------------------------------

class TestPerfilDeCarona(unittest.TestCase):
    """O objeto `author` vem embutido em cada tweet, e também dentro de
    `retweeted_tweet` e `quoted_tweet`. É o que permite um ator Tier C existir
    com nome e bio sem nunca custar uma requisição.

    Sem isto o grafo é legível só para quem decora id numérico: a saída da
    primeira amostra trouxe cinco atores com `display_name` e `bio` vazios,
    com o texto todo ali no payload já pago.
    """

    def test_o_autor_traz_nome_e_bio(self):
        ev = x_api.normalize_tweet(FIXTURE["retuite_real"])
        self.assertEqual(ev.actor_display_name, "Vergilio Sobrinho")
        # armadilha 2: a bio do autor embutido mora em `profile_bio`
        self.assertEqual(ev.actor_bio, "Deus, família e Flamengo!")

    def test_o_autor_traz_a_data_de_criacao_da_conta(self):
        """Em formato legado, aqui — armadilha 1."""
        ev = x_api.normalize_tweet(FIXTURE["retuite_real"])
        self.assertTrue(ev.actor_created_at.startswith("2009-05-13T22:49:07"))

    def test_o_alvo_do_retuite_tambem_traz_perfil(self):
        """Quem é retuitado nunca é coletado, e é justamente de quem se quer
        saber o nome no relatório."""
        alvo, = [a for a in x_api.normalize_tweet(FIXTURE["retuite_real"]).targets
                 if a.kind == "repost"]
        self.assertEqual(alvo.handle, "KimKataguiri")
        self.assertEqual(alvo.display_name, "Kim Kataguiri")

    def test_a_mencao_traz_o_nome_mas_nao_inventa_bio(self):
        """`user_mentions` tem `name`, não tem bio. Campo ausente vira None e
        não string vazia: None deixa o valor existente em paz, "" o apaga."""
        ev = x_api.normalize_tweet(FIXTURE["resposta_com_mencao_extra"])
        mencoes = [a for a in ev.targets if a.kind == "mention"]
        self.assertTrue(mencoes)
        self.assertIsNone(mencoes[0].bio)


# --------------------------------------------------------------------------
# o teto que não segurou nada
# --------------------------------------------------------------------------

class TestTetoContraSaldo(unittest.TestCase):
    """A primeira coleta real gastou 7.380 dos 9.737 créditos que havia, com
    um teto de US$ 1,00 configurado. O teto nunca disparou porque US$ 1,00 são
    100.000 créditos — dez vezes TODO o saldo. Teto acima do saldo não é teto.

    E o aviso prévio não ajudou: imprimiu tempo e teto, nenhum dos dois em
    dinheiro contra o que havia na conta.
    """

    def test_a_estimativa_conta_tweets_e_nao_requisicoes(self):
        """31 requisições custaram US$ 0,074 porque trouxeram 529 tweets. A
        conta por requisição erra por uma ordem de grandeza."""
        est = x_api.estimativa_usd(31)
        self.assertGreater(est, 0.05, "estimativa barata demais — conta por requisição?")
        self.assertLess(est, 0.15)

    def test_o_teto_nunca_passa_do_saldo(self):
        saldo_usd = 0.00187                       # o que sobrou da primeira coleta
        self.assertEqual(x_api.teto_efetivo(1.00, saldo_usd), saldo_usd)

    def test_sem_teto_configurado_o_saldo_vira_o_teto(self):
        """'nenhum teto' com dinheiro na conta é como a conta ficou vazia."""
        self.assertEqual(x_api.teto_efetivo(None, 0.05), 0.05)

    def test_teto_abaixo_do_saldo_e_respeitado(self):
        self.assertEqual(x_api.teto_efetivo(0.02, 0.05), 0.02)


class TestAvisoPrevio(unittest.TestCase):
    """O aviso prévio existe para a pessoa decidir ANTES de gastar. Com teto,
    tempo e nenhum número de dinheiro contra o saldo, ele não deu a informação
    que decidia: que o plano custava metade do que havia na conta."""

    def setUp(self):
        import tempfile
        from nabote import cli, db, identity, ingest
        self.cli, self.ingest = cli, ingest
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name)
        self.conn = db.connect(self.raiz / "s.db")
        db.migrate(self.conn, ROOT / "migrations")
        ingest.upsert_actor(self.conn, "x", "1", "A", "semente_um")
        ingest.upsert_actor(self.conn, "x", "2", "A", "semente_dois")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _preflight(self, creditos, teto=None):
        import argparse, contextlib, io as _io, os

        def abrir(req, timeout=None):
            return _Resposta({"recharge_credits": 0,
                              "total_bonus_credits": creditos})

        from nabote import sources
        real = sources.XApiSource
        os.environ["NABOTE_X_API_KEY"] = "chave_falsa_de_teste"
        os.environ.pop("NABOTE_BUDGET_USD_PER_CYCLE", None)
        falsa = lambda k, **kw: real(  # noqa: E731
            k, **{**kw, "intervalo": 0, "abrir": abrir, "dormir": lambda s: None})
        falsa.name = real.name        # `_fonte_x` lê o nome pela classe
        sources.XApiSource = falsa
        args = argparse.Namespace(query=None, teto_usd=teto, paginas=1, intervalo=5.5)
        saida = _io.StringIO()
        try:
            with contextlib.redirect_stdout(saida):
                self.cli._fonte_x(self.conn, args)
        finally:
            sources.XApiSource = real
        return saida.getvalue()

    def test_mostra_o_saldo_e_o_custo_estimado(self):
        saida = self._preflight(creditos=9737)
        self.assertIn("saldo", saida)
        self.assertIn("0.09737", saida)     # US$ do saldo, não só créditos
        self.assertIn("estimado", saida)

    def test_grita_quando_o_plano_nao_cabe_no_saldo(self):
        """187 créditos e um plano de US$ 0,006: precisa ser impossível de
        não ver."""
        saida = self._preflight(creditos=187)
        self.assertIn("NÃO CABE", saida.upper())

    def test_nao_grita_quando_cabe(self):
        saida = self._preflight(creditos=500_000)
        self.assertNotIn("NÃO CABE", saida.upper())

    def test_o_teto_exibido_e_o_que_vale(self):
        """Exibir US$ 1,00 quando o saldo é US$ 0,0019 foi o que deu a falsa
        sensação de proteção."""
        saida = self._preflight(creditos=187, teto=1.00)
        self.assertNotIn("US$ 1.00", saida)
        self.assertIn("0.00187", saida)


class TestStatusPorPlataforma(unittest.TestCase):
    """`status` conta o banco inteiro. Num banco onde o arquivo de 2023 tem
    milhões de linhas, isso enterra as 529 que acabaram de chegar do X —
    a resposta a "o que temos de dados" fica ilegível justamente na parte nova.
    """

    def setUp(self):
        import tempfile
        from nabote import cli, db, ingest
        from nabote.events import NormalizedEvent, Target
        self.cli, self.ingest = cli, ingest
        self._tmp = tempfile.TemporaryDirectory()
        self.caminho = Path(self._tmp.name) / "s.db"
        conn = db.connect(self.caminho)
        db.migrate(conn, ROOT / "migrations")
        run = ingest.start_run(conn, "twitterapi_io")
        ingest.upsert_actor(conn, "x", "1", "A", "semente_x")
        ingest.upsert_actor(conn, "bluesky", "did:plc:z", "A", "outra.bsky.social")
        ingest.handle_event(conn, run, NormalizedEvent(
            platform="x", kind="post", actor_uid="1", actor_handle="semente_x",
            occurred_at="2026-09-21T10:00:00Z", post_uid="p1", post_type="repost",
            targets=[Target(kind="repost", uid="99", handle="amplificado",
                            display_name="Quem Recebeu")]), ingest.Stats())
        ingest.handle_event(conn, run, NormalizedEvent(
            platform="bluesky", kind="post", actor_uid="did:plc:z",
            occurred_at="2026-09-21T10:00:00Z", post_uid="b1", post_type="original"),
            ingest.Stats())
        conn.commit(); conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def _status(self, platform=None):
        import argparse, contextlib, io as _io
        saida = _io.StringIO()
        with contextlib.redirect_stdout(saida):
            self.cli.cmd_status(argparse.Namespace(db=self.caminho, platform=platform))
        return saida.getvalue()

    def test_sem_filtro_continua_mostrando_o_banco_inteiro(self):
        self.assertIn("schema", self._status())

    def test_com_plataforma_separa_o_que_e_daquela_fonte(self):
        saida = self._status("x")
        self.assertIn("semente_x", saida)
        self.assertNotIn("outra.bsky.social", saida)

    def test_mostra_quem_recebeu_sem_nunca_ter_sido_coletado(self):
        """O ator Tier C é metade do grafo e não aparece em contagem de post."""
        saida = self._status("x")
        self.assertIn("amplificado", saida)
        self.assertIn("Quem Recebeu", saida)


class TestStatusNaoMisturaFontes(unittest.TestCase):
    """`--platform x` respondeu com 5,8 milhões de reposts a uma pergunta sobre
    uma coleta de 529 posts.

    O arquivo de 2023 entra com `platform='x'` porque É X. Plataforma diz de
    QUE REDE o dado é; ela não separa o que foi coletado ontem do que foi
    carregado de um zip de três anos atrás. A pergunta "o que esta coleta
    trouxe" se corta por FONTE.
    """

    def setUp(self):
        import tempfile
        from nabote import cli, db, ingest
        from nabote.events import NormalizedEvent, Target
        self.cli = cli
        self._tmp = tempfile.TemporaryDirectory()
        self.caminho = Path(self._tmp.name) / "s.db"
        conn = db.connect(self.caminho)
        db.migrate(conn, ROOT / "migrations")
        ingest.upsert_actor(conn, "x", "1", "A", "semente_x")

        def post(run, uid, alvo, alvo_handle):
            ingest.handle_event(conn, run, NormalizedEvent(
                platform="x", kind="post", actor_uid="1", actor_handle="semente_x",
                occurred_at="2026-09-21T10:00:00Z", post_uid=uid, post_type="repost",
                targets=[Target(kind="repost", uid=alvo, handle=alvo_handle)]),
                ingest.Stats())

        # o arquivo de 2023: uma fonte, muitos posts
        arquivo = ingest.start_run(conn, "x_parquet:2023.zip")
        for i in range(20):
            post(arquivo, f"velho{i}", "900", "alvo_do_arquivo")
        # a coleta de ontem: outra fonte, um post
        api = ingest.start_run(conn, "twitterapi_io")
        post(api, "novo1", "901", "alvo_da_api")
        conn.commit(); conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def _status(self, **kw):
        import argparse, contextlib, io as _io
        args = argparse.Namespace(db=self.caminho,
                                  **{"platform": None, "source": None, **kw})
        saida = _io.StringIO()
        with contextlib.redirect_stdout(saida):
            self.cli.cmd_status(args)
        return saida.getvalue()

    def test_por_fonte_ignora_o_arquivo(self):
        saida = self._status(source="x")
        self.assertIn("alvo_da_api", saida)
        self.assertNotIn("alvo_do_arquivo", saida)

    def test_por_fonte_conta_so_os_posts_daquela_fonte(self):
        """1 post, não 21."""
        saida = self._status(source="x")
        linha, = [l for l in saida.splitlines() if "semente_x" in l]
        self.assertIn("1 posts", linha)

    def test_por_plataforma_avisa_que_mistura(self):
        """A visão da plataforma inteira é legítima — silenciosa é que não."""
        saida = self._status(platform="x")
        self.assertIn("x_parquet:2023.zip", saida)
        self.assertIn("twitterapi_io", saida)
