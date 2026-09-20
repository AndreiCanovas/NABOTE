"""Fonte: X via twitterapi.io.

Primeira fonte do projeto que custa dinheiro por requisição, e a primeira em
que "onde eu parei" é uma resposta por conta em vez de uma posição só.

TRÊS ARMADILHAS DO FORMATO, todas descobertas na resposta real de 20/09/2026 e
todas capazes de custar horas se descobertas depois:

1. `createdAt` TEM DOIS FORMATOS, no mesmo nome de campo. Em `/twitter/user/info`
   vem ISO com microssegundos — "2012-08-15T01:22:19.000000Z". Dentro do objeto
   `author` embutido num tweet, vem no formato legado do Twitter —
   "Wed Aug 15 01:22:19 +0000 2012". Um parser só que assuma qualquer um dos
   dois quebra na metade das chamadas.

2. `description` DO AUTOR EMBUTIDO VEM VAZIO. A bio de verdade está em
   `profile_bio.description`. Em `/twitter/user/info` é o contrário: `description`
   tem a bio e `profile_bio` não existe. Ler só o primeiro faz todo ator que
   nasce como alvo de aresta chegar sem bio, em silêncio.

3. A MENÇÃO JÁ ESTÁ CONTADA POR OUTRA ARESTA, e emiti-la de novo infla o grafo.
   `entities.user_mentions` traz `id_str` e `screen_name` — id estável, o que
   é bom. Mas num retuíte ela contém o autor retuitado, por causa do prefixo
   "RT @fulano:"; numa resposta, contém quem foi respondido. Emitir menção sem
   descontar já-cobertos conta a mesma relação duas vezes em `edge_window`, e
   o peso de amplificação sai inflado. Aqui só vira menção quem não é alvo de
   repost, resposta ou citação no mesmo post.

   (Esta entrada já esteve errada: com base em amostras onde `entities` vinha
   `{}`, o módulo afirmava que menção não era extraível com id. O campo vinha
   vazio porque aqueles tweets não mencionavam ninguém. Ausência de dado não
   é ausência de campo.)

Ganho em relação ao arquivo do Zenodo: o objeto `author` vem embutido em CADA
tweet, e em `retweeted_tweet`/`quoted_tweet` também. Ator Tier C nasce com id
estável, nome e bio — sem uma chamada a mais, sem custo a mais.

LIMITE DE TAXA: 1 requisição a cada 5 segundos no tier gratuito. O `SKILL.md`
do provedor anuncia ~200 QPS, que é conta paga. A diferença é de duas ordens de
grandeza e decide quanto tempo um ciclo leva.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterator

from ..events import (KIND_MENTION, KIND_QUOTE, KIND_REPLY, KIND_REPOST,
                      NormalizedEvent, Target)

PLATFORM = "x"
BASE = "https://api.twitterapi.io"

# 100.000 créditos = US$ 1,00, na tabela do provedor.
CREDITOS_POR_USD = 100_000

# Tier gratuito: uma requisição a cada 5 segundos. A folga de 0,2 s existe
# porque o servidor mede o intervalo do lado dele, e um relógio adiantado
# transforma o limite num 429 que custa uma requisição inteira.
INTERVALO_PADRAO = 5.2

# Formato legado do Twitter, usado dentro do objeto `author` embutido.
_LEGADO = "%a %b %d %H:%M:%S %z %Y"


def parse_data(valor: str | None) -> str | None:
    """Normaliza as duas formas de `createdAt` para o ISO-8601 UTC do schema.

    Devolve None em vez de levantar: uma data torta no meio de uma página
    de 20 tweets não pode derrubar a coleta inteira — o evento sem data é
    descartado depois, com contagem.
    """
    # `isinstance` e não só `if not valor`: a promessa do docstring é não
    # levantar, e `.replace` num int levanta AttributeError, que nenhum dos
    # `except` abaixo pega.
    if not isinstance(valor, str) or not valor:
        return None
    try:
        return datetime.strptime(valor, _LEGADO).astimezone(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        pass
    try:
        iso = valor.replace("Z", "+00:00")
        return datetime.fromisoformat(iso).astimezone(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def bio_do_autor(autor: dict[str, Any]) -> str | None:
    """A bio, venha ela de onde vier. Ver armadilha 2 no topo do módulo."""
    direto = (autor.get("description") or "").strip()
    if direto:
        return direto
    aninhado = ((autor.get("profile_bio") or {}).get("description") or "").strip()
    return aninhado or None


def _alvo(kind: str, tweet: dict[str, Any] | None) -> Target | None:
    """Ponta de destino a partir de um tweet aninhado (retuitado ou citado)."""
    if not isinstance(tweet, dict):
        return None
    autor = tweet.get("author") or {}
    uid = autor.get("id")
    if not uid:
        return None
    return Target(kind=kind, uid=str(uid), handle=autor.get("userName"),
                  post_uid=str(tweet["id"]) if tweet.get("id") else None)


def normalize_tweet(tweet: dict[str, Any], *, cursor: str | None = None,
                    conta: str = "") -> NormalizedEvent | None:
    """Um tweet da API → um `NormalizedEvent`. Puro, sem rede.

    Devolve None para o que não dá para usar: sem id, sem autor com id, ou sem
    data legível. São as três coisas sem as quais a aresta não existe.
    """
    if not isinstance(tweet, dict) or not tweet.get("id"):
        return None
    autor = tweet.get("author") or {}
    if not autor.get("id"):
        return None
    quando = parse_data(tweet.get("createdAt"))
    if not quando:
        return None

    alvos: list[Target] = []
    # A precedência importa: um post pode ser mais de uma coisa, e `post_type`
    # é uma coluna só. Repost primeiro porque é o que o grafo de amplificação
    # pondera mais alto; resposta antes de citação, como no x_parquet.
    rt = _alvo(KIND_REPOST, tweet.get("retweeted_tweet"))
    if rt:
        alvos.append(rt)
    if tweet.get("isReply") and tweet.get("inReplyToUserId"):
        alvos.append(Target(
            kind=KIND_REPLY, uid=str(tweet["inReplyToUserId"]),
            handle=tweet.get("inReplyToUsername"),
            post_uid=str(tweet["inReplyToId"]) if tweet.get("inReplyToId") else None))
    qt = _alvo(KIND_QUOTE, tweet.get("quoted_tweet"))
    if qt:
        alvos.append(qt)

    # Menção por último, e só de quem ainda não é alvo por outra via — ver
    # armadilha 3. `tipo` sai de alvos[0], então a menção nunca rouba o rótulo
    # de um post que é retuíte ou resposta.
    ja = {a.uid for a in alvos}
    for m in (tweet.get("entities") or {}).get("user_mentions") or []:
        uid = str(m.get("id_str") or "")
        if uid and uid not in ja:
            ja.add(uid)
            alvos.append(Target(kind=KIND_MENTION, uid=uid,
                                handle=m.get("screen_name")))

    tipo = alvos[0].kind if alvos else "original"
    if tipo == KIND_MENTION:
        # menção não é um tipo de post: quem só menciona escreveu um original
        tipo = "original"
    # `repost` e `quote` já são os nomes de post_type; `reply` também. A
    # coincidência é do vocabulário de `events.py`, não acidente.

    return NormalizedEvent(
        platform=PLATFORM,
        kind="post",
        actor_uid=str(autor["id"]),
        actor_handle=autor.get("userName"),
        occurred_at=quando,
        cursor=cursor,
        cursor_account=conta,
        post_uid=str(tweet["id"]),
        post_type=tipo,
        text=tweet.get("text"),
        lang=tweet.get("lang"),
        targets=alvos,
        raw=tweet,
    )


def tweets_da_resposta(corpo: dict[str, Any]) -> list[dict[str, Any]]:
    """Os tweets, seja qual for o envelope.

    O provedor tem quatro formatos de envelope e não há regra: `last_tweets`
    embrulha em `data`, `advanced_search` não embrulha em nada. Tentar os dois
    é mais barato do que manter um mapa de endpoint para envelope.
    """
    if not isinstance(corpo, dict):
        return []
    direto = corpo.get("tweets")
    if isinstance(direto, list):
        return [t for t in direto if isinstance(t, dict)]
    dentro = (corpo.get("data") or {})
    if isinstance(dentro, dict) and isinstance(dentro.get("tweets"), list):
        return [t for t in dentro["tweets"] if isinstance(t, dict)]
    return []


def proxima_pagina(corpo: dict[str, Any]) -> str | None:
    """Cursor da próxima página, ou None quando acabou.

    A terminação é por `has_next_page`, não por cursor vazio: o catálogo do
    provedor avisa em maiúsculas que o cursor pode vir preenchido na última
    página, e um laço que confia nele não termina.
    """
    dentro = corpo.get("data") if isinstance(corpo.get("data"), dict) else corpo
    if not dentro.get("has_next_page"):
        return None
    return dentro.get("next_cursor") or None


class ErroDoProvedor(RuntimeError):
    """Falha que veio do provedor, já com o motivo separado do transporte."""

    def __init__(self, mensagem: str, *, status: int | None = None,
                 sem_credito: bool = False, excesso: bool = False):
        super().__init__(mensagem)
        self.status = status
        self.sem_credito = sem_credito
        self.excesso = excesso


def erro_do_corpo(corpo: dict[str, Any]) -> str | None:
    """A mensagem de erro, inclusive a que vem com HTTP 200.

    Falha semântica do provedor responde 200 com `{"status":"error"}`. Conferir
    só o código HTTP engoliria isso em silêncio, e uma coleta vazia pareceria
    uma semana sem assunto.
    """
    if not isinstance(corpo, dict):
        return None
    if corpo.get("error"):
        return str(corpo.get("message") or corpo["error"])
    if corpo.get("status") == "error":
        return str(corpo.get("msg") or "erro sem mensagem")
    return None


class XApiSource:
    """Coleta do X pelo twitterapi.io, em dois modos.

    `contas=[...]`  baseline: a timeline de cada perfil, paginada, com um
                    cursor por conta — é o que a migração 006 passou a guardar.
    `query="..."`   campanha: busca por termo, cursor único.

    O teto de gasto é aplicado ANTES da requisição, não depois: o saldo é lido
    no provedor na abertura e a cada página, e a coleta para quando o gasto
    alcança o limite. O que ficou de fora vai para `frame`, porque número de
    cobertura desconhecida não é número medido.
    """

    name = "twitterapi_io"

    def __init__(self, api_key: str, *, contas: list[str] | None = None,
                 query: str | None = None, cursores: dict[str, str] | None = None,
                 paginas_por_conta: int = 1, teto_usd: float | None = None,
                 conferir_saldo_a_cada: int = 10,
                 intervalo: float = INTERVALO_PADRAO, incluir_respostas: bool = True,
                 abrir: Any = None):
        if bool(contas) == bool(query):
            raise ValueError("passe contas OU query, nunca os dois nem nenhum")
        self.api_key = api_key
        self.contas = list(contas or [])
        self.query = query
        self.cursores = dict(cursores or {})
        self.paginas_por_conta = paginas_por_conta
        self.teto_usd = teto_usd
        self.conferir_saldo_a_cada = max(1, conferir_saldo_a_cada)
        self.intervalo = intervalo
        self.incluir_respostas = incluir_respostas
        self._abrir = abrir or urllib.request.urlopen
        self._ultima = 0.0
        self._desde_a_conferencia = 0
        self.skipped = 0
        self.saldo_inicial: int | None = None
        self.saldo_atual: int | None = None
        self.frame: dict[str, Any] = {
            "kind": "accounts" if contas else "search",
            "planned": len(self.contas) if contas else 1,
            "reached": 0,
            "left_out": [],
            "pages": 0,
            "budget_usd": teto_usd,
            "stopped_by": None,
        }

    # ---------- rede ----------

    def _esperar(self) -> None:
        falta = self.intervalo - (time.monotonic() - self._ultima)
        if falta > 0:
            time.sleep(falta)
        self._ultima = time.monotonic()

    def _get(self, caminho: str, **params: Any) -> dict[str, Any]:
        self._esperar()
        url = f"{BASE}{caminho}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v not in (None, "")})
        req = urllib.request.Request(url, headers={"x-api-key": self.api_key})
        try:
            with self._abrir(req, timeout=30) as resp:
                corpo = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            texto = exc.read().decode("utf-8", "replace")
            try:
                corpo = json.loads(texto)
            except ValueError:
                corpo = {"error": texto[:200]}
            raise ErroDoProvedor(
                str(corpo.get("message") or corpo.get("error") or exc.reason),
                status=exc.code, excesso=exc.code == 429) from exc

        motivo = erro_do_corpo(corpo)
        if motivo:
            raise ErroDoProvedor(
                motivo, status=200,
                excesso="QPS" in motivo or "Too Many" in motivo,
                sem_credito="credit" in motivo.lower() or "balance" in motivo.lower())
        return corpo

    def saldo(self) -> int:
        """Créditos disponíveis. É o que torna o custo medido e não estimado."""
        c = self._get("/oapi/my/info")
        return int(c.get("recharge_credits", 0)) + int(c.get("total_bonus_credits", 0))

    # ---------- custo ----------

    @property
    def gasto_usd(self) -> float:
        """Gasto MEDIDO: a diferença entre dois saldos lidos no provedor.

        Não é multiplicação de tabela de preço, e a diferença não é acadêmica —
        a primeira captura custou 18 créditos onde a tabela publicada previa
        45 ou mais. Número de custo em relatório sai daqui.
        """
        if self.saldo_inicial is None or self.saldo_atual is None:
            return 0.0
        return max(0, self.saldo_inicial - self.saldo_atual) / CREDITOS_POR_USD

    def _conferir_saldo(self, forcar: bool = False) -> None:
        """Atualiza o saldo, mas NÃO a cada página.

        Ler o saldo É uma requisição. Conferir a cada página faria metade do
        que se paga ser para saber quanto se está pagando — e, a uma requisição
        a cada 5 s, dobraria o tempo de parede: um baseline de 150 perfis iria
        de 12,5 para 25 minutos.

        O teto passa a ser conferido a cada N requisições. O excesso máximo
        vira o custo de N páginas — com N=10 e uma página a ~6 créditos, são
        US$ 0,0006 contra um teto de US$ 1,00 — e o erro é sempre para o lado
        de parar cedo, nunca tarde. O custo gravado no run continua MEDIDO,
        porque `forcar=True` no fim lê o saldo de verdade.
        """
        if self.teto_usd is None and not forcar:
            return          # sem teto, o saldo só interessa no começo e no fim
        self._desde_a_conferencia += 1
        if forcar or self._desde_a_conferencia >= self.conferir_saldo_a_cada:
            self._desde_a_conferencia = 0
            self.saldo_atual = self.saldo()

    def _estourou(self) -> bool:
        return self.teto_usd is not None and self.gasto_usd >= self.teto_usd

    # ---------- eventos ----------

    def events(self, cursor: str | None = None) -> Iterator[NormalizedEvent]:
        """`cursor` só é usado no modo de busca. No modo de contas a posição de
        cada perfil vem do dicionário `cursores` — um firehose tem uma posição,
        uma coleta por perfil tem uma por perfil."""
        self.saldo_inicial = self.saldo_atual = self.saldo()
        try:
            if self.query is not None:
                yield from self._buscar(cursor)
            else:
                yield from self._timelines()
        finally:
            # o saldo final vai no `finally` de propósito: um run que morreu no
            # meio gastou dinheiro, e o custo dele precisa estar gravado igual.
            self._conferir_saldo(forcar=True)

    def _buscar(self, cursor: str | None) -> Iterator[NormalizedEvent]:
        pagina = cursor or ""
        while True:
            if self._estourou():
                self.frame["stopped_by"] = "budget"
                return
            corpo = self._get("/twitter/tweet/advanced_search",
                              query=self.query, queryType="Latest", cursor=pagina)
            self.frame["pages"] += 1
            proxima = proxima_pagina(corpo)
            for t in tweets_da_resposta(corpo):
                ev = normalize_tweet(t, cursor=proxima or pagina)
                if ev is None:
                    self.skipped += 1
                    continue
                yield ev
            self._conferir_saldo()
            if not proxima:
                self.frame["reached"] = 1
                return
            pagina = proxima

    def _timelines(self) -> Iterator[NormalizedEvent]:
        for i, conta in enumerate(self.contas):
            if self._estourou():
                self.frame["left_out"] = self.contas[i:]
                self.frame["stopped_by"] = "budget"
                return
            yield from self._timeline_de(conta)
            self.frame["reached"] += 1

    def _timeline_de(self, conta: str) -> Iterator[NormalizedEvent]:
        pagina = self.cursores.get(conta, "")
        for _ in range(self.paginas_por_conta):
            if self._estourou():
                self.frame["stopped_by"] = "budget"
                return
            corpo = self._get("/twitter/user/last_tweets", userName=conta,
                              cursor=pagina,
                              includeReplies=str(self.incluir_respostas).lower())
            self.frame["pages"] += 1
            proxima = proxima_pagina(corpo)
            for t in tweets_da_resposta(corpo):
                # O cursor viaja no evento marcado COM A CONTA: sem isso, a
                # última conta paginada sobrescreveria a posição de todas.
                ev = normalize_tweet(t, cursor=proxima or pagina, conta=conta)
                if ev is None:
                    self.skipped += 1
                    continue
                yield ev
            self._conferir_saldo()
            if not proxima:
                return
            pagina = proxima
