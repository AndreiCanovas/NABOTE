# NABOTE

Instrumento analítico interno para mapear a rede do discurso público brasileiro.

**Não é um produto.** É a base a partir da qual os produtos de análise são
construídos: ele responde quem interage com quem, quais comunidades existem,
sobre o que cada uma fala, e como isso muda ao longo do tempo.

Restrição de projeto: **custo recorrente perto de zero** até que algum produto
dependa de fato do instrumento. Toda decisão técnica aqui é subordinada a isso.

---

## Estado atual — passo 1 de 5

| Passo | O quê | Estado |
|-------|-------|--------|
| **0** | Schema, banco, migrações | ✅ feito |
| **1** | Coleta (Bluesky Jetstream → `post` + `interaction`) | ✅ **feito** |
| 2 | Grafo (`edge_window` + igraph: comunidades, PageRank, E-I) | a fazer |
| 3 | Tópicos (embeddings, clustering, `post_topic`) | a fazer |
| 4 | Exportação (radar semanal e dossiê sob encomenda) | a fazer |
| 5 | Ligar o X (agregador terceiro na mesma interface de fonte) | a fazer |

A ordem é de baixo para cima porque cada passo só é testável se o anterior
estiver produzindo dado real. O X entra por último, de propósito: todo o risco
de engenharia é queimado no Bluesky, que é gratuito e ilimitado.

**O passo 2 é o momento de verdade.** É onde se descobre se a curadoria de
sementes produz *um* grafo conectado ou quarenta componentes isolados. Se for o
segundo caso, nada a jusante importa — e isso aparece na terceira semana, de
graça, sem uma linha de NLP escrita.

---

## Uso

Sem dependências externas no passo 0 — só a biblioteca padrão do Python 3.11+.

```bash
python3 -m nabote.cli init      # cria o banco e aplica as migrações
python3 -m nabote.cli status    # versão do schema, volume, custo acumulado

# coleta a partir da fixture sintética — sem rede, determinística
python3 -m nabote.cli fetch --fixture tests/fixtures/jetstream_sintetico.jsonl --author-tier A

# coleta real do Bluesky, filtrando pela lista curada, com teto
python3 -m nabote.cli fetch --seeds seeds/meus_perfis.txt --author-tier A --max-seconds 300
```

`fetch` salva o cursor a cada 250 eventos: se cair ou você der Ctrl-C, a próxima
execução retoma de onde parou em vez de perder a janela. Os tetos `--max-events`
e `--max-seconds` existem porque o plano exige que orçamento seja código — aqui
o custo é zero, mas a mesma função vai servir para o X.

### Lista de sementes

Um DID por linha, `#` comenta (veja `seeds/exemplo.txt`). Para resolver um handle:

```bash
curl "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle=NOME.bsky.social"
```

Sem `--seeds`, `fetch` consome o firehose global — centenas de eventos por
segundo. Útil para conhecer o formato, inviável como coleta.

Se o pacote não estiver instalado, prefixe com `PYTHONPATH=src`. Para instalar
em modo editável: `pip install -e .` (aí o comando `nabote` fica disponível).

O banco vive em `data/nabote.db` por padrão e **não é versionado**.

### Testes

```bash
python3 -m unittest discover -s tests
```

39 testes, sem dependências e sem rede. Rodam também sob `pytest` se preferir.

Os testes de ingestão rodam contra `tests/fixtures/jetstream_sintetico.jsonl`,
que é **inventado à mão, não capturado**. Versionar posts reais de pessoas num
repositório é exatamente a prática que gerou a polêmica do dataset de Bluesky
em 2024 — e a fixture sintética exercita o parser igual.

---

## Decisões de arquitetura

**SQLite, não Postgres nem banco de grafos.** No volume do MVP (~8 mil nós,
~150 mil arestas) o `igraph` roda Louvain, PageRank e betweenness em menos de
dois segundos em memória. Um banco de grafos resolve o problema de *servir
travessia interativa a terceiros* — que não é o problema deste projeto.
Gatilho para Postgres: escrita concorrente, não tamanho.

**Tabelas `STRICT`.** Exigem SQLite ≥ 3.37 e fazem o tipo declarado valer de
verdade. Sem elas, texto numa coluna `INTEGER` passa em silêncio — a classe de
bug mais cara de descobrir tarde.

**As regras vivem no banco, não na cabeça de quem escreve o `INSERT`.**
Campanha sem rótulo, autointeração, figura pública sem critério registrado e
custo negativo são todos rejeitados por `CHECK`. Regra que depende de
disciplina humana não sobrevive a seis meses de uso.

