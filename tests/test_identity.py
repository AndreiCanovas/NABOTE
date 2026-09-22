"""Testes da resolução de handle e do registro de sementes.

O resolvedor é injetável justamente para que isto rode sem rede: o que
precisa ser testado é a normalização, o tratamento de falha parcial e a regra
de tier — não o cliente HTTP.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nabote import db, identity  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_strips_arroba_and_lowercases(self):
        self.assertEqual(identity.normalize_handle("@Fulano.BSky.Social"),
                         "fulano.bsky.social")
        self.assertEqual(identity.normalize_handle("  ciclano.test  "), "ciclano.test")

    def test_bare_name_gets_default_domain(self):
        """Esquecer o domínio é o erro de digitação mais comum da lista."""
        self.assertEqual(identity.normalize_handle("fulano"), "fulano.bsky.social")

    def test_custom_domain_is_preserved(self):
        self.assertEqual(identity.normalize_handle("@jornal.com.br"), "jornal.com.br")


class TestSeedFile(unittest.TestCase):
    def test_comments_and_blanks_are_ignored(self):
        texto = """
        # lista de política
        fulano.bsky.social    # comentário na linha
        did:plc:abc123

        @ciclano
        """
        self.assertEqual(identity.parse_seed_file(texto),
                         ["fulano.bsky.social", "did:plc:abc123", "@ciclano"])

    def test_marca_de_incerteza_nao_gruda_no_handle(self):
        """`?handle` é anotação do curador — "não confirmei este" — e está na
        lista de X para ser resolvida no registro, que é quem sabe se a conta
        existe. Se o `?` chegasse ao provedor, toda entrada marcada falharia,
        gastando uma requisição para dizer que `?Fulano` não existe."""
        texto = "?FernandoHaddad   # confirmar\n?ErikaHilton\nLulaOficial\n"
        self.assertEqual(identity.parse_seed_file(texto),
                         ["FernandoHaddad", "ErikaHilton", "LulaOficial"])

    def test_a_lista_de_x_versionada_sai_limpa(self):
        """O teste acima prova a regra; este prova o arquivo que de fato vai ao
        provedor. Duas formas válidas, e nada além: handle de X, que é só letra,
        número e sublinhado; ou `id:<n>`, para a conta cujo handle óbvio está
        ocupado por homônimo. Qualquer outro caractere sobrando é anotação que
        vazou da curadoria para a chamada."""
        texto = (ROOT / "seeds" / "politica_br_x.txt").read_text(encoding="utf-8")
        entradas = identity.parse_seed_file(texto)
        self.assertTrue(entradas)
        for e in entradas:
            self.assertRegex(e, r"^(id:[0-9]+|[A-Za-z0-9_]{1,15})$",
                             f"entrada suja: {e!r}")

    def test_a_lista_nao_repete_a_mesma_conta(self):
        """Corrigir um handle para `id:` deixa a entrada antiga no arquivo com
        facilidade, e a conta viraria duas linhas — uma paga requisição à toa,
        e as duas disputam o mesmo ator."""
        texto = (ROOT / "seeds" / "politica_br_x.txt").read_text(encoding="utf-8")
        entradas = [e.lower() for e in identity.parse_seed_file(texto)]
        repetidas = {e for e in entradas if entradas.count(e) > 1}
        self.assertEqual(repetidas, set())


class TestRegisterSeeds(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "s.db")
        db.migrate(self.conn, ROOT / "migrations")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    @staticmethod
    def _fake_resolver(mapping):
        def resolve(handle):
            if handle not in mapping:
                raise identity.ResolveError(f"handle inexistente: {handle}")
            return mapping[handle]
        return resolve

    def test_handles_and_dids_both_register(self):
        ok, falhas = identity.register_seeds(
            self.conn, ["@Fulano", "did:plc:direto01"], tier="A",
            resolver=self._fake_resolver({"fulano.bsky.social": "did:plc:resolvido01"}))
        self.assertEqual(falhas, [])
        self.assertEqual(dict(ok), {"fulano.bsky.social": "did:plc:resolvido01",
                                    "did:plc:direto01": "did:plc:direto01"})
        self.assertEqual(sorted(identity.seed_dids(self.conn)),
                         ["did:plc:direto01", "did:plc:resolvido01"])

    def test_one_bad_handle_does_not_lose_the_rest(self):
        """Um handle errado na lista não pode derrubar a curadoria inteira."""
        ok, falhas = identity.register_seeds(
            self.conn, ["bom.bsky.social", "naoexiste.bsky.social", "outro.bsky.social"],
            resolver=self._fake_resolver({"bom.bsky.social": "did:plc:b1",
                                          "outro.bsky.social": "did:plc:o1"}))
        self.assertEqual(len(ok), 2)
        self.assertEqual(len(falhas), 1)
        self.assertIn("naoexiste", falhas[0][0])

    def test_handle_is_stored_alongside_the_did(self):
        identity.register_seeds(
            self.conn, ["fulano"], resolver=self._fake_resolver(
                {"fulano.bsky.social": "did:plc:x1"}))
        row = self.conn.execute(
            "SELECT handle, tier FROM actor WHERE platform_user_id = 'did:plc:x1'").fetchone()
        self.assertEqual(row["handle"], "fulano.bsky.social")
        self.assertEqual(row["tier"], "A")

    def test_registering_promotes_a_previously_observed_actor(self):
        """Alguém visto antes só como alvo (C) vira semente ao ser curado."""
        from nabote.atproto import PLATFORM
        from nabote.ingest import upsert_actor
        upsert_actor(self.conn, PLATFORM, "did:plc:antigo", "C")
        identity.register_seeds(self.conn, ["did:plc:antigo"], tier="A")
        row = self.conn.execute(
            "SELECT tier FROM actor WHERE platform_user_id = 'did:plc:antigo'").fetchone()
        self.assertEqual(row["tier"], "A")

    def test_tier_c_actors_are_not_seeds(self):
        from nabote.atproto import PLATFORM
        from nabote.ingest import upsert_actor
        upsert_actor(self.conn, PLATFORM, "did:plc:soalvo", "C")
        identity.register_seeds(self.conn, ["did:plc:semente"], tier="A")
        self.assertEqual(identity.seed_dids(self.conn), ["did:plc:semente"])

    def test_network_failure_reports_actionable_message(self):
        import urllib.error

        def offline(handle):
            raise identity.ResolveError(
                "sem acesso à API do Bluesky ao resolver x (offline). "
                "Rode numa máquina com saída para public.api.bsky.app.")

        ok, falhas = identity.register_seeds(self.conn, ["x.bsky.social"], resolver=offline)
        self.assertEqual(ok, [])
        self.assertIn("public.api.bsky.app", falhas[0][1])


if __name__ == "__main__":
    unittest.main()


class TestSearchParsing(unittest.TestCase):
    """O que importa aqui é a extração dos campos, não o cliente HTTP."""

    def test_extracts_the_fields_a_human_needs_to_choose(self):
        import json as _json
        from unittest.mock import patch

        payload = _json.dumps({"actors": [
            {"handle": "fulano.bsky.social", "did": "did:plc:a1",
             "displayName": "Fulano de Tal", "followersCount": 12345,
             "description": "linha um\nlinha dois"},
            {"handle": "parodia.bsky.social", "did": "did:plc:a2"},
        ]}).encode()

        class FakeResponse:
            def read(self): return payload
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            found = identity.search_actors("Fulano")

        self.assertEqual(len(found), 2)
        self.assertEqual(found[0]["handle"], "fulano.bsky.social")
        self.assertEqual(found[0]["followers"], 12345)
        # quebra de linha na bio estragaria o alinhamento da tabela no terminal
        self.assertNotIn("\n", found[0]["description"])
        # conta sem os campos opcionais não pode quebrar a listagem
        self.assertEqual(found[1]["display_name"], "")
        self.assertIsNone(found[1]["followers"])


class TestUnofficialFlag(unittest.TestCase):
    """Marcar conta que se declara não-oficial é a defesa mais barata contra o
    erro mais caro: atribuir discurso de paródia a uma figura pública real."""

    def test_flags_self_declared_fan_and_parody_accounts(self):
        casos = [
            ("Jair M. Bolsonaro", "CONTA DE MEME⚠️ Não tem ligação com nenhum politico"),
            ("Jair Messias Bolsonaro", "38° Presidente. 🚨PÁGINA DE FANS🚨"),
            ("Nikolas F. de Oliveira", "41• Presidente 🇧🇷 CONTA DE FÃ 🇧🇷"),
            ("Arthur Lira", "NÃO sou o político he/him"),
            ("Silas Não o Malafaia", "onde queres agito sou sossego"),
            ("Not Guilherme Boulos", "Neto de estivador, filho de professora."),
            ("Lucas Constantino - não sou o", "eu sou lucascons"),
            ("Fernando Haddad", "Perfil não oficial. Divulgando atividades do ministro"),
            ("Apoiadores Do Guilherme Boulos", "Perfil de apoio a Guilherme Boulos"),
            ("Romeu Zema", "fã clube do pior governador do brasil"),
        ]
        for nome, bio in casos:
            with self.subTest(nome=nome):
                self.assertIsNotNone(identity.flag_declared_unofficial(nome, bio),
                                     f"não marcou: {nome}")

    def test_does_not_flag_plausible_official_accounts(self):
        casos = [
            ("Fernando Haddad",
             "Ministro da Fazenda de Lula, Ex-Ministro da Educação, Ex-Prefeito de São Paulo"),
            ("ERIKA HILTON", "Deputada do PSOL ☀️ por São Paulo."),
            ("Marina Silva", "Ministra do Meio Ambiente e Mudança do Clima"),
            ("Reinaldo Azevedo", "Jornalista. Siga no Reconversa (YouTube), na BandNews FM"),
            # sobrenome que CONTÉM 'not' não pode disparar — daí a borda de palavra
            ("Renato Notaro", "Analista de políticas públicas"),
            ("Míriam Leitão", "Jornalista e escritora"),
        ]
        for nome, bio in casos:
            with self.subTest(nome=nome):
                self.assertIsNone(identity.flag_declared_unofficial(nome, bio),
                                  f"marcou indevidamente: {nome}")

    def test_flag_is_a_signal_not_a_verdict(self):
        """Bio vazia não diz nada — e é justamente o caso mais ambíguo."""
        self.assertIsNone(identity.flag_declared_unofficial("", ""))


class TestProcurarAtor(unittest.TestCase):
    """Quatro sementes resolveram para homônimos: o registro pediu
    `fernandohaddad` e recebeu uma conta de 46 tweets e 261 seguidores.

    O handle óbvio de uma figura pública costuma estar ocupado por conta
    abandonada, de fã ou de paródia, e o registro não tem como saber — ele
    pergunta um nome e recebe um id, e o id É daquele nome.

    Quem sabe é o arquivo de 2023, que já está no disco: a conta de verdade
    aparece lá recebendo milhares de arestas, e a homônima não aparece. É a
    pergunta respondida por dado que já foi pago.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "p.db")
        db.migrate(self.conn, ROOT / "migrations")
        from nabote import ingest
        from nabote.events import NormalizedEvent, Target
        run = ingest.start_run(self.conn, "x_parquet:2023.zip")
        real = ingest.upsert_actor(self.conn, "x", "43163406", "C", "Haddad_Fernando")
        ingest.upsert_actor(self.conn, "x", "999", "C", "fernandohaddad")
        for i in range(30):                      # o de verdade é muito citado
            ingest.handle_event(self.conn, run, NormalizedEvent(
                platform="x", kind="post", actor_uid=f"amp{i}",
                actor_handle=f"quem_amplifica{i}", occurred_at="2023-05-01T10:00:00Z",
                post_uid=f"p{i}", post_type="repost",
                targets=[Target(kind="repost", uid="43163406",
                                handle="Haddad_Fernando")]), ingest.Stats())
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_acha_pelo_pedaco_do_handle(self):
        achados = identity.procurar_ator(self.conn, "x", "haddad")
        self.assertEqual({a["handle"] for a in achados},
                         {"Haddad_Fernando", "fernandohaddad"})

    def test_ordena_por_quem_e_de_fato_citado(self):
        """É a contagem de arestas recebidas que separa a conta real da
        homônima — não o handle, que é justamente o que engana."""
        achados = identity.procurar_ator(self.conn, "x", "haddad")
        self.assertEqual(achados[0]["handle"], "Haddad_Fernando")
        self.assertEqual(achados[0]["recebidas"], 30)
        self.assertEqual(achados[1]["recebidas"], 0)

    def test_nao_acha_de_outra_plataforma(self):
        from nabote import ingest
        ingest.upsert_actor(self.conn, "bluesky", "did:plc:h", "C", "haddad.bsky.social")
        achados = identity.procurar_ator(self.conn, "x", "haddad")
        self.assertNotIn("haddad.bsky.social", {a["handle"] for a in achados})

    def test_busca_vazia_devolve_lista_vazia(self):
        self.assertEqual(identity.procurar_ator(self.conn, "x", "zzzznaoexiste"), [])


