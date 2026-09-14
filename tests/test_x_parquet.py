"""Testes do adaptador da base histórica do X.

As três armadilhas do formato — repr de Python em vez de JSON, data sem fuso,
e `user` sendo handle e não id — foram descobertas inspecionando o dado real.
Cada uma tem teste, porque cada uma passaria despercebida produzindo resultado
plausível: json.loads falhando vira aresta faltando, data sem fuso vira janela
errada, e handle no lugar do id faz o mesmo ator virar dois quando ele troca
de nome.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nabote.sources import x_parquet as xp  # noqa: E402


def linha(**over):
    base = {
        "created_at": "2023-01-25 02:53:06",
        "tweet_id": 1618079484053458946,
        "tweet_content": "texto do post",
        "user": "zetao0669",
        "user_info": "{'id': 721203846517809152, 'name': 'Donisete'}",
        "has_mention": False, "mentions": None,
        "is_reply": False, "reply_to": None,
        "is_quote": False, "quoted_from": None,
        "is_retweet": False, "retweeted_from": None,
        "hashtags": None,
    }
    base.update(over)
    return base


class TestPythonReprFields(unittest.TestCase):
    def test_single_quoted_dicts_are_parsed(self):
        """São repr de Python, não JSON — json.loads falharia em silêncio."""
        ev = xp.row_to_event(linha(
            is_retweet=True,
            retweeted_from="{'user': 'AlexSchwartsman', 'user_id': 370970579}"))
        self.assertEqual(len(ev.targets), 1)
        self.assertEqual(ev.targets[0].uid, "370970579")
        self.assertEqual(ev.targets[0].handle, "AlexSchwartsman")

    def test_malformed_nested_field_loses_the_edge_not_the_row(self):
        ev = xp.row_to_event(linha(is_retweet=True, retweeted_from="{isto não é válido"))
        self.assertIsNotNone(ev, "linha torta não pode sumir inteira")
        self.assertEqual(ev.targets, [])
        self.assertEqual(ev.post_type, "original")

    def test_accepts_already_parsed_dict(self):
        ev = xp.row_to_event(linha(
            is_retweet=True, retweeted_from={"user": "x", "user_id": 5}))
        self.assertEqual(ev.targets[0].uid, "5")


class TestDates(unittest.TestCase):
    def test_space_separated_without_zone_becomes_utc_iso(self):
        ev = xp.row_to_event(linha(created_at="2023-01-25 02:53:06"))
        self.assertEqual(ev.occurred_at, "2023-01-25T02:53:06Z")

    def test_other_shapes_also_parse(self):
        for bruto in ["2023-01-25T02:53:06Z", "2023-01-25T02:53:06",
                      "2023-01-25 02:53:06.123", "2023-01-25 02:53:06+00:00"]:
            with self.subTest(bruto=bruto):
                ev = xp.row_to_event(linha(created_at=bruto))
                self.assertTrue(ev.occurred_at.startswith("2023-01-25T02:53:06"))

    def test_unparseable_date_drops_the_row(self):
        """Sem data não há janela, e post sem janela corrompe a série."""
        self.assertIsNone(xp.row_to_event(linha(created_at="ontem à tarde")))


class TestIdentity(unittest.TestCase):
    def test_stable_id_comes_from_user_info_handle_from_user(self):
        ev = xp.row_to_event(linha())
        self.assertEqual(ev.actor_uid, "721203846517809152")
        self.assertEqual(ev.actor_handle, "zetao0669")

    def test_falls_back_to_handle_when_user_info_is_broken(self):
        ev = xp.row_to_event(linha(user_info="{quebrado"))
        self.assertEqual(ev.actor_uid, "zetao0669")
        self.assertEqual(ev.actor_handle, "zetao0669")

    def test_row_without_any_identity_is_dropped(self):
        self.assertIsNone(xp.row_to_event(linha(user=None, user_info=None)))


class TestEdges(unittest.TestCase):
    def test_each_reference_type_maps_to_its_kind(self):
        casos = [("is_retweet", "retweeted_from", "repost"),
                 ("is_reply", "reply_to", "reply"),
                 ("is_quote", "quoted_from", "quote")]
        for flag, coluna, kind in casos:
            with self.subTest(kind=kind):
                ev = xp.row_to_event(linha(**{flag: True,
                                              coluna: "{'user': 'alvo', 'user_id': 77}"}))
                self.assertEqual(ev.post_type, kind)
                self.assertEqual([(t.kind, t.uid) for t in ev.targets], [(kind, "77")])

    def test_post_type_follows_precedence_but_all_edges_survive(self):
        """Um post pode ser retuíte E citação; o rótulo é um, as arestas são todas."""
        ev = xp.row_to_event(linha(
            is_retweet=True, retweeted_from="{'user': 'a', 'user_id': 1}",
            is_quote=True, quoted_from="{'user': 'b', 'user_id': 2}"))
        self.assertEqual(ev.post_type, "repost")
        self.assertEqual({(t.kind, t.uid) for t in ev.targets},
                         {("repost", "1"), ("quote", "2")})

    def test_mentions_become_edges_with_handle(self):
        ev = xp.row_to_event(linha(
            mentions="[{'id': '8236782', 'username': 'choracuica'}]"))
        self.assertEqual([(t.kind, t.uid, t.handle) for t in ev.targets],
                         [("mention", "8236782", "choracuica")])

    def test_flag_without_target_yields_no_edge(self):
        ev = xp.row_to_event(linha(is_retweet=True, retweeted_from=None))
        self.assertEqual(ev.targets, [])


class TestMemberNames(unittest.TestCase):
    def test_filename_yields_date_and_search_term(self):
        """O termo é o rótulo de tópico: a base foi coletada por Trending Topic."""
        self.assertEqual(xp.parse_member_name("2023-01-24-CPMF.parquet"),
                         ("2023-01-24", "CPMF"))
        self.assertEqual(xp.parse_member_name("2023-01-08-#GolpeDeEstado.parquet"),
                         ("2023-01-08", "#GolpeDeEstado"))
        self.assertEqual(xp.parse_member_name("2023-01-08-Alexandre de Moraes.parquet"),
                         ("2023-01-08", "Alexandre de Moraes"))

    def test_unexpected_name_returns_none_instead_of_raising(self):
        self.assertEqual(xp.parse_member_name("leiame.txt"), (None, None))


if __name__ == "__main__":
    unittest.main()
