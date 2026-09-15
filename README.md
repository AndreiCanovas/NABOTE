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
nabote dossie --topic X          # aprofundamento de uma pauta, em JSON

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
do X, tamanho e E-I correlacionam −0,73 *dentro de uma única janela* — nem é
efeito da densidade da coleta.

A causa não é sutil. A rede é audiência-em-torno-de-hub, e a maioria dos atores
amplificou **uma vez só**: grau 1, E-I −1 por aritmética. Essa gente não escolheu
ficar dentro do grupo — não houve segunda oportunidade de atravessar. A média
sobre todo mundo tende a −1 conforme a audiência cresce, e satura ali, onde
nenhuma diferença é mais visível.

Por isso `analyze` grava duas medidas:

| coluna | o que é |
| --- | --- |
| `ei_mean` | E-I cru, média sobre todos os atores — **não comparável entre tamanhos** |
| `ei_choice` | média só entre atores com força ≥ 2, quem teve mais de uma chance de atravessar |
| `choice_actors` | quantos atores sustentam o `ei_choice` |

Com audiências de 100 a 4.000 e comportamento relativo idêntico, a correlação
com o tamanho cai de −0,63 para −0,21 e os valores se agrupam em torno de −0,82
em vez de todos virarem −1,00. `tests/synthetic.py:hub_audience_graph` é o banco
de provas: métrica de fechamento que varia com o tamanho naquele grafo está
medindo tamanho, não fechamento.

`choice_actors` viaja junto porque importa: comunidade onde quase ninguém teve
escolha tem `ei_choice` frágil, e isso precisa ficar visível em vez de virar um
número bonito sem lastro.

**O que não funcionou.** A primeira tentativa foi um z-score contra um grafo
embaralhado (migração 003, revertida pela 004). No dado real ele saiu inútil: z
de −7,5 a +26,1, e só 38 de 1009 comunidades recebendo valor. A causa é
estrutural — a rede tem grau médio ~2, e num grafo tão esparso o Leiden acha, no
embaralhado, uma partição com E-I −1,0000 exato e desvio 0,00000. O nulo satura,
o z ou não existe ou explode. Também não serviu o excesso analítico sob modelo
de configuração: ele troca a correlação de −0,49 por +1,00.

#### Carregar um período inteiro, não um prefixo

`--files N` pega os N primeiros arquivos em ordem cronológica. Serve para a
primeira inspeção e para nada além disso: ele não tem como saber onde uma semana
termina, então corta no meio. A janela resultante tem volume decidido pelo
recorte, não pelo mundo — e como a análise agrega por semana, a série temporal
mente sem avisar.

```bash
nabote load-x BASE.zip --dry-run                         # o que existe no zip
nabote load-x BASE.zip --from 2022-12-26 --to 2023-01-01 --no-raw
```

O relatório de cobertura sai antes de qualquer carga:

```
semana             dias   termos   situação
--------------------------------------------------------
2022-12-26      4/4            2   completa
2023-01-02      2/3            2   PARCIAL — faltam 1 dia(s): 2023-01-05
```

"Completa" é relativa ao **zip**, não ao calendário: a coleta original foi em
dias esparsos de Trending Topics, então uma semana pode legitimamente ter três
dias. O que o relatório garante é que nenhum dia existente ficou de fora.

#### Dois números que precisam aparecer antes da leitura

**Concentração.** Um grafo pode ter centenas de nós e ser, na prática, uma
pessoa falando. No grafo de respostas da base histórica, **uma conta apareceu em
15 das 20 arestas mais pesadas**. Métrica de rede calculada ali descreve aquela
conta, não a rede — e sai parecendo achado coletivo. `analyze` avisa quando um
ator concentra 5% ou mais do peso de saída.

**In-degree junto do PageRank.** PageRank é herdado: quem é repostado por um hub
recebe quase todo o rank dele. Num grafo fragmentado isso põe contas de
in-degree 1 acima de contas com dezenas de arestas — aconteceu, seis de uma vez.
`dump --min-degree G` restringe a lista de atores a quem tem in-degree ponderado
≥ G, para o ranking poder ser lido como ranking.

#### Comparar visões: importar a partição, nunca recalcular

