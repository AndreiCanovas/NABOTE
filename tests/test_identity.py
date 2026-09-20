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
        provedor. Handle de X é só letra, número e sublinhado: qualquer outro
        caractere sobrando é anotação que vazou da curadoria para a chamada."""
        texto = (ROOT / "seeds" / "politica_br_x.txt").read_text(encoding="utf-8")
        entradas = identity.parse_seed_file(texto)
        self.assertTrue(entradas)
        for e in entradas:
            self.assertRegex(e, r"^[A-Za-z0-9_]{1,15}$", f"entrada suja: {e!r}")


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
