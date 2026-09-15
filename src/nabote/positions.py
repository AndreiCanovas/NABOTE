"""Eixo de posicionamento: análise de correspondência sobre a matriz de amplificação.

A pergunta que isto responde é "quem está perto de quem", e a resposta vem da
ESTRUTURA, não do texto: dois atores ficam próximos quando amplificam as mesmas
contas, mesmo que nunca tenham escrito uma palavra parecida. É a mesma família
de método que a literatura chama de ideologia latente.

Três decisões que valem registro, porque cada uma é uma armadilha:

1. NÃO USAMOS numpy. O projeto tem duas dependências e nenhuma delas é numpy;
   acrescentar uma biblioteca inteira para extrair um vetor singular de uma
   matriz esparsa não se paga. A primeira dimensão sai por iteração de potência
   com deflação, que é O(arestas) por passo e cabe em 60 linhas.

2. QUEM AMPLIFICOU UMA CONTA SÓ NÃO TEM POSIÇÃO. Um ator com um único alvo cai
   exatamente em cima dele — a coordenada existe e não significa nada. É o mesmo
   raciocínio do E-I "com escolha": sem escolha não há posição. Por isso o filtro
   roda até o ponto fixo, como a poda do núcleo.

3. O SINAL DO EIXO É CONVENÇÃO. A decomposição devolve o eixo, não a orientação;
   trocar o sinal dá a mesma solução. Fixamos pela comunidade #0 ficar do lado
   negativo, para que duas execuções da mesma janela não saiam espelhadas. Isso
   torna o sinal comparável DENTRO de uma campanha e não fora dela — é por isso
   que `method` vai gravado junto com o escore.
"""

from __future__ import annotations

import math
import random
import sqlite3
from typing import Any, Iterable

from .db import utcnow
from .graph import GRAPH_VERSION, LEIDEN_SEED, VIEWS, edge_scope_of

METHOD = "ca-amp-v1"
AXIS = "amp"

# Forma única do diagnóstico. Definida num lugar só porque os três caminhos de
# saída precisam das MESMAS chaves: quem consome não pode ter que descobrir
# quais existem em qual caso.
DIAG_VAZIO = {"convergiu": False, "inercia": 0.0, "inercia_total": 0.0,
              "fatia_inercia": 0.0, "iteracoes": 0, "residuo": None}

# Abaixo disto o ator não escolheu: amplificou uma conta só.
MIN_ALVOS = 2
# Alvo amplificado por uma pessoa só não separa ninguém de ninguém.
MIN_AMPLIFICADORES = 2
MAX_ITER = 400
# Tolerância em norma do MÁXIMO, não da soma. Com soma de valores absolutos
# sobre 1.490 componentes, 1e-10 exige ~7e-14 por componente — abaixo do ruído
# do float64 para entradas da ordem de 1/√1490. O critério era inatingível por
# construção e a iteração sempre batia no teto, informando "300 iterações" tanto
# para quem convergiu quanto para quem desistiu.
TOL = 1e-9


def _matriz(conn: sqlite3.Connection, window_start: str, scope: str,
            view: str = "amp") -> dict[tuple[int, int], float]:
    """Arestas de amplificação da janela, já ponderadas pela visão."""
    pesos = VIEWS[view]
    marcas = ",".join("?" * len(pesos))
    bruto: dict[tuple[int, int], float] = {}
    for r in conn.execute(
        f"SELECT src_actor_id, dst_actor_id, kind, weight FROM edge_window "
        f"WHERE window_start = ? AND scope = ? AND kind IN ({marcas})",
        (window_start, scope, *pesos.keys()),
    ):
        chave = (r["src_actor_id"], r["dst_actor_id"])
        bruto[chave] = bruto.get(chave, 0.0) + r["weight"] * pesos[r["kind"]]
    return bruto


def _podar(matriz: dict[tuple[int, int], float]) -> dict[tuple[int, int], float]:
    """Remove linhas e colunas sem poder de separação, até estabilizar.

    Remover um alvo pode deixar um amplificador com um alvo só, e remover esse
    amplificador pode deixar outro alvo com um amplificador só. Uma passada não
    basta — é o mesmo ponto fixo da poda do núcleo.
    """
    atual = dict(matriz)
    while atual:
        alvos: dict[int, int] = {}
        fontes: dict[int, int] = {}
        for (i, j) in atual:
            fontes[i] = fontes.get(i, 0) + 1
            alvos[j] = alvos.get(j, 0) + 1
        sobra = {k: v for k, v in atual.items()
                 if fontes[k[0]] >= MIN_ALVOS and alvos[k[1]] >= MIN_AMPLIFICADORES}
        if len(sobra) == len(atual):
            return atual
        atual = sobra
    return atual


