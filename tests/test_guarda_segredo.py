"""Testes da guarda de segredo do pre-commit.

Uma guarda tem dois jeitos de falhar, e os dois são caros. Se ela não pega, dá
falsa confiança — pior do que não existir, porque a pessoa para de conferir. Se
ela grita à toa, alguém a desliga na terceira vez e nunca mais liga.

Por isso os dois lados são testados com o mesmo peso, e o lado dos falsos
positivos usa linhas REAIS deste repositório.

Rodar:  python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "guarda_segredo", ROOT / "tools" / "guarda_segredo.py")
guarda = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guarda)


def diff_de(linha: str, arquivo: str = "algum_script.py") -> str:
    """Envelope mínimo de diff com uma linha adicionada."""
    return f"+++ b/{arquivo}\n@@ -0,0 +1 @@\n+{linha}\n"


class TestPegaSegredo(unittest.TestCase):
    """Se algum destes passar batido, a guarda não serve para nada."""

    CASOS = [
        ("chave hexadecimal", 'NABOTE_X_API_KEY=a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6'),  # guarda:permitido
        ("cabeçalho literal", 'curl -H "x-api-key: 7f3a9c2e8b1d4f6a0c5e2b8d9a1f3c7e"'),  # guarda:permitido
        ("com aspas e espaços", 'API_KEY = "sk_live_abcdefghij1234567890"'),  # guarda:permitido
        ("token tipo JWT", 'token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'),  # guarda:permitido
        ("senha", 'password=SenhaSuperSecreta12345'),  # guarda:permitido
        ("em dicionário python", '    "apikey": "Kx9mP2nQ7rT4vW8yZ1aB3cD5eF6gH0jL",'),  # guarda:permitido
        ("maiúsculas", 'SECRET=ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'),  # guarda:permitido
    ]

    def test_pega_cada_forma_de_segredo(self):
        for nome, linha in self.CASOS:
            with self.subTest(nome):
                achados = guarda.achados_no_diff(diff_de(linha))
                self.assertTrue(achados, f"passou batido: {nome}")

    def test_arquivo_de_ambiente_e_proibido(self):
        for caminho in (".env", ".env.local", ".env.producao", "sub/.env"):
            with self.subTest(caminho):
                self.assertTrue(guarda.ARQUIVO_PROIBIDO.search(caminho))


class TestNaoGritaAToa(unittest.TestCase):
    """Todas estas linhas existem neste repositório, versionadas."""

    CASOS = [
        ("valor vazio do .env.example", '# NABOTE_X_API_KEY='),
        ("placeholder do runbook", 'NABOTE_X_API_KEY=cole_a_chave_aqui'),  # guarda:permitido
        ("variável de shell", 'curl -H "x-api-key: $NABOTE_X_API_KEY" "$B/twitter/user/info"'),
        ("placeholder de printf", "printf 'NABOTE_X_API_KEY=%s\\n' \"$K\""),
        ("leitura de ambiente", 'self.key = os.environ["TWITTERAPI_IO_KEY"]'),
        ("atributo curto", '        self.s.headers.update({"x-api-key": self.key})'),
        ("comprimento, não conteúdo", 'echo "chave: ${#NABOTE_X_API_KEY} caracteres"'),
        ("prosa sobre chave", 'A chave sai do .env e nunca aparece na tela nem no histórico'),
        ("caminho de arquivo", 'token = caminho/para/um/arquivo_de_token.txt'),  # guarda:permitido
        ("nome de variável em texto", 'O cabeçalho é x-api-key: <sua-chave-aqui>'),
    ]

    def test_nao_acusa_nenhuma(self):
        for nome, linha in self.CASOS:
            with self.subTest(nome):
                achados = guarda.achados_no_diff(diff_de(linha))
                self.assertFalse(achados, f"falso positivo em {nome}: {achados}")

    def test_o_exemplo_versionado_nao_e_proibido(self):
        self.assertIsNone(guarda.ARQUIVO_PROIBIDO.search(".env.example"))

    def test_linha_removida_nao_conta(self):
        """Tirar um segredo do repositório não pode ser bloqueado pela guarda
        que existe justamente para tirá-lo de lá."""
        diff = ("+++ b/x.py\n@@ -1 +0,0 @@\n"
                "-API_KEY=a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6\n")  # guarda:permitido
        self.assertEqual(guarda.achados_no_diff(diff), [])


class TestMarcaDePermissao(unittest.TestCase):
    """A marca existe porque a guarda recusou este próprio arquivo de teste —
    que é o comportamento correto dela e um impasse para quem o escreve."""

    def test_a_marca_isenta_a_linha(self):
        linha = 'API_KEY=a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6  # guarda:permitido'
        self.assertEqual(guarda.achados_no_diff(diff_de(linha)), [])

    def test_a_marca_isenta_so_a_linha_dela(self):
        """Isenção que vaza para a linha seguinte seria pior que não existir."""
        diff = ("+++ b/x.py\n@@ -0,0 +1,2 @@\n"
                "+TOKEN=a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6  # guarda:permitido\n"
                "+SECRET=z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4\n")
        (arquivo, numero, _), = guarda.achados_no_diff(diff)
        self.assertEqual(numero, 2)


class TestMensagem(unittest.TestCase):
    def test_o_valor_sai_mascarado(self):
        """A mensagem vai para o terminal, o histórico do shell e talvez um log
        de CI. Reimprimir o segredo inteiro o espalharia mais."""
        segredo = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  # guarda:permitido
        saida = guarda.mascara(segredo)
        self.assertNotIn(segredo, saida)
        self.assertIn("32 caracteres", saida)

    def test_aponta_arquivo_e_linha(self):
        diff = ("+++ b/tools/algo.py\n@@ -0,0 +12,3 @@\n"
                "+import os\n"
                "+\n"
                "+API_KEY=a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6\n")  # guarda:permitido
        (arquivo, linha, _), = guarda.achados_no_diff(diff)
        self.assertEqual(arquivo, "tools/algo.py")
        self.assertEqual(linha, 14)


if __name__ == "__main__":
    unittest.main()
