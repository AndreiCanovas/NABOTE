"""Recusa o commit quando um segredo entra pelo diff.

O `.gitignore` protege o arquivo `.env`. Não protege o caso que de fato vaza
chave em projeto pequeno: a pessoa cola a chave em OUTRO lugar — um script de
teste, um comando de exemplo no README, um arquivo temporário que depois vira
commit — e o `.gitignore` não tem nada a dizer sobre isso.

Esta guarda olha o que está PRESTES a ser commitado, não a árvore de trabalho.

DUAS REGRAS, e a segunda é onde mora a dificuldade:

1. O arquivo proibido chegando por `git add -f`. Simples e sem ambiguidade.

2. Uma atribuição de segredo com valor concreto. Difícil porque o repositório
   está cheio de menções legítimas a chave — `.env.example`, o runbook, os
   comandos de exemplo. A regra tem que separar "aqui vai a chave" de "a chave
   é esta". A separação é o LADO DIREITO: `$VAR`, `%s`, `<placeholder>` e
   palavras de exemplo são referência; uma string opaca de 16+ caracteres é
   conteúdo.

Guarda que grita à toa é guarda que alguém desliga na terceira vez. Por isso
ela é conferida contra o histórico inteiro do repositório: se acusasse um
commit legítimo que já existe, estaria calibrada errada.
"""

from __future__ import annotations

import re
import subprocess
import sys

# .env, .env.local, .env.producao — menos o exemplo, que é versionado de propósito
ARQUIVO_PROIBIDO = re.compile(r"(^|/)\.env(\.|$)(?!example)")

_NOME = r"(?:api[_-]?key|apikey|secret|token|password|passwd|bearer|x-api-key)"
# aspas e espaços opcionais dos dois lados do separador
ATRIBUICAO = re.compile(
    rf"""{_NOME}["']?\s*[:=]\s*["']?(?P<valor>[^\s"',;)}}\]]+)""",
    re.IGNORECASE,
)

# O lado direito que NÃO é segredo. Tudo aqui é referência à chave, não a chave:
#   $VAR ${VAR}      expansão de shell
#   %s {} {{}}       placeholder de formatação
#   <algo> [algo]    marcador de documentação
#   os.environ[...]  leitura de ambiente em Python
REFERENCIA = re.compile(
    r"""^(\$|%|\{|<|\[|["']?\s*$|os\.environ|process\.env|YOUR_|SEU_|SUA_)""",
    re.IGNORECASE,
)

# Palavras que a pessoa escreve onde a chave vai. Precisam passar porque este
# repositório as usa no runbook e no .env.example.
PLACEHOLDERS = {
    "cole_a_chave_aqui", "cole-a-chave-aqui", "sua_chave_aqui", "your_key_here",
    "coloque_sua_chave", "changeme", "xxx", "xxxx", "todo", "none", "null",
    "example", "exemplo", "placeholder", "redacted", "chave_falsa_de_teste",
}

TAMANHO_MINIMO = 16

# Escape por linha, para o caso legítimo: um teste que precisa de uma string
# com cara de chave, ou documentação que mostra um exemplo. Por LINHA e não por
# arquivo de propósito — isentar `tests/test_guarda_segredo.py` inteiro faria a
# guarda ignorar uma chave de verdade colada ali por acidente. A marca obriga
# quem escreve a dizer, naquela linha, que é intencional.
PERMITIDO = re.compile(r"guarda:\s*permitido", re.IGNORECASE)


def valor_e_segredo(valor: str) -> bool:
    """O lado direito de uma atribuição carrega conteúdo, e não referência?"""
    if REFERENCIA.match(valor):
        return False
    if valor.strip("\"'").lower() in PLACEHOLDERS:
        return False
    nu = valor.strip("\"'")
    if len(nu) < TAMANHO_MINIMO:
        return False
    # string opaca é a assinatura de uma chave: sem espaço, sem barra de
    # caminho, e misturando classes de caractere. "caminho/para/arquivo.txt"
    # e "uma frase qualquer" têm 16+ caracteres e não são segredo.
    if "/" in nu or "\\" in nu:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_\-.+=]{%d,}" % TAMANHO_MINIMO, nu))


def mascara(valor: str) -> str:
    """Nunca reimprimir o segredo inteiro: a mensagem de erro vai para o
    terminal, o histórico do shell e possivelmente um log de CI."""
    nu = valor.strip("\"'")
    return f"{nu[:4]}…{nu[-2:]} ({len(nu)} caracteres)"


def _git(*args: str) -> str:
    return subprocess.run(("git",) + args, capture_output=True, text=True,
                          check=True).stdout


def arquivos_staged() -> list[str]:
    saida = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return [linha for linha in saida.splitlines() if linha]


def achados_no_diff(diff: str) -> list[tuple[str, int, str]]:
    """(arquivo, linha, valor mascarado) para cada segredo em linha ADICIONADA.

    Só linhas adicionadas: conteúdo que já estava no repositório não é
    responsabilidade deste commit, e reclamar dele travaria qualquer edição
    num arquivo que por acaso mencione a palavra "token".
    """
    achados: list[tuple[str, int, str]] = []
    arquivo, numero = "?", 0
    for linha in diff.splitlines():
        if linha.startswith("+++ b/"):
            arquivo, numero = linha[6:], 0
            continue
        if linha.startswith("@@"):
            m = re.search(r"\+(\d+)", linha)
            numero = int(m.group(1)) - 1 if m else 0
            continue
        if not linha.startswith("+") or linha.startswith("+++"):
            continue
        numero += 1
        if PERMITIDO.search(linha):
            continue
        for m in ATRIBUICAO.finditer(linha[1:]):
            if valor_e_segredo(m.group("valor")):
                achados.append((arquivo, numero, mascara(m.group("valor"))))
    return achados


def main() -> int:
    proibidos = [a for a in arquivos_staged() if ARQUIVO_PROIBIDO.search(a)]
    achados = achados_no_diff(_git("diff", "--cached", "-U0"))

    if not proibidos and not achados:
        return 0

    print("\n  COMMIT RECUSADO — parece haver segredo no que vai ser commitado\n",
          file=sys.stderr)
    for a in proibidos:
        print(f"    {a}  — arquivo de ambiente, nunca versionado", file=sys.stderr)
    for arquivo, linha, valor in achados:
        print(f"    {arquivo}:{linha}  — valor {valor}", file=sys.stderr)

    print("\n  Se for chave de verdade: tire do commit, e se ela já saiu desta"
          "\n  máquina, revogue no painel do provedor antes de qualquer coisa."
          "\n\n  Se for falso positivo: `git commit --no-verify`, e me avise para"
          "\n  eu calibrar a guarda.\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