def _dimensao_1(matriz: dict[tuple[int, int], float],
                seed: int = LEIDEN_SEED) -> tuple[dict[int, float], dict[str, Any]]:
    """Primeira dimensão não trivial da análise de correspondência.

    Devolve (escore por linha, diagnóstico). O diagnóstico traz `convergiu`
    porque sem ele não há como distinguir uma solução estável de uma que bateu
    no teto de iterações — e as duas viram o mesmo número no relatório.

    A matriz normalizada A = Dr^-1/2 · P · Dc^-1/2 tem um par singular trivial
    conhecido de antemão — (√r, √c) com σ = 1 — que corresponde à independência
    entre linha e coluna e não carrega informação nenhuma. A dimensão que
    interessa é a SEGUNDA, e é por isso que cada passo da iteração projeta o
    vetor trivial fora: sem a deflação a iteração converge para o resultado que
    já sabíamos.
    """
    linhas = sorted({i for i, _ in matriz})
    colunas = sorted({j for _, j in matriz})
    li = {a: k for k, a in enumerate(linhas)}
    ci = {a: k for k, a in enumerate(colunas)}
    nl, nc = len(linhas), len(colunas)
    vazio = dict(DIAG_VAZIO)
    if nl < 2 or nc < 2:
        return {}, vazio

    total = sum(matriz.values())
    r = [0.0] * nl
    c = [0.0] * nc
    for (i, j), w in matriz.items():
        r[li[i]] += w / total
        c[ci[j]] += w / total

    # A_ij pré-calculado: a iteração toca cada aresta duas vezes por passo e
    # recomputar a raiz aqui dentro dobraria o custo.
    a_ij = [(li[i], ci[j], (w / total) / math.sqrt(r[li[i]] * c[ci[j]]))
            for (i, j), w in matriz.items()]
    raiz_c = [math.sqrt(x) for x in c]
    # Inércia total = χ²/N = Σ a_ij² − 1. Sai de graça do que já foi calculado,
    # e é o denominador que transforma σ₁² em "% da inércia" — sem ele o número
    # sozinho não diz se a dimensão 1 explica muito ou pouco.
    inercia_total = sum(a * a for _, _, a in a_ij) - 1.0

    def a_vec(v: list[float]) -> list[float]:
        out = [0.0] * nl
        for i, j, a in a_ij:
            out[i] += a * v[j]
        return out

    def at_vec(u: list[float]) -> list[float]:
        out = [0.0] * nc
        for i, j, a in a_ij:
            out[j] += a * u[i]
        return out

    def deflacionar(v: list[float]) -> None:
        proj = sum(v[k] * raiz_c[k] for k in range(nc))
        for k in range(nc):
            v[k] -= proj * raiz_c[k]

    rng = random.Random(seed)
    v = [rng.uniform(-1.0, 1.0) for _ in range(nc)]
    deflacionar(v)
    norma = math.sqrt(sum(x * x for x in v)) or 1.0
    v = [x / norma for x in v]

    sigma2 = 0.0
    gastas = 0
    residuo = float("inf")
    convergiu = False
    for gastas in range(1, MAX_ITER + 1):
        prox = at_vec(a_vec(v))
        deflacionar(prox)
        sigma2 = math.sqrt(sum(x * x for x in prox))
        if sigma2 <= 0:
            return {}, {**vazio, "iteracoes": gastas}
        prox = [x / sigma2 for x in prox]
        residuo = max(abs(prox[k] - v[k]) for k in range(nc))
        v = prox
        if residuo < TOL:
            convergiu = True
            break

    diag = {"convergiu": convergiu, "inercia": sigma2,
            "inercia_total": inercia_total,
            "fatia_inercia": (sigma2 / inercia_total) if inercia_total > 0 else 0.0,
            "iteracoes": gastas, "residuo": residuo}
    u = a_vec(v)
    norma_u = math.sqrt(sum(x * x for x in u))
    if norma_u <= 0:
        return {}, {**vazio, "iteracoes": gastas}
    sigma = math.sqrt(sigma2)
    # coordenada principal da linha: σ · u_i / √r_i
    escores = {linhas[k]: sigma * (u[k] / norma_u) / math.sqrt(r[k]) for k in range(nl)}
    return escores, diag