`amp` e `reply` são grafos diferentes. Rodar o Leiden em cada um produz
comunidades **próprias**: a "#7" de um não tem relação com a "#7" do outro, e
comparar as duas listas lado a lado gera uma tabela plausível e sem sentido.

A comparação certa define a comunidade por **quem você promove** (a visão `amp`)
e mede o comportamento de resposta contra essa definição:

```bash
nabote analyze --view amp --core                      # define as comunidades
nabote analyze --view reply --partition amp:core      # mede as respostas nelas
nabote themes  --view reply --partition amp:core
```

O segundo grava em `reply@amp:core` — a visão `reply` medida sobre a partição de
`amp:core`. A leitura:

| amp | reply | o que é |
| --- | --- | --- |
| fechada | fechada | clube isolado: não briga, só não sai |
| fechada | **aberta** | polarização: promove os seus, discute com os outros |
| aberta | aberta | não é bloco |

`--partition` também importa de **outra semana**, com `2023-01-16:amp:core`. Isso
separa fusão real de artefato de resolução: quando comunidades de uma semana
viram uma só na seguinte, o E-I não decide nada — fundir comunidades transforma
aresta externa em interna, então ele despenca por construção, tenha ou não
mudado o comportamento de alguém. Aplicando a partição ANTIGA ao grafo NOVO, o
E-I volta a informar: se os grupos continuam internos, foi o Leiden agrupando
mais grosso porque o grafo adensou; se passaram a se amplificar entre si, a
fusão é real.

Atores do grafo de respostas que não estão na partição ficam de fora, e o
`analyze` diz quantos: número alto significa que os dois grafos mal se
sobrepõem e a comparação não se sustenta.

#### Alcance e fechamento são duas análises

No dado real **71% a 79% dos atores aparecem com uma aresta só**. Isso não é
ruído: é a forma da rede. E significa que a detecção de comunidade sobre o grafo
inteiro é conduzida por gente que apareceu uma vez — "comunidade" acaba querendo
dizer "quem amplificou o hub X uma vez", que é uma lista de fãs, não um grupo.

Daí duas análises que convivem, gravadas em escopos separados:

```bash
nabote analyze --all --view amp          # escopo amp      — alcance
nabote analyze --all --view amp --core   # escopo amp:core — fechamento
```

| | grafo | mede | por quê |
| --- | --- | --- | --- |
| `amp` | inteiro | alcance: quem é amplificado, por quantos | a audiência de uma aresta **é** o alcance; tirá-la apagaria o que se quer medir |
| `amp:core` | só quem tem 2+ arestas | fechamento: quem teve chance de atravessar e não atravessou | quem apareceu uma vez não escolheu nada |

A poda repete até estabilizar: remover quem tem uma aresta reduz o grau de quem
sobrou e pode deixar alguém novo com uma aresta só. É a ideia do k-core, com
força ponderada no lugar do grau.

`dump` e `themes` também aceitam `--core` e leem o escopo certo.

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

### Recorte por pauta (níveis 1 e 2)

```bash
nabote aggregate --all --by-topic          # um escopo por pauta, ao lado do cheio
nabote analyze --all --view amp --scope topic:Yanomami
nabote radar --all --view amp --out radar.json
```

`--by-topic` materializa `topic:<rótulo>` recortando as interações àquela coleta.
Isso é o que permite perguntar **quem é central NESTA pauta** em vez de "no
grafo" — e o mesmo perfil aparece em posições muito diferentes conforme a pauta,
que é justamente o comportamento que uma métrica global esconderia.

O escopo de pauta antes não recortava nada: gravava o grafo inteiro sob o rótulo
da pauta. Toda métrica calculada ali descreveria a rede toda enquanto dizia
descrever uma pauta.

`radar` reúne num JSON só o que o relatório recorrente pede: volume, autores e
concentração por pauta, por quantas comunidades ela circula e qual domina, os
atores centrais dentro dela, e as comunidades com fatia de volume e pautas.

### Dossiê — aprofundamento de uma pauta

```bash
nabote aggregate --window 2023-01-23 --scope topic:Yanomami
nabote analyze   --window 2023-01-23 --view amp --scope topic:Yanomami --core
nabote dossie    --window 2023-01-23 --topic Yanomami --out dossie.json
```