class TestCorrigirSemente(unittest.TestCase):
    """Uma semente pode estar errada, e descobrir isso não tinha consequência.

    Registrar promove tier C → A e `upsert_actor` NUNCA rebaixa, de propósito:
    tier é decisão de curadoria e não pode oscilar com a ordem de chegada dos
    eventos. Só que isso deixava a curadoria sem a outra metade — quatro contas
    homônimas ficariam tier A para sempre, coletadas toda semana, gastando
    página de API numa conta de 1 tweet.

    E o conserto não deveria custar requisição: o id da conta certa já está no
    arquivo de 2023, então promover por id é trabalho de banco, não de rede.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "c.db")
        db.migrate(self.conn, ROOT / "migrations")
        from nabote import ingest
        ingest.upsert_actor(self.conn, "x", "372649342", "A", "ErikaHilton")
        ingest.upsert_actor(self.conn, "x", "738143559920934912", "C", "ErikakHilton")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _tier(self, uid):
        r = self.conn.execute("SELECT tier FROM actor WHERE platform_user_id = ?",
                              (uid,)).fetchone()
        return r["tier"] if r else None

    def test_promove_por_id_sem_tocar_a_rede(self):
        """`transporte=None` é o teste: se tentasse resolver, levantaria."""
        ok, falhas = identity.register_seeds_x(
            self.conn, ["id:738143559920934912"], transporte=None)
        self.assertEqual(falhas, [])
        self.assertEqual(self._tier("738143559920934912"), "A")

    def test_promover_id_desconhecido_falha_sem_inventar_ator(self):
        """Promover pressupõe que o ator existe. Criar um ator vazio a partir
        de um id digitado errado seria pior que recusar."""
        ok, falhas = identity.register_seeds_x(
            self.conn, ["id:000000000"], transporte=None)
        self.assertEqual(ok, [])
        self.assertEqual(len(falhas), 1)
        self.assertIsNone(self._tier("000000000"))

    def test_rebaixar_tira_da_coleta(self):
        identity.remover_sementes(self.conn, "x", ["ErikaHilton"])
        self.assertEqual(self._tier("372649342"), "C")

    def test_rebaixar_aceita_id(self):
        identity.remover_sementes(self.conn, "x", ["id:372649342"])
        self.assertEqual(self._tier("372649342"), "C")

    def test_rebaixar_preserva_o_que_a_conta_ja_produziu(self):
        """Rebaixar é 'pare de coletar', não 'apague o que veio'. O post e a
        aresta de uma semana em que ela FOI coletada continuam sendo fato."""
        from nabote import ingest
        from nabote.events import NormalizedEvent
        run = ingest.start_run(self.conn, "twitterapi_io")
        ingest.handle_event(self.conn, run, NormalizedEvent(
            platform="x", kind="post", actor_uid="372649342",
            actor_handle="ErikaHilton", occurred_at="2026-09-21T10:00:00Z",
            post_uid="p1", post_type="original"), ingest.Stats())
        identity.remover_sementes(self.conn, "x", ["ErikaHilton"])
        n = self.conn.execute("SELECT COUNT(*) n FROM post").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_rebaixar_quem_nao_e_semente_nao_quebra(self):
        self.assertEqual(identity.remover_sementes(self.conn, "x", ["ninguem"]), [])


class TestCoberturaDaBusca(unittest.TestCase):
    """`quem` responde com o arquivo histórico, e o arquivo tem recorte.

    `pablomarcal` apareceu com 3 arestas recebidas — não porque o handle esteja
    errado, mas porque o arquivo cobre abril a junho de 2023 e Pablo Marçal
    ficou nacionalmente conhecido na eleição de 2024. Conta ausente do recorte
    parece pequena, e "parece pequena" foi exatamente o sinal que mandei o
    usuário usar para julgar handle errado.

    Uma ferramenta que devolve um ranking sem dizer sobre QUE PERÍODO ele fala
    convida à leitura errada. O período está no banco.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "c.db")
        db.migrate(self.conn, ROOT / "migrations")
        from nabote import ingest
        from nabote.events import NormalizedEvent
        run = ingest.start_run(self.conn, "x_parquet:2023.zip")
        for i, quando in enumerate(("2023-04-19T10:00:00Z", "2023-06-25T10:00:00Z")):
            ingest.handle_event(self.conn, run, NormalizedEvent(
                platform="x", kind="post", actor_uid=f"a{i}", actor_handle=f"alguem{i}",
                occurred_at=quando, post_uid=f"p{i}", post_type="original"),
                ingest.Stats())
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_devolve_o_periodo_que_o_ranking_cobre(self):
        cob = identity.cobertura_do_arquivo(self.conn, "x")
        self.assertEqual(cob["de"], "2023-04-19")
        self.assertEqual(cob["ate"], "2023-06-25")
        self.assertEqual(cob["posts"], 2)

    def test_banco_sem_historico_nao_finge_cobertura(self):
        outro = db.connect(Path(self._tmp.name) / "vazio.db")
        db.migrate(outro, ROOT / "migrations")
        self.assertIsNone(identity.cobertura_do_arquivo(outro, "x"))
        outro.close()