def _orientar(escores: dict[int, float], comunidade: dict[int, int]) -> dict[int, float]:
    """Fixa o sinal do eixo pela comunidade #0 ficar do lado negativo.

    A decomposição devolve um eixo, não uma orientação: -v é tão solução quanto
    v. Sem uma regra, a mesma janela sai espelhada entre execuções e a coluna
    "Δ posição" vira ruído puro.
    """
    membros = [escores[a] for a in escores if comunidade.get(a) == 0]
    if membros and sum(membros) > 0:
        return {a: -s for a, s in escores.items()}
    return escores


def _normalizar(escores: dict[int, float]) -> dict[int, float]:
    """Reescala para [-1, +1] pelos extremos, que viram as âncoras do eixo."""
    maior = max((abs(s) for s in escores.values()), default=0.0)
    if maior <= 0:
        return escores
    return {a: s / maior for a, s in escores.items()}


def compute_positions(
    conn: sqlite3.Connection, window_start: str, scope: str,
    view: str = "amp", graph_version: int = GRAPH_VERSION,
    edge_scope: str | None = None, persist: bool = True,
) -> dict[str, Any]:
    """Calcula e grava o eixo de posicionamento de uma janela e escopo.

    `scope` é o escopo ANALÍTICO (onde estão as comunidades, ex.
    `amp:topic:Yanomami:core`); `edge_scope` é onde estão as arestas
    (`topic:Yanomami`). Quando não informado, deriva-se do primeiro.
    """
    if edge_scope is None:
        edge_scope = edge_scope_of(scope)

    bruto = _matriz(conn, window_start, edge_scope, view)
    matriz = _podar(bruto)
    if not matriz:
        return {"scope": scope, "atores": 0, "alvos": 0,
                "descartados": len({i for i, _ in bruto}),
                "decis": [], "fatia_no_meio": 0.0, **DIAG_VAZIO}

    comunidade = {
        r["actor_id"]: r["community_id"] for r in conn.execute(
            "SELECT actor_id, community_id FROM actor_community "
            "WHERE window_start = ? AND scope = ? AND graph_version = ?",
            (window_start, scope, graph_version))
    }
    escores, diag = _dimensao_1(matriz)
    if not escores:
        return {"scope": scope, "atores": 0, "alvos": 0,
                "descartados": len({i for i, _ in bruto}),
                "decis": [], "fatia_no_meio": 0.0, **diag}

    escores = _normalizar(_orientar(escores, comunidade))
    extremos = sorted(escores, key=lambda a: escores[a])
    ancoras = {extremos[0], extremos[-1]}

    if persist:
        conn.execute(
            "DELETE FROM actor_position WHERE window_start = ? AND scope = ? AND axis = ?",
            (window_start, scope, AXIS))
        agora = utcnow()
        conn.executemany(
            "INSERT INTO actor_position (actor_id, axis, window_start, scope, score, "
            "method, is_anchor, computed_at) VALUES (?,?,?,?,?,?,?,?)",
            [(a, AXIS, window_start, scope, s, METHOD, 1 if a in ancoras else 0, agora)
             for a, s in escores.items()])

    # Distribuição: um eixo com σ₁ perto de 1 tende a virar indicador binário
    # do bloco em vez de um contínuo de posições. O relatório precisa saber em
    # qual dos dois casos está antes de chamar isto de "eixo".
    ordenados = sorted(escores.values())
    n = len(ordenados)
    decis = [ordenados[min(n - 1, (n * k) // 10)] for k in range(11)]
    meio = sum(1 for s in ordenados if abs(s) < 0.25)

    return {
        "scope": scope, "atores": len(escores),
        "alvos": len({j for _, j in matriz}),
        "descartados": len({i for i, _ in bruto}) - len(escores),
        "ancoras": sorted(ancoras),
        "decis": decis, "fatia_no_meio": meio / n if n else 0.0,
        **diag,
    }


def positions_of(conn: sqlite3.Connection, window_start: str, scope: str,
                 actor_ids: Iterable[int] | None = None) -> dict[int, float]:
    """Escores já gravados, para juntar a uma tabela de atores."""
    linhas = conn.execute(
        "SELECT actor_id, score FROM actor_position "
        "WHERE window_start = ? AND scope = ? AND axis = ?",
        (window_start, scope, AXIS)).fetchall()
    tudo = {r["actor_id"]: r["score"] for r in linhas}
    if actor_ids is None:
        return tudo
    alvo = set(actor_ids)
    return {a: s for a, s in tudo.items() if a in alvo}