O radar decide onde aprofundar; o dossiê aprofunda. Tudo sai do escopo
`amp:topic:<pauta>:core` — um grafo só com as interações daquela coleta —, e o
comando recusa rodar se o escopo não existir, em vez de devolver zeros.

Quatro coisas existem só aqui:

| | o que é | limite declarado |
|---|---|---|
| **mapa** | subgrafo dos N mais centrais, com esqueleto por filtro de disparidade | diz quantos nós ficaram de fora, quantas ligações foram cortadas e em que nível |
| **eixo** | análise de correspondência sobre a matriz de amplificação | só para quem amplificou ≥ 2 contas; o sinal é convenção, não achado |
| **coamplificação** | pares que amplificaram o mesmo alvo em ≤ 60 s | a chave é o ator alvo, não o post; viralidade em massa é excluída e contada |
| **sub-pautas** | n-gramas que cada comunidade usa desproporcionalmente | `lift` = P(termo\|comunidade) / P(termo); exige `post.text` no banco |

O eixo não usa numpy: a primeira dimensão sai por iteração de potência com
deflação do par singular trivial, que é O(arestas) por passo. Sem a deflação a
conta converge para a dimensão que ordena por TAMANHO, e a tabela sai plausível
medindo volume em vez de posição — é o que `test_nao_e_so_tamanho_disfarcado`
existe para impedir.

**A dimensão 1 pode degenerar.** No dado real (Yanomami, S04) σ₁ deu 0,993 e as
comunidades #1 a #7 caíram TODAS em +0,285 ± 0,003, contra −0,954 da #0: com um
bloco quase desconexo, a primeira dimensão vira o indicador daquele bloco — um
teste de "é a #0 ou não" — e não um eixo de posições. Por isso o comando calcula
**duas** dimensões e avisa quando a primeira degenera; o posicionamento que
sobra está na segunda.

**Δ entre janelas é de POSTO, não de escore.** Cada janela renormaliza pelo
próprio extremo e ancora o sinal na própria comunidade #0, que não é a mesma de
uma semana para a outra. Comparar escores crus deu Δ ≈ +0,287 idêntico para seis
perfis de comunidades diferentes — reescala, não movimento.

**Leia σ₁, não "% da inércia".** A matriz é esparsíssima (20 mil amplificadores,
mil e poucos alvos, dois alvos por amplificador) e nesse regime a inércia total
é dominada por células vazias: duas metades *perfeitamente* separadas, com
σ₁ = 1,000, aparecem como 0,5% da inércia. O número interpretável é σ₁ sozinho —
a correlação entre a posição de quem amplifica e a de quem é amplificado.

O comando também reporta **quanto o sinal do eixo coincide com a partição do
Leiden**. Acima de 95% ele avisa: o eixo virou a partição repintada e não é uma
medida independente das comunidades. É a diferença entre um achado e o mesmo
achado vendido duas vezes.

**O mapa precisa de esqueleto.** A projeção por audiência compartilhada é quase
completa — 1.079 ligações entre 60 perfis, densidade 0,60 — e um layout de força
sobre isso colapsa tudo em manchas. O filtro de disparidade guarda as ligações
desproporcionais *de cada nó*, o que preserva o perfil pequeno que um corte por
peso absoluto apagaria. O nível não é constante: a escada sobe até ninguém ficar
sem ligação, porque nó solto num layout de força é empurrado para a periferia
por repulsão pura e a posição dele não significa nada. Medido na densidade real,
alfa 0,05 deixava 27 dos 60 perfis soltos; 0,10 deixa zero.

As sub-pautas usam poda progressiva (só monta trigrama cujos bigramas passaram
no corte). Sem ela, um corpus de 143 mil posts gera milhões de n-gramas
distintos e o processo morre por memória; com ela são 6 s e 152 MB.

### Uma pauta, vários rótulos

```bash
nabote dossie --topic CPMI --topic '#CPMIdoGolpe' --window 2023-05-22
```

A coleta por Trending Topic parte a MESMA pauta em etiquetas diferentes. Na base
real, `CPMI` e `#CPMIdoGolpe` são seis semanas do mesmo assunto; `Xandão` e
`Alexandre de Moraes`, a mesma pessoa. Analisá-los separados divide o grafo da
pauta ao meio por acidente de rótulo.

