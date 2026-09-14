"""Testes do schema.

O que estes testes protegem não é "o SQL roda" — é que as regras que o plano
depende estejam aplicadas pelo BANCO, e não pela disciplina de quem escreve o
INSERT. Regra que só existe na cabeça do desenvolvedor não sobrevive a seis
meses de uso.

Rodar:  python -m unittest discover -s tests
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nabote import db  # noqa: E402


class SchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        self.conn = db.connect(self.path)
        db.migrate(self.conn, ROOT / "migrations")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # --- helpers -------------------------------------------------------------

    def _actor(self, platform="bluesky", uid="did:plc:aaa", tier="A", **kw) -> int:
        cur = self.conn.execute(
            "INSERT INTO actor (platform, platform_user_id, handle, tier, "
            "is_public_figure, public_figure_reason, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (platform, uid, kw.get("handle", uid), tier,
             kw.get("is_public_figure", 0), kw.get("public_figure_reason"),
             db.utcnow(), db.utcnow()),
        )
        return cur.lastrowid

    def _run(self, kind="baseline", label=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO collection_run (source, kind, campaign_label, started_at) "
            "VALUES (?,?,?,?)",
            ("bluesky_jetstream", kind, label, db.utcnow()),
        )
        return cur.lastrowid

    def _post(self, actor_id, run_id, pid="p1", ptype="original") -> int:
        cur = self.conn.execute(
            "INSERT INTO post (platform, platform_post_id, actor_id, created_at, "
            "post_type, collected_at, run_id) VALUES (?,?,?,?,?,?,?)",
            ("bluesky", pid, actor_id, db.utcnow(), ptype, db.utcnow(), run_id),
        )
        return cur.lastrowid


class TestMigration(SchemaTestCase):
    def test_migration_is_recorded(self):
        self.assertEqual(db.current_version(self.conn), 1)
        row = self.conn.execute(
            "SELECT name, applied_at FROM schema_migrations WHERE version = 1"
        ).fetchone()
        self.assertEqual(row["name"], "initial")
        self.assertTrue(row["applied_at"].endswith("Z"))

    def test_migrate_is_idempotent(self):
        applied = db.migrate(self.conn, ROOT / "migrations")
        self.assertEqual(applied, [])
        self.assertEqual(db.current_version(self.conn), 1)

    def test_expected_objects_exist(self):
        names = {
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        for expected in [
            "actor", "actor_snapshot", "collection_run", "raw_payload", "post",
            "interaction", "edge_window", "topic", "post_topic",
            "actor_topic_window", "topic_community_window", "actor_position",
            "actor_metric", "community", "actor_community", "export_log",
            "v_actor_current", "v_top_atores", "v_arestas_janela", "v_custo_por_run",
        ]:
            self.assertIn(expected, names, f"faltando: {expected}")

    def test_tables_are_strict(self):
        """STRICT é o que impede texto numa coluna INTEGER passar em silêncio."""
        actor_id = self._actor()
        run_id = self._run()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO post (platform, platform_post_id, actor_id, created_at, "
                "post_type, collected_at, run_id, like_count) VALUES (?,?,?,?,?,?,?,?)",
                ("bluesky", "px", actor_id, db.utcnow(), "original",
                 db.utcnow(), run_id, "muitos"),
            )


class TestInvariants(SchemaTestCase):
    def test_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO actor_snapshot (actor_id, snapshot_date) VALUES (?, ?)",
                (9999, "2026-09-15"),
            )

    def test_actor_identity_is_platform_scoped(self):
        self._actor(platform="bluesky", uid="shared-id")
        # mesmo id em outra plataforma é outro ator — não conflita
        self._actor(platform="x", uid="shared-id")
        with self.assertRaises(sqlite3.IntegrityError):
            self._actor(platform="bluesky", uid="shared-id")

    def test_campaign_requires_label(self):
        """Campanha sem rótulo é dado que não se consegue interpretar depois."""
        with self.assertRaises(sqlite3.IntegrityError):
            self._run(kind="campanha", label=None)
        self.assertIsInstance(self._run(kind="campanha", label="C-014"), int)

    def test_public_figure_requires_documented_reason(self):
        """Fronteira de publicação: o critério tem de estar registrado."""
        with self.assertRaises(sqlite3.IntegrityError):
            self._actor(uid="did:plc:pf", is_public_figure=1)
        self.assertIsInstance(
            self._actor(uid="did:plc:pf2", is_public_figure=1,
                        public_figure_reason="mandato eletivo"),
            int,
        )

    def test_self_interaction_is_rejected(self):
        a = self._actor(uid="did:plc:a")
        run = self._run()
        post = self._post(a, run)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO interaction (post_id, src_actor_id, dst_actor_id, "
                "kind, occurred_at) VALUES (?,?,?,?,?)",
                (post, a, a, "repost", db.utcnow()),
            )

    def test_ingestion_is_idempotent_by_platform_id(self):
        a = self._actor(uid="did:plc:a")
        run = self._run()
        self._post(a, run, pid="abc")
        with self.assertRaises(sqlite3.IntegrityError):
            self._post(a, run, pid="abc")

    def test_tier_c_actor_needs_no_collected_post(self):
        """O mecanismo que permite medir influência sem pagar para coletar."""
        author = self._actor(uid="did:plc:author", tier="A")
        target = self._actor(uid="did:plc:target", tier="C")
        run = self._run()
        # parent_post_id NULL: o post-alvo nunca foi coletado, mas o autor dele
        # é conhecido e a aresta existe.
        self.conn.execute(
            "INSERT INTO post (platform, platform_post_id, actor_id, created_at, "
            "post_type, parent_post_id, parent_platform_post_id, parent_actor_id, "
            "collected_at, run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("bluesky", "rt1", author, db.utcnow(), "repost", None, "remote-xyz",
             target, db.utcnow(), run),
        )
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM post WHERE parent_actor_id = ? "
            "AND parent_post_id IS NULL",
            (target,),
        ).fetchone()
        self.assertEqual(row["n"], 1)


class TestScope(SchemaTestCase):
    """O escopo é a correção que permite métrica global e temática coexistirem."""

    def test_same_actor_has_independent_metric_per_scope(self):
        a = self._actor(uid="did:plc:a")
        for scope, value in [("global", 0.011), ("topic:7", 0.094), ("topic:12", 0.031)]:
            self.conn.execute(
                "INSERT INTO actor_metric (actor_id, window_start, scope, metric, value) "
                "VALUES (?,?,?,?,?)",
                (a, "2026-09-15", scope, "pagerank", value),
            )
        rows = self.conn.execute(
            "SELECT scope, value FROM actor_metric WHERE actor_id = ? "
            "AND metric = 'pagerank' ORDER BY value DESC",
            (a,),
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["scope"], "topic:7")

        # o mesmo (ator, janela, escopo, métrica) continua único
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO actor_metric (actor_id, window_start, scope, metric, value) "
                "VALUES (?,?,?,?,?)",
                (a, "2026-09-15", "global", "pagerank", 0.02),
            )

    def test_actor_belongs_to_one_community_per_scope(self):
        a = self._actor(uid="did:plc:a")
        self.conn.execute(
            "INSERT INTO actor_community (actor_id, window_start, scope, community_id) "
            "VALUES (?,?,?,?)", (a, "2026-09-15", "global", 3))
        # em outro escopo pode estar em outra comunidade
        self.conn.execute(
            "INSERT INTO actor_community (actor_id, window_start, scope, community_id) "
            "VALUES (?,?,?,?)", (a, "2026-09-15", "topic:7", 1))
        # mas não em duas comunidades no mesmo escopo
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO actor_community (actor_id, window_start, scope, community_id) "
                "VALUES (?,?,?,?)", (a, "2026-09-15", "global", 4))


class TestCostTracking(SchemaTestCase):
    def test_cost_cannot_be_negative(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO collection_run (source, kind, started_at, cost_usd) "
                "VALUES (?,?,?,?)", ("twitterapi_io", "baseline", db.utcnow(), -1.0))

    def test_cost_view_separates_baseline_from_campaign(self):
        for kind, label, cost in [("baseline", None, 0.15), ("baseline", None, 0.15),
                                  ("campanha", "C-014", 0.17)]:
            self.conn.execute(
                "INSERT INTO collection_run (source, kind, campaign_label, started_at, "
                "cost_usd, status) VALUES (?,?,?,?,?,?)",
                ("twitterapi_io", kind, label, db.utcnow(), cost, "ok"))
        rows = {r["kind"]: r for r in self.conn.execute("SELECT * FROM v_custo_por_run")}
        self.assertAlmostEqual(rows["baseline"]["cost_usd"], 0.30, places=4)
        self.assertAlmostEqual(rows["campanha"]["cost_usd"], 0.17, places=4)
        self.assertEqual(rows["campanha"]["campaign_label"], "C-014")


if __name__ == "__main__":
    unittest.main()
