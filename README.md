# NABOTE

Instrumento analítico interno para mapear a rede do discurso público brasileiro.

**Não é um produto.** É a base a partir da qual os produtos de análise são
construídos: ele responde quem interage com quem, quais comunidades existem,
sobre o que cada uma fala, e como isso muda ao longo do tempo.

Restrição de projeto: **custo recorrente perto de zero** até que algum produto
dependa de fato do instrumento. Toda decisão técnica aqui é subordinada a isso.

---

## Estado atual — passo 2 de 5

| Passo | O quê | Estado |
|-------|-------|--------|
| **0** | Schema, banco, migrações | ✅ feito |
| **1** | Coleta (Bluesky Jetstream → `post` + `interaction`) | ✅ feito |
| **2** | Grafo (`edge_window` + igraph: comunidades, PageRank, E-I) | ✅ **feito** |
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

### Onde rodar

O código roda **na sua máquina ou num Codespace** — precisa de saída de rede
para o Bluesky (`public.api.bsky.app` e `jetstream*.bsky.network`).

**Sem instalar nada — GitHub Codespaces.** No repositório: botão verde `Code`
→ aba `Codespaces` → `Create codespace`. Abre um VS Code no navegador com
Python e o projeto já instalado (`.devcontainer/` cuida disso). Vá no terminal
e rode os comandos abaixo. Contas pessoais têm cota gratuita mensal.

**Na sua máquina.** Precisa de Python 3.11+ e git:

```bash
python3 --version      # precisa ser 3.11 ou maior
git --version
```

#### Sem pip? `discover` roda assim mesmo

`discover` e `seeds` usam só a biblioteca padrão — nada para instalar:

```bash
PYTHONPATH=src python3 -m nabote.cli discover --file seeds/politica_br_nomes.txt
```

As dependências só entram depois: `websockets` para o `fetch` e `igraph` para
o `analyze`. Quando chegar lá, no Debian/Ubuntu:

```bash
sudo apt install python3-pip python3-venv
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

O ambiente virtual evita mexer no Python do sistema, que em distribuições
recentes recusa instalação global de qualquer jeito.

### Começando do zero — quatro comandos

```bash
pip install -e .
nabote init

# escreva seus perfis em seeds/meus.txt (um handle por linha) e registre:
nabote seeds --file seeds/meus.txt

# coleta + grafo + métricas + dump, num comando só:
nabote cycle --max-seconds 300
```

`cycle` encadeia `fetch → aggregate → analyze → dump`. Rodar de novo retoma do
cursor e recomputa a janela — pode chamar quantas vezes quiser.

### Comandos

```bash
nabote seeds --file lista.txt   # resolve handles → DIDs e registra como tier A
nabote seeds                    # lista as sementes registradas
nabote status                   # schema, volume, custo acumulado
nabote fetch --max-seconds 300  # só a coleta; usa as sementes do banco
nabote fetch --global           # firehose inteiro, sem filtro (só para ver o formato)
nabote aggregate --all          # interaction → edge_window
nabote analyze --all --view amp # comunidades, PageRank, E-I
nabote dump --view amp --top 20 # dump cru, para depuração

# sem rede, com a fixture sintética:
nabote fetch --fixture tests/fixtures/jetstream_sintetico.jsonl --author-tier A
```

`fetch` salva o cursor a cada 250 eventos: se cair ou você der Ctrl-C, a próxima
execução retoma de onde parou em vez de perder a janela. Os tetos `--max-events`
e `--max-seconds` existem porque o plano exige que orçamento seja código — aqui
o custo é zero, mas a mesma função vai servir para o X.

### Lista de sementes

Um perfil por linha, `#` comenta (veja `seeds/exemplo.txt`). Aceita **handle**
(`fulano.bsky.social`, `@fulano`, `fulano`, `jornal.com.br`) ou DID direto —
`nabote seeds` resolve e guarda os dois. Handle muda, DID não; é o DID que
filtra o firehose.

Um handle errado é reportado e **não derruba o resto da lista**.