Os rótulos entram no **nome do escopo** (`topic:CPMI+#CPMIdoGolpe`), e não numa
tabela de apelidos, porque `scope` é gravado em toda linha de métrica e precisa
continuar dizendo o que contém seis meses depois. Ordenados, para que
`topic:A+B` e `topic:B+A` não virem dois escopos com o mesmo conteúdo.

`tools/termos.py <zip>` lista quais termos aparecem em quantas semanas, lendo só
os nomes dos arquivos — é o que decide qual pauta sustenta um dossiê com
trajetória, antes de gastar carga.

### Nomes de comunidade

```bash
nabote label --window 2023-05-22 --scope amp:topic:CPMI:core
nabote label --window 2023-05-22 --scope amp:topic:CPMI:core --set 2="Crime ambiental"
```

Um relatório que chama os grupos de "#0", "#1" e "#2" não informa nada: para ler
a tabela de atores é preciso decorar a de comunidades. O dossiê propõe um nome a
partir dos n-gramas que a comunidade usa desproporcionalmente e mostra a
evidência ao lado — termos e perfis de topo. `label --set` troca pelo nome do
analista; a evidência continua saindo do dado, então o rótulo permanece
auditável contra o que o justificou.

### Comparar dois recortes

```bash
nabote compare 2023-01-23:amp:core 2023-01-23:reply@amp:core   # visões
nabote compare 2023-01-16:amp:core 2023-01-23:amp:core          # semanas
```

Responde as duas perguntas que a estrutura de campos levanta: *promove os seus
E discute com os outros?* e *os campos são os mesmos toda semana?*

O casamento é por **sobreposição de membros**, nunca por número de comunidade. A
numeração é por tamanho dentro do recorte, então "#0" de uma semana não é "#0"
da seguinte, e uma tabela alinhada por número sairia plausível e falsa.

O par é escolhido por **interseção bruta** — quem compartilha mais gente de
verdade. As razões aparecem na saída, não na escolha:

| coluna | o que é |
| --- | --- |
| `comum` | atores presentes nos dois |
| `do A` | que fatia de A eles são |
| `do B` | que fatia de B eles são |
| `jacc` | semelhança simétrica |

`do B` 100% com `do A` baixo é **cobertura, não discordância**: B é uma amostra
de A. É o caso normal ao comparar visões, porque o grafo de respostas cobre uma
fração do de amplificação.

Duas medidas foram testadas e descartadas contra dado real, cada uma errando
para um lado: **Jaccard** pune diferença de tamanho, e declarou "sem par"
comunidades cujos membros eram literalmente os mesmos (4.164 em 34.165 = 0,12).
**Contenção**, dividindo pelo menor, premia o inverso: três atores dentro de uma
comunidade de 26 mil davam 1,00, e ruído virava casamento perfeito. Interseção
bruta não se deixa enganar por nenhum dos dois, e três de 26.386 se denuncia
sozinho na tela.

### Exportação (passo 4)

```bash
nabote export --window 2023-01-23 --view amp --core --out relatorios/
```

Escreve em `relatorios/<janela>_<escopo>/`:

| arquivo | conteúdo |
| --- | --- |
| `actors.csv` | um ator por linha, comunidade e todas as métricas em colunas, ordenado por PageRank |
| `communities.csv` | uma comunidade por linha, com `ei_mean`, `ei_choice`, `choice_actors` e as pautas já resolvidas em texto |
| `edges.csv` | arestas da visão pedida, restritas ao escopo |
| `runs.csv` | procedência: qual coleta alimentou esta janela e com que volume |
| `manifest.json` | janela, escopo, dias de coleta, termos, contagens e **as ressalvas** |

CSV e não um formato esperto porque abre no Excel, no pandas e no R — o
consumidor é um analista, não um sistema.

O manifesto não é burocracia. Um CSV solto não diz de qual janela veio, de qual
escopo, nem quantos dias de coleta o alimentaram; esta POC gastou três mensagens
interpretando dados cuja procedência ninguém tinha verificado. E as ressalvas
conhecidas do recorte viajam dentro dele, não no README: quem recebe o arquivo
não leu a conversa, e número sem ressalva vira slide.

### Testes

```bash
python3 -m unittest discover -s tests
```

221 testes, sem dependências e sem rede. Rodam também sob `pytest` se preferir.

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