### Cinco invariantes que o schema existe para proteger

1. **Ator é agnóstico de plataforma.** Chave natural `(platform,
   platform_user_id)` — nunca o handle, que muda. É o que permite Bluesky e X
   coexistirem e outra plataforma entrar depois sem migração.
2. **`post` é append-only.** `interaction` é *derivada* e sempre recomputável.
   Quando a extração de arestas melhorar, recompute — nunca transforme
   destrutivamente.
3. **Janela *e* escopo em tudo que é analítico.** `window_start` dá evolução
   temporal; `scope` (`global` ou `topic:<id>`) permite que o mesmo ator tenha
   PageRank diferente no grafo estrutural e dentro de cada tema. Faltando um
   dos dois, adicionar depois é reescrita.
4. **Payload bruto é arquivo.** Gravado antes de qualquer parsing. Post apagado
   não volta, e em monitoramento político é o dado mais valioso que existe.
5. **Custo e intensidade são colunas.** `cost_usd` decide todo upgrade de tier;
   `kind` (`baseline` | `campanha`) separa coleta fina contínua de coleta
   profunda sob demanda. Sem `kind`, um pico de volume é indistinguível de uma
   mudança na própria intensidade de coleta — erro que invalida a análise e é
   quase indetectável depois.

### Camada de fonte

`sources/` isola de onde o dado vem. `JetstreamSource` fala com o Bluesky;
`FixtureSource` lê um JSONL. As duas emitem o mesmo formato de evento, e nada
fora do pacote sabe a diferença.

Isso não é abstração gratuita: o agregador de X que o projeto vai usar no passo
5 opera em zona cinzenta e pode sumir sem aviso. Trocar de provedor precisa
caber numa tarde, e só cabe se essa fronteira existir desde o começo.

### Exclusões

O firehose emite eventos de exclusão, e o AT Protocol é descentralizado —
apagar um post no Bluesky não remove cópias que terceiros baixaram. As
Diretrizes de Desenvolvedor exigem que quem guardou apague.

A regra implementada (a conservadora):

- **o conteúdo sai** — `text` vira `NULL` e a linha de `raw_payload` é removida,
  em todos os runs;
- **a aresta fica**, com `deleted_at` preenchido.

A interação é um fato de rede com data; o texto é a expressão da pessoa. Apagar
a aresta também faria uma janela já fechada mentir retroativamente.

Reter conteúdo de **figura pública** (um parlamentar apagar um post é fato de
interesse público) é uma condição no código de ingestão, não mudança de schema:
`actor.is_public_figure` já existe para isso. Hoje está desligado.

### Tiers de ator

- **A** — coleta semanal, timeline mais profunda
- **B** — coleta mensal, amostra rasa
- **C** — **nunca coletado**; existe no grafo apenas como alvo de aresta

O tier C é o que permite medir influência sem pagar para coletar o influente:
a relevância de um perfil é medida pelos posts *dos outros*, que já foram pagos.

### Fronteira de publicação

Opinião política é dado pessoal sensível (LGPD, art. 5º, II), e legítimo
interesse não cobre dado sensível. Por isso a fronteira de compliance **não é o
banco, é a publicação**: o instrumento pode guardar escore individual com base
legal documentada; o que sai num entregável passa por filtro.

O schema sustenta isso com `actor.is_public_figure` + `public_figure_reason`
(obrigatório, por `CHECK`) e `export_log`, que registra o que saiu, quando e
para qual entregável. **O filtro em si vive no código de exportação** — filtro
em código é auditável, disciplina humana não é.

---

## Estrutura

```
migrations/001_initial.sql   schema — o documento mais importante do repositório
migrations/002_*.sql         exclusões honradas e cursor de retomada
src/nabote/db.py             conexão, pragmas, migrações
src/nabote/atproto.py        normalização de registros AT Protocol (puro, sem I/O)
src/nabote/sources/          jetstream (rede) e fixture (arquivo), mesma interface
src/nabote/ingest.py         evento → raw_payload → actor/post/interaction
src/nabote/cli.py            init, status, fetch
tests/test_schema.py         invariantes aplicados pelo banco
tests/test_ingest.py         o que o parser deduz das arestas
seeds/exemplo.txt            formato da lista curada
```

Migrações seguem `NNN_descricao.sql` e são aplicadas em ordem, uma vez só,
registradas em `schema_migrations`.
