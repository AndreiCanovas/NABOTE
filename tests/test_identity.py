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
        from nabote.ingest import upsert_actor
        upsert_actor(self.conn, "did:plc:antigo", "C")
        identity.register_seeds(self.conn, ["did:plc:antigo"], tier="A")
        row = self.conn.execute(
            "SELECT tier FROM actor WHERE platform_user_id = 'did:plc:antigo'").fetchone()
        self.assertEqual(row["tier"], "A")

    def test_tier_c_actors_are_not_seeds(self):
        from nabote.ingest import upsert_actor
        upsert_actor(self.conn, "did:plc:soalvo", "C")
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
