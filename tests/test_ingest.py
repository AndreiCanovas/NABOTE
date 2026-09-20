"""Testes da ingestão, contra a fixture sintética.

O foco é o que o parser DEDUZ, não o que ele copia: quem virou aresta de quem,
qual o tipo do post, e o que acontece quando o alvo nunca foi coletado.

Rodar:  python -m unittest discover -s tests
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nabote import atproto, db, ingest  # noqa: E402
from nabote.events import NormalizedEvent  # noqa: E402
from nabote.sources import FixtureSource  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "jetstream_sintetico.jsonl"

A = "did:plc:exemplo0001seed"   # semente, autor
B = "did:plc:exemplo0002seed"   # semente, autor
C = "did:plc:exemplo0003alvo"   # nunca coletado — Tier C
D = "did:plc:exemplo0004alvo"   # só mencionado


class TestAtUri(unittest.TestCase):
    def test_parses_author_from_uri(self):
        uri = atproto.parse_at_uri("at://did:plc:abc/app.bsky.feed.post/3kx")
        self.assertEqual(uri.did, "did:plc:abc")
        self.assertEqual(uri.collection, "app.bsky.feed.post")
        self.assertEqual(uri.rkey, "3kx")

    def test_malformed_uri_returns_none_instead_of_raising(self):
        for bad in ["", "http://x/y/z", "at://", "at://did/only-two", None, 42]:
            self.assertIsNone(atproto.parse_at_uri(bad))


class TestNormalize(unittest.TestCase):
    def test_like_is_ignored(self):
        event = {"did": A, "time_us": 1, "kind": "commit",
                 "commit": {"operation": "create", "collection": "app.bsky.feed.like",
                            "rkey": "k", "record": {}}}
        self.assertIsNone(atproto.normalize(event))

    def test_reply_and_quote_in_same_post_yields_both_edges(self):
        event = {"did": A, "time_us": 1, "kind": "commit", "commit": {
            "operation": "create", "collection": "app.bsky.feed.post", "rkey": "z",
            "record": {"text": "x", "createdAt": "2026-09-15T10:00:00Z",
                       "reply": {"parent": {"uri": f"at://{B}/app.bsky.feed.post/1"}},
                       "embed": {"$type": "app.bsky.embed.record",
                                 "record": {"uri": f"at://{C}/app.bsky.feed.post/2"}}}}}
        ev = atproto.normalize(event)
        # rótulo único é 'reply' (tem precedência), mas as duas arestas saem
        self.assertEqual(ev.post_type, "reply")
        self.assertEqual({(t.kind, t.uid) for t in ev.targets},
                         {("reply", B), ("quote", C)})

    def test_quote_inside_record_with_media(self):
        event = {"did": A, "time_us": 1, "kind": "commit", "commit": {
            "operation": "create", "collection": "app.bsky.feed.post", "rkey": "z",
            "record": {"text": "x", "embed": {
                "$type": "app.bsky.embed.recordWithMedia",
                "record": {"record": {"uri": f"at://{C}/app.bsky.feed.post/9"}}}}}}
        ev = atproto.normalize(event)
        self.assertEqual(ev.post_type, "quote")
        self.assertEqual(ev.targets[0].uid, C)

    def test_delete_carries_no_record(self):
        event = {"did": A, "time_us": 1, "kind": "commit", "commit": {
            "operation": "delete", "collection": "app.bsky.feed.post", "rkey": "p9"}}
        ev = atproto.normalize(event)
        self.assertEqual(ev.kind, "delete")
        self.assertEqual(ev.post_uid, f"{A}/app.bsky.feed.post/p9")
        self.assertIsNone(ev.text)


class IngestTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "t.db")
        db.migrate(self.conn, ROOT / "migrations")
        self.run_id, self.stats = ingest.ingest(
            self.conn, FixtureSource(FIXTURE), author_tier="A")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _actor(self, did):
        return self.conn.execute(
            "SELECT * FROM actor WHERE platform_user_id = ?", (did,)).fetchone()

    def _edges(self):
        rows = self.conn.execute(
            "SELECT s.platform_user_id AS src, d.platform_user_id AS dst, i.kind "
            "FROM interaction i JOIN actor s ON s.actor_id = i.src_actor_id "
            "JOIN actor d ON d.actor_id = i.dst_actor_id").fetchall()
        return {(r["src"], r["dst"], r["kind"]) for r in rows}


class TestIngestGraph(IngestTestCase):
    def test_edges_are_exactly_what_the_records_imply(self):
        self.assertEqual(self._edges(), {
            (B, C, "reply"),      # resposta a post de ator nunca coletado
            (A, C, "quote"),      # citação
            (B, D, "mention"),    # menção
            (B, A, "mention"),    # menção
            (A, C, "repost"),     # repost
        })

    def test_self_reply_creates_post_but_no_edge(self):
        row = self.conn.execute(
            "SELECT post_type FROM post WHERE platform_post_id LIKE ?",
            (f"{A}/app.bsky.feed.post/p005",)).fetchone()
        self.assertEqual(row["post_type"], "reply")
        self.assertNotIn((A, A, "reply"), self._edges())

    def test_tier_c_actor_exists_without_ever_being_collected(self):
        """O mecanismo que corta o custo de coleta: influência medida pelos outros."""
        target = self._actor(C)
        self.assertIsNotNone(target)
        self.assertEqual(target["tier"], "C")
        posts = self.conn.execute(
            "SELECT COUNT(*) AS n FROM post WHERE actor_id = ?",
            (target["actor_id"],)).fetchone()["n"]
        self.assertEqual(posts, 0)
        recebidas = self.conn.execute(
            "SELECT COUNT(*) AS n FROM interaction WHERE dst_actor_id = ?",
            (target["actor_id"],)).fetchone()["n"]
        self.assertEqual(recebidas, 3)

    def test_tier_rises_when_a_target_later_turns_out_to_be_an_author(self):
        """A ordem de chegada dos eventos é aleatória; o tier não pode depender dela."""
        conn = db.connect(Path(self._tmp.name) / "ordem.db")
        db.migrate(conn, ROOT / "migrations")
        run = ingest.start_run(conn, "t")
        stats = ingest.Stats()

        # primeiro C aparece só como alvo de um repost de B
        ingest.handle_event(conn, run, atproto.normalize({
            "did": B, "time_us": 1, "kind": "commit", "commit": {
                "operation": "create", "collection": "app.bsky.feed.repost", "rkey": "r1",
                "record": {"subject": {"uri": f"at://{C}/app.bsky.feed.post/x"}}}}),
            stats, author_tier="A")
        self.assertEqual(conn.execute(
            "SELECT tier FROM actor WHERE platform_user_id=?", (C,)).fetchone()["tier"], "C")

        # depois um post do próprio C é coletado: ele estava na lista o tempo todo
        ingest.handle_event(conn, run, atproto.normalize({
            "did": C, "time_us": 2, "kind": "commit", "commit": {
                "operation": "create", "collection": "app.bsky.feed.post", "rkey": "q1",
                "record": {"text": "oi", "createdAt": "2026-09-15T10:00:00Z"}}}),
            stats, author_tier="A")
        self.assertEqual(conn.execute(
            "SELECT tier FROM actor WHERE platform_user_id=?", (C,)).fetchone()["tier"], "A")

        # e nunca desce de volta ao virar alvo outra vez
        ingest.upsert_actor(conn, atproto.PLATFORM, C, "C", stats=stats)
        self.assertEqual(conn.execute(
            "SELECT tier FROM actor WHERE platform_user_id=?", (C,)).fetchone()["tier"], "A")
        conn.close()

    def test_authors_get_seed_tier_targets_do_not(self):
        self.assertEqual(self._actor(A)["tier"], "A")
        self.assertEqual(self._actor(B)["tier"], "A")
        self.assertEqual(self._actor(C)["tier"], "C")
        self.assertEqual(self._actor(D)["tier"], "C")

    def test_identity_event_fills_handle(self):
        self.assertEqual(self._actor(A)["handle"], "exemplo-a.test")
        self.assertIsNone(self._actor(C)["handle"])


class TestIngestRobustness(IngestTestCase):
    def test_irrelevant_and_malformed_events_are_counted_not_fatal(self):
        # a fonte descarta antes: o like e o evento sem did não chegam ao ingest
        self.assertEqual(self.stats.events_seen, 9)
        self.assertEqual(self.stats.events_ignored, 2)

    def test_duplicate_record_is_not_reinserted(self):
        self.assertEqual(self.stats.posts_dup, 1)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM post WHERE platform_post_id = ?",
            (f"{A}/app.bsky.feed.post/p001",)).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_rerunning_the_same_fixture_adds_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) AS n FROM post").fetchone()["n"]
        ingest.ingest(self.conn, FixtureSource(FIXTURE), author_tier="A", resume=False)
        after = self.conn.execute("SELECT COUNT(*) AS n FROM post").fetchone()["n"]
        self.assertEqual(before, after)

    def test_raw_payload_is_stored_compressed_and_readable(self):
        row = self.conn.execute(
            "SELECT payload_gz FROM raw_payload WHERE platform_post_id = ?",
            (f"{A}/app.bsky.feed.post/p003",)).fetchone()
        event = json.loads(gzip.decompress(row["payload_gz"]).decode("utf-8"))
        self.assertEqual(event["commit"]["rkey"], "p003")

    def test_cursor_advances_and_allows_resume(self):
        cursor = ingest.get_cursor(self.conn, "fixture")
        self.assertEqual(cursor, "1757000000000011")
        # retomando do cursor, nada novo é visto
        _, stats = ingest.ingest(self.conn, FixtureSource(FIXTURE))
        self.assertEqual(stats.events_seen, 0)


class TestDeletion(IngestTestCase):
    """Opção conservadora: conteúdo sai, aresta fica marcada."""

    def test_deleted_post_loses_text_and_raw_payload(self):
        row = self.conn.execute(
            "SELECT post_id, text, deleted_at, post_type FROM post "
            "WHERE platform_post_id = ?", (f"{B}/app.bsky.feed.post/p002",)).fetchone()
        self.assertIsNotNone(row["deleted_at"])
        self.assertIsNone(row["text"])
        self.assertEqual(row["post_type"], "reply")

        raw = self.conn.execute(
            "SELECT COUNT(*) AS n FROM raw_payload WHERE platform_post_id = ?",
            (f"{B}/app.bsky.feed.post/p002",)).fetchone()["n"]
        self.assertEqual(raw, 0)

    def test_edge_survives_deletion(self):
        self.assertIn((B, C, "reply"), self._edges())

    def test_active_view_excludes_deleted(self):
        total = self.conn.execute("SELECT COUNT(*) AS n FROM post").fetchone()["n"]
        ativos = self.conn.execute("SELECT COUNT(*) AS n FROM v_post_ativo").fetchone()["n"]
        self.assertEqual(total - ativos, 1)

    def test_delete_of_unknown_post_is_counted_not_fatal(self):
        stats = ingest.Stats()
        ingest.handle_event(self.conn, self.run_id, atproto.normalize({
            "did": A, "time_us": 9, "kind": "commit", "commit": {
                "operation": "delete", "collection": "app.bsky.feed.post",
                "rkey": "nunca-visto"}}), stats)
        self.assertEqual(stats.deletes_unknown, 1)


class TestRunAccounting(IngestTestCase):
    def test_run_is_closed_with_counts_and_zero_cost(self):
        row = self.conn.execute(
            "SELECT * FROM collection_run WHERE run_id = ?", (self.run_id,)).fetchone()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["kind"], "baseline")
        self.assertEqual(row["source"], "fixture")
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["cost_usd"], 0.0)
        self.assertEqual(row["items_fetched"], self.stats.posts_new)

    def test_max_events_stops_early(self):
        conn = db.connect(Path(self._tmp.name) / "t2.db")
        db.migrate(conn, ROOT / "migrations")
        _, stats = ingest.ingest(conn, FixtureSource(FIXTURE), max_events=3)
        self.assertEqual(stats.events_seen, 3)
        conn.close()


class TestJetstreamUrl(unittest.TestCase):
    """A URL é construída sem rede — o que dá para testar aqui, testamos."""

    def test_url_has_collections_dids_and_cursor(self):
        from nabote.sources import JetstreamSource
        src = JetstreamSource(wanted_dids=[A, B])
        url = src.url(cursor="1757000000000011")
        self.assertIn("wantedCollections=app.bsky.feed.post", url)
        self.assertIn("wantedCollections=app.bsky.feed.repost", url)
        self.assertIn("cursor=1757000000000011", url)
        self.assertEqual(url.count("wantedDids="), 2)
        self.assertTrue(url.startswith("wss://"))

    def test_did_limit_is_enforced_before_connecting(self):
        from nabote.sources import JetstreamSource
        with self.assertRaises(ValueError):
            JetstreamSource(wanted_dids=[f"did:plc:{i}" for i in range(10_001)])


class _FonteFalsa:
    """Fonte mínima, para exercitar o laço sem rede nem fixture.

    `frame` é atributo mutável de propósito: é assim que uma fonte real vai
    anotar, enquanto pagina, quais contas cobriu e quais o teto deixou de fora.
    """

    def __init__(self, name, eventos, frame=None, explode=False):
        self.name = name
        self._eventos = eventos
        self._explode = explode
        if frame is not None:
            self.frame = frame

    def events(self, cursor=None):
        for ev in self._eventos:
            yield ev
        if self._explode:
            raise RuntimeError("a fonte caiu no meio")


def _post(uid, pid, cursor=None, conta=""):
    return NormalizedEvent(
        platform="x", kind="post", actor_uid=uid, actor_handle=uid,
        occurred_at="2026-09-15T10:00:00Z", post_uid=pid, post_type="original",
        text="oi", cursor=cursor, cursor_account=conta)


class CursorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "c.db")
        db.migrate(self.conn, ROOT / "migrations")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestCursorPorConta(CursorTestCase):
    def test_duas_contas_avancam_cursores_independentes(self):
        """Com chave só em `source`, a segunda conta sobrescreveria a primeira
        em silêncio — e a retomada recoletaria o que já tinha sido pago."""
        ingest.ingest(self.conn, _FonteFalsa("x_api", [
            _post("u1", "p1", cursor="pag-a1", conta="u1"),
            _post("u2", "p2", cursor="pag-b1", conta="u2"),
            _post("u1", "p3", cursor="pag-a2", conta="u1"),
        ]), author_tier="A")

        self.assertEqual(ingest.get_cursor(self.conn, "x_api", "u1"), "pag-a2")
        self.assertEqual(ingest.get_cursor(self.conn, "x_api", "u2"), "pag-b1")

    def test_fonte_de_um_cursor_so_grava_na_conta_em_branco(self):
        ingest.ingest(self.conn, _FonteFalsa("firehose", [
            _post("u1", "p1", cursor="1757000000000011")]), author_tier="A")

        self.assertEqual(ingest.get_cursor(self.conn, "firehose"), "1757000000000011")
        self.assertEqual(self.conn.execute(
            "SELECT account FROM source_state WHERE source = 'firehose'"
        ).fetchone()["account"], "")

    def test_conta_sem_cursor_e_conta_nunca_coletada(self):
        """`cursors_for` omite quem não tem posição: quem chama precisa ler
        ausência como 'nunca coletada', não como 'está no começo'."""
        ingest.set_cursor(self.conn, "x_api", "pag-a1", "u1")
        self.conn.execute(
            "INSERT INTO source_state (source, account, cursor, updated_at) "
            "VALUES ('x_api','u2',NULL,?)", (db.utcnow(),))

        self.assertEqual(ingest.cursors_for(self.conn, "x_api"), {"u1": "pag-a1"})

    def test_cursores_de_outra_fonte_nao_vazam(self):
        ingest.set_cursor(self.conn, "x_api", "pag-a1", "u1")
        ingest.set_cursor(self.conn, "outra", "pag-z9", "u1")

        self.assertEqual(ingest.cursors_for(self.conn, "x_api"), {"u1": "pag-a1"})
        self.assertEqual(ingest.cursors_for(self.conn, "outra"), {"u1": "pag-z9"})


class TestRecorteDoRun(CursorTestCase):
    def _frame(self, run_id):
        return ingest.run_frame(self.conn, run_id)

    def test_recorte_da_fonte_fica_gravado(self):
        run_id, _ = ingest.ingest(self.conn, _FonteFalsa(
            "x_api", [_post("u1", "p1")],
            frame={"kind": "accounts", "planned": 3}), author_tier="A")

        self.assertEqual(self._frame(run_id), {"kind": "accounts", "planned": 3})

    def test_o_que_a_fonte_alcancou_vence_o_que_ela_planejou(self):
        """A fonte mexe no próprio recorte enquanto pagina; o valor gravado no
        fim é o que responde 'esta janela cobriu quantas das sementes?'."""
        recorte = {"kind": "accounts", "planned": 3}

        class Cobre(_FonteFalsa):
            def events(self, cursor=None):
                yield _post("u1", "p1")
                self.frame["reached"] = 2
                self.frame["left_out"] = ["u3"]

        run_id, _ = ingest.ingest(
            self.conn, Cobre("x_api", [], frame=recorte), author_tier="A")

        self.assertEqual(self._frame(run_id), {
            "kind": "accounts", "planned": 3, "reached": 2, "left_out": ["u3"]})

    def test_recorte_explicito_vence_o_da_fonte(self):
        run_id, _ = ingest.ingest(
            self.conn, _FonteFalsa("x_api", [_post("u1", "p1")],
                                   frame={"kind": "accounts"}),
            author_tier="A", frame={"kind": "search", "term": "CPMI"})

        self.assertEqual(self._frame(run_id), {"kind": "search", "term": "CPMI"})

    def test_run_que_cai_no_meio_mantem_o_recorte(self):
        """Status 'failed' sem recorte é um run que ninguém consegue explicar
        no dia seguinte: o plano é justamente o que diz o que ele tentava."""
        fonte = _FonteFalsa("x_api", [_post("u1", "p1")],
                            frame={"kind": "accounts", "planned": 3}, explode=True)
        with self.assertRaises(RuntimeError):
            ingest.ingest(self.conn, fonte, author_tier="A")

        row = self.conn.execute(
            "SELECT run_id, status FROM collection_run ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self._frame(row["run_id"]), {"kind": "accounts", "planned": 3})

    def test_fechar_sem_recorte_preserva_o_da_abertura(self):
        run_id = ingest.start_run(self.conn, "x_api", frame={"kind": "accounts"})
        ingest.finish_run(self.conn, run_id, ingest.Stats())

        self.assertEqual(self._frame(run_id), {"kind": "accounts"})

    def test_fonte_sem_recorte_nao_inventa_um(self):
        run_id, _ = ingest.ingest(
            self.conn, _FonteFalsa("firehose", [_post("u1", "p1")]), author_tier="A")

        self.assertIsNone(self._frame(run_id))


if __name__ == "__main__":
    unittest.main()


class TestPerfilQueChegaPelaAresta(unittest.TestCase):
    """Fontes de API trazem o perfil do alvo embutido no post de quem o citou.
    Guardar isso é o que faz um ator Tier C aparecer no relatório com nome em
    vez de id numérico, sem nenhuma requisição a mais.

    A regra difícil é a segunda vez. A mesma conta reaparece como menção, que
    traz `name` e não traz bio. Se a segunda passagem escrevesse o que veio,
    apagaria a bio que a primeira pagou para trazer.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "i.db")
        db.migrate(self.conn, ROOT / "migrations")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _linha(self, uid):
        return self.conn.execute(
            "SELECT handle, display_name, bio, account_created_at FROM actor "
            "WHERE platform_user_id = ?", (uid,)).fetchone()

    def test_grava_nome_e_bio_do_ator(self):
        ingest.upsert_actor(self.conn, "x", "900", "C", "fulano",
                            display_name="Fulano da Silva", bio="deputado",
                            account_created_at="2009-05-13T22:49:07Z")
        r = self._linha("900")
        self.assertEqual((r["display_name"], r["bio"]), ("Fulano da Silva", "deputado"))
        self.assertEqual(r["account_created_at"], "2009-05-13T22:49:07Z")

    def test_o_que_nao_veio_nao_apaga_o_que_ja_havia(self):
        ingest.upsert_actor(self.conn, "x", "900", "C", "fulano",
                            display_name="Fulano da Silva", bio="deputado")
        # segunda passagem, vinda de uma menção: tem nome, não tem bio
        ingest.upsert_actor(self.conn, "x", "900", "C", "fulano",
                            display_name="Fulano da Silva")
        self.assertEqual(self._linha("900")["bio"], "deputado")

    def test_valor_novo_substitui_o_antigo(self):
        """Bio muda, e a última vista é a boa — o cuidado é com ausência, não
        com mudança."""
        ingest.upsert_actor(self.conn, "x", "900", "C", "fulano", bio="deputado")
        ingest.upsert_actor(self.conn, "x", "900", "C", "fulano", bio="senador")
        self.assertEqual(self._linha("900")["bio"], "senador")

    def test_o_alvo_da_aresta_nasce_com_perfil(self):
        """O caminho inteiro: evento com alvo → ator Tier C legível."""
        from nabote.events import Target
        ev = NormalizedEvent(
            platform="x", kind="post", actor_uid="100", actor_handle="quem_amplifica",
            occurred_at="2026-09-20T20:20:42Z", post_uid="p1", post_type="repost",
            targets=[Target(kind="repost", uid="200", handle="quem_e_amplificado",
                            display_name="Quem É Amplificado", bio="a bio dele")])
        run_id = ingest.start_run(self.conn, "teste")
        ingest.handle_event(self.conn, run_id, ev, ingest.Stats())
        r = self._linha("200")
        self.assertEqual((r["display_name"], r["bio"]),
                         ("Quem É Amplificado", "a bio dele"))