Mantenha a lista versionada: a curadoria é o ativo do projeto, e o histórico de
quem entrou e saiu é informação.

### Do dado ao grafo

```bash
nabote aggregate --all                  # interaction → edge_window
nabote analyze --all --view amp         # comunidades, PageRank, E-I
nabote dump --view amp --top 15         # dump cru, para depurar
nabote runs                             # de onde veio cada aresta
```

```bash
nabote themes --view amp --min-community 500   # do que cada comunidade falava
```

`themes` é o **nível 1** do plano: a pauta emerge da estrutura. A comunidade é
descoberta pelo grafo, sem olhar texto nenhum; só depois se pergunta sobre o que
ela falava. Nesta base o rótulo sai de graça, porque a coleta foi por Trending
Topic e o termo veio no nome do arquivo — o que faz dele **gabarito** para
conferir o clustering de texto do passo 3.

O termo é atribuído pelos posts autorados na comunidade. Ator Tier C não
escreveu nada na amostra, então não vota: ele é alvo, não voz.

`runs` mostra qual termo alimentou qual janela. Numa coleta por termo isso não é
metadado burocrático: o termo é parte do resultado. Um grafo montado com "bbb23"
e um montado com "impeachment" não são a mesma rede vista duas vezes, e sem a
procedência comunidade, centralidade e E-I ficam calculáveis e ininterpretáveis.

`dump` não é a camada de exportação — é instrumento de depuração, e existe
justamente para você enxergar o que a coleta trouxe antes de existir qualquer
relatório. Os relatórios vêm no passo 4.

#### Quando o grafo vem fragmentado

`analyze` reporta, além do número de comunidades, o tamanho do **maior
componente** e a fração do grafo que ele cobre. Isto é o que distingue estrutura
de ruído: contar comunidades não denuncia nada, porque o Leiden não junta o que
o grafo já separou — cada componente isolado vira pelo menos uma comunidade.

Coleta por termo produz isso em massa: a maioria dos atores aparece uma vez só e
sai como díade solta. Uma comunidade de dois atores tem E-I −1,00 **por
construção** — não existe aresta externa possível —, então o número parece
"câmara de eco fechada" e não significa nada.

```bash
nabote dump --view amp --min-community 50   # TODAS as comunidades com 50+ atores
nabote dump --view amp --community 0        # quem está na maior comunidade
```

`--min-community` ignora o `--top` de propósito: quem pede "todas acima de 50"
quer todas. A cauda escondida vira uma linha de resumo, para você nunca
confundir "filtrei" com "não existe", e o `dump` abre com um histograma de
tamanhos — é a linha que responde "isto é uma rede ou uma pilha de cacos?".

#### E-I sozinho não é comparável

O E-I anda junto com o **tamanho** da comunidade. No dado real da base histórica
do X, tamanho e E-I correlacionam −0,73 *dentro de uma única janela* — ou seja,
nem é efeito da densidade da coleta.

O mecanismo é exato e não tem nada de sutil. A comunidade típica é a audiência
de um hub: toda folha reposta só o hub, então tem E-I −1; a única contribuição
externa, a do hub, é diluída por 1/tamanho. Duas estrelas com o **mesmo**
comportamento, hub com três arestas para fora:

```
 10 folhas  →  E-I -0,958
500 folhas  →  E-I -1,000
```

A estrela grande parece mais fechada sem ninguém ter agido diferente.

Por isso `analyze` roda um **modelo nulo**: embaralha as arestas preservando o
grau de cada nó, roda o Leiden de novo no grafo embaralhado e compara cada
comunidade observada com as comunidades nulas **de tamanho parecido**. O
resultado é `ei_z`, gravado junto do `ei_mean` e mostrado por `dump` e `themes`:

```
z ≈ 0    fechamento igual ao que o acaso produz nesse tamanho — não há achado
z ≪ 0    fechada além do que tamanho e graus explicam — câmara de eco de fato
```

