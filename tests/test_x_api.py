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
        """ATENÇÃO: este caso usa o bloco sintético do fixture. Nenhuma das
        amostras reais trouxe `retweeted_tweet` preenchido, então a forma foi
        inferida por simetria com `quoted_tweet`. Confirmar contra a API antes
        de confiar em número de amplificação."""
        ev = x_api.normalize_tweet(FIXTURE["retweet_sintetico"])
        self.assertEqual(ev.post_type, "repost")
        (alvo,) = ev.targets
        self.assertEqual((alvo.kind, alvo.uid), ("repost", "14594813"))

    def test_mencao_nao_e_emitida(self):
        """Armadilha 3: `entities` vem `{}`, sem `user_mentions`. Extrair do
        texto daria handle sem id, e handle muda — cada troca de nome viraria
        um ator novo. A lacuna fica registrada em vez de virar dado ruim."""
        for chave in ("last_tweets", "advanced_search"):
            tweets = x_api.tweets_da_resposta(FIXTURE[chave])
            for t in tweets:
                self.assertEqual(t.get("entities"), {})
        ev = x_api.normalize_tweet(FIXTURE["advanced_search"]["tweets"][0])
        self.assertNotIn("mention", [a.kind for a in ev.targets])


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
        fonte = x_api.XApiSource("k", contas=["ua", "ub", "uc"],
                                 teto_usd=0.05, intervalo=0, abrir=rede)
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


class TestLimiteDeTaxa(unittest.TestCase):
    def test_espera_entre_requisicoes(self):
        """1 req/5 s no tier gratuito. Sem a espera, a segunda chamada volta
        429 — e 429 gasta a requisição sem trazer dado."""
        rede = _Rede({"userName=": _pagina([], None)})
        fonte = x_api.XApiSource("k", contas=["ua", "ub"], intervalo=0.05, abrir=rede)
        import time as _t
        inicio = _t.monotonic()
        list(fonte.events())
        # saldo inicial + 2 timelines + 2 saldos = 5 requisições, 4 esperas
        self.assertGreaterEqual(_t.monotonic() - inicio, 0.05 * 4)


if __name__ == "__main__":
    unittest.main()
