"""Testes da descoberta de tema no texto.

O tema sai de: termo -> coocorrência -> grupo -> confirmação humana. O ponto
delicado NÃO é o agrupamento, é o que acontece na SEGUNDA janela: tema
confirmado tem que continuar sendo o mesmo tema, e só o que não casou com ele
pode virar proposta nova. Sem isso, "CPMI subiu 40%" compara o grupo 3 desta
semana com o grupo 7 da anterior.

Rodar:  python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nabote import db, ingest, temas  # noqa: E402
from nabote.events import NormalizedEvent  # noqa: E402

CPMI = [
    "A CPMI do INSS precisa ouvir os aposentados lesados pelos descontos",
    "Descontos no beneficio do aposentado é roubo. CPMI tem que apurar",
    "Relatorio da CPMI do INSS aponta desvio nos descontos associativos",
    "Aposentado nao paga a conta. CPMI do INSS tem que indiciar",
    "O INSS descontou de quem trabalhou a vida toda. CPMI ja",
]
TARIFA = [
    "Tarifaço do Trump vai quebrar o agronegocio brasileiro",
    "Trump anuncia sobretaxa e o governo nao responde ao tarifaço",
    "O tarifaço americano atinge o café e a carne. Exportacao parada",
    "Trump usa o tarifaço como chantagem. Agronegocio pressiona",
]


class TestExtracaoDeTermos(unittest.TestCase):
    def test_tira_acento_para_a_mesma_palavra_nao_virar_duas(self):
        self.assertIn("tarifaco", temas.extrair_termos("O tarifaço subiu"))

    def test_descarta_palavra_vazia_de_conteudo(self):
        t = temas.extrair_termos("para que de sobre com uma")
        self.assertEqual(t, set())

    def test_guarda_hashtag_porque_hashtag_e_pauta(self):
        self.assertIn("#foratodos", temas.extrair_termos("protesto #ForaTodos hoje"))

    def test_guarda_ano_de_quatro_digitos(self):
        """'2026' é pauta; '50' é ruído."""
        t = temas.extrair_termos("eleicao de 2026 com 50 candidatos")
        self.assertIn("2026", t)
        self.assertNotIn("50", t)


class TestAgrupamento(unittest.TestCase):
    def test_separa_assuntos_que_nao_se_misturam(self):
        por_post = [temas.extrair_termos(t) for t in CPMI + TARIFA]
        grupos = temas.agrupar_termos(por_post)
        achatado = [set(g) for g in grupos]
        cpmi = next(g for g in achatado if "cpmi" in g)
        self.assertIn("inss", cpmi)
        self.assertNotIn("trump", cpmi)

    def test_termo_que_aparece_uma_vez_so_nao_agrupa(self):
        """Sem repetição não há coocorrência, e um termo solto viraria um
        'tema' de um post só."""
        por_post = [temas.extrair_termos(t) for t in CPMI +
                    ["uma frase completamente isolada sobre jabuticaba"]]
        grupos = temas.agrupar_termos(por_post)
        self.assertNotIn("jabuticaba", {t for g in grupos for t in g})


class TestCicloDeCuradoria(unittest.TestCase):
    """O ciclo inteiro, que é onde mora o valor."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "t.db")
        db.migrate(self.conn, ROOT / "migrations")
        self.run = ingest.start_run(self.conn, "twitterapi_io")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _postar(self, textos, quando="2026-09-21T12:00:00Z", base=0):
        for i, texto in enumerate(textos):
            ingest.handle_event(self.conn, self.run, NormalizedEvent(
                platform="x", kind="post", actor_uid=f"a{base + i}",
                actor_handle=f"conta{base + i}", occurred_at=quando,
                post_uid=f"p{base}_{i}", post_type="original", text=texto),
                ingest.Stats())

    def test_propoe_tema_com_os_termos_dele(self):
        self._postar(CPMI + TARIFA)
        propostos = temas.propor_temas(self.conn, "2026-09-21")
        self.assertGreaterEqual(len(propostos), 2)
        um = next(t for t in propostos if "cpmi" in t["termos"])
        self.assertIn("inss", um["termos"])
        self.assertEqual(um["status"], "proposto")

    def test_confirmar_da_nome_de_gente_e_mantem_o_id(self):
        self._postar(CPMI + TARIFA)
        propostos = temas.propor_temas(self.conn, "2026-09-21")
        alvo = next(t for t in propostos if "cpmi" in t["termos"])
        temas.confirmar_tema(self.conn, alvo["topic_id"], "CPMI do INSS")
        guardado, = [t for t in temas.listar_temas(self.conn)
                     if t["topic_id"] == alvo["topic_id"]]
        self.assertEqual(guardado["label"], "CPMI do INSS")
        self.assertEqual(guardado["status"], "confirmado")

    def test_a_segunda_janela_casa_com_o_tema_ja_confirmado(self):
        """O ponto do desenho todo: o mesmo assunto, na semana seguinte, cai no
        MESMO topic_id — senão não há o que comparar entre semanas."""
        self._postar(CPMI + TARIFA)
        propostos = temas.propor_temas(self.conn, "2026-09-21")
        alvo = next(t for t in propostos if "cpmi" in t["termos"])
        temas.confirmar_tema(self.conn, alvo["topic_id"], "CPMI do INSS")

        self._postar(["Nova sessao da CPMI do INSS ouve mais aposentados hoje"],
                     quando="2026-09-28T12:00:00Z", base=100)
        temas.propor_temas(self.conn, "2026-09-28")
        casados = self.conn.execute(
            "SELECT pt.topic_id FROM post_topic pt JOIN post p USING (post_id) "
            "WHERE p.platform_post_id = 'p100_0'").fetchall()
        self.assertEqual([r["topic_id"] for r in casados], [alvo["topic_id"]])

    def test_o_que_casou_com_confirmado_nao_vira_proposta_nova(self):
        self._postar(CPMI + TARIFA)
        propostos = temas.propor_temas(self.conn, "2026-09-21")
        alvo = next(t for t in propostos if "cpmi" in t["termos"])
        temas.confirmar_tema(self.conn, alvo["topic_id"], "CPMI do INSS")

        self._postar(CPMI, quando="2026-09-28T12:00:00Z", base=200)
        novos = temas.propor_temas(self.conn, "2026-09-28")
        self.assertEqual([t for t in novos if "cpmi" in t["termos"]], [])

    def test_tema_descartado_nao_volta_a_ser_proposto(self):
        """Recusar uma vez tem que valer para as próximas janelas, senão a
        curadoria vira trabalho de Sísifo.

        A asserção é sobre o NÚMERO DE TEMAS no banco, e não sobre o rótulo da
        proposta: a primeira versão deste teste olhava o rótulo e passava por
        causa da deduplicação de texto, com o descarte inteiro desligado.
        """
        self._postar(CPMI + TARIFA)
        propostos = temas.propor_temas(self.conn, "2026-09-21")
        alvo = next(t for t in propostos if "trump" in t["termos"])
        temas.descartar_tema(self.conn, alvo["topic_id"])
        antes = self.conn.execute("SELECT COUNT(*) n FROM topic").fetchone()["n"]

        # os mesmos posts de tarifa, escritos com outras palavras: o rótulo do
        # reagrupamento sairia diferente, então só o descarte pode barrá-los
        self._postar(["Sobretaxa de Trump ao agronegocio derruba exportacao",
                      "Trump e o tarifaço: agronegocio sem exportacao para os EUA",
                      "Exportacao do agronegocio parada pelo tarifaço de Trump"],
                     quando="2026-09-28T12:00:00Z", base=300)
        temas.propor_temas(self.conn, "2026-09-28")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM topic").fetchone()["n"],
            antes, "o descarte não barrou: tema novo foi criado para o mesmo assunto")

    def test_um_termo_em_comum_nao_basta_para_casar(self):
        """'governo' sozinho casaria com metade da política brasileira. A regra
        dos dois termos é o que impede o tema de virar um saco."""
        self._postar(CPMI + TARIFA)
        propostos = temas.propor_temas(self.conn, "2026-09-21")
        alvo = next(t for t in propostos if "cpmi" in t["termos"])
        temas.confirmar_tema(self.conn, alvo["topic_id"], "CPMI do INSS")

        # um único termo do tema ("aposentado"), num post que é sobre outra coisa
        self._postar(["O aposentado brasileiro enfrenta fila no posto de saude"],
                     quando="2026-09-28T12:00:00Z", base=500)
        temas.propor_temas(self.conn, "2026-09-28")
        casados = self.conn.execute(
            "SELECT pt.topic_id FROM post_topic pt JOIN post p USING (post_id) "
            "WHERE p.platform_post_id = 'p500_0' AND pt.topic_id = ?",
            (alvo["topic_id"],)).fetchall()
        self.assertEqual(casados, [], "casou com um termo só")

    def test_assunto_novo_aparece_como_proposta(self):
        """O buraco do léxico curado: pauta nova precisa aparecer sozinha."""
        self._postar(CPMI)
        p1 = temas.propor_temas(self.conn, "2026-09-21")
        temas.confirmar_tema(self.conn, p1[0]["topic_id"], "CPMI do INSS")

        self._postar(TARIFA, quando="2026-09-28T12:00:00Z", base=400)
        novos = temas.propor_temas(self.conn, "2026-09-28")
        self.assertTrue(any("tarifaco" in t["termos"] for t in novos),
                        f"pauta nova não apareceu: {[t['termos'] for t in novos]}")

    def test_repropor_a_mesma_janela_nao_duplica(self):
        self._postar(CPMI + TARIFA)
        primeiro = temas.propor_temas(self.conn, "2026-09-21")
        segundo = temas.propor_temas(self.conn, "2026-09-21")
        self.assertEqual(len(primeiro), len(segundo))
        n = self.conn.execute("SELECT COUNT(*) n FROM topic").fetchone()["n"]
        self.assertEqual(n, len(primeiro))

    def test_os_termos_sao_json_valido(self):
        self._postar(CPMI + TARIFA)
        temas.propor_temas(self.conn, "2026-09-21")
        for r in self.conn.execute("SELECT terms FROM topic"):
            self.assertIsInstance(json.loads(r["terms"]), list)