Custa ~17s para 20 rodadas num grafo de 68 mil nós. `analyze --null 0` desliga e
deixa as colunas nulas.

Refazer a detecção no grafo embaralhado é o detalhe que faz o nulo funcionar.
Manter a partição original não serve: ela foi ajustada àquele grafo e vence
qualquer embaralhamento dele por construção — na primeira versão, um grafo sem
estrutura nenhuma saía com z −9, parecendo achado.

#### O número da comunidade

`#0` é sempre a **maior** comunidade da janela, `#1` a segunda, e assim por
diante. Duas coisas garantem isso:

- a semente do Leiden é fixa (`graph.LEIDEN_SEED`), porque o algoritmo é
  heurístico e aleatório — sem semente, a mesma janela analisada duas vezes
  devolve números diferentes e qualquer relatório que cite "#197" vira ficção;
- as comunidades são renumeradas por tamanho depois da detecção, com empate
  desfeito pelo menor índice de nó.

Isto **não** resolve identidade entre janelas: uma comunidade que cresce troca
de posição de uma semana para a outra. Rastrear a mesma comunidade ao longo do
tempo é casamento por sobreposição de membros, e é outro problema.

Se o pacote não estiver instalado, prefixe com `PYTHONPATH=src`. Para instalar
em modo editável: `pip install -e .` (aí o comando `nabote` fica disponível).

O banco vive em `data/nabote.db` por padrão e **não é versionado**.

### Testes

```bash
python3 -m unittest discover -s tests
```

145 testes, sem dependências e sem rede. Rodam também sob `pytest` se preferir.

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

### Duas visões do grafo, nunca misturadas

| Visão | Arestas | Pergunta |
|-------|---------|----------|
| `amp` (padrão) | repost ×1,0 + citação ×0,8 | quem se alinha com quem |
| `reply` | respostas | quem confronta quem |
| `all` | tudo, ponderado | atenção total |

Quem mais responde um ator costuma ser quem mais **discorda** dele. Somar
resposta com repost produz comunidades sem sentido — é o erro analítico mais
comum da área, e por isso a separação é o padrão, não uma opção.

`tests/test_graph.py` planta comunidades conhecidas nos reposts e respostas
que **cruzam** essas comunidades de propósito; depois verifica que o Leiden
recupera a partição plantada nó a nó e que as duas visões dão partições
diferentes. Se a separação deixar de valer, o teste quebra.

**Escopo** tem gramática:

```
edge_window.scope    'all' | 'topic:<id>'           QUAIS posts entram
actor_metric.scope   '<view>' | '<view>:topic:<id>' QUAL visão sobre eles
```

`edge_window` guarda **contagem**, não peso interpretado — a ponderação por
tipo é aplicada ao montar o grafo. Mudar de ideia sobre quanto vale uma
citação não obriga a reagregar nada.

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
src/nabote/graph.py          edge_window, igraph, comunidades e métricas
src/nabote/cli.py            init, status, fetch, aggregate, analyze, dump
tests/test_schema.py         invariantes aplicados pelo banco
tests/test_ingest.py         o que o parser deduz das arestas
tests/test_graph.py          recuperação de comunidades plantadas
tests/synthetic.py           gerador determinístico de grafo com gabarito
seeds/exemplo.txt            formato da lista curada
```

### Métricas do MVP

`in_degree_w`, `out_degree_w`, `pagerank`, `ei_index` e `betweenness` — todas
em segundos no volume previsto. Duas sutilezas que já estão tratadas:

- **Betweenness trata peso como distância.** Aresta forte precisa virar caminho
  curto, então o peso é invertido antes do cálculo. Sem isso o resultado sai com
  o sentido trocado — e parece plausível, que é o pior tipo de erro.
- **Detecção de comunidade roda sobre a versão não-dirigida.** Modularidade é
  definida assim; a conversão soma os pesos das duas direções. A direção
  continua valendo para PageRank e in-degree.

Migrações seguem `NNN_descricao.sql` e são aplicadas em ordem, uma vez só,
registradas em `schema_migrations`.
