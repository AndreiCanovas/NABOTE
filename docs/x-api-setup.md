# Ligar o X — setup do provedor

Runbook do passo 5. Você executa este documento uma vez; depois ele serve para
quando a chave vazar, o crédito acabar ou o provedor sumir.

**Estado:** conta criada (20/09/2026). Faltam os endpoints; nada em
`sources/x_api.py` ainda.

---

## Por que um agregador e não a API oficial

Em **fevereiro de 2026 a X acabou com o tier gratuito** para novos
desenvolvedores e fechou Basic (US$ 200/mês) e Pro (US$ 5.000/mês) para quem se
cadastra agora. Quem entra hoje cai em *pay-per-use*: **US$ 0,005 por post
lido**, ou US$ 5 por mil posts.

O twitterapi.io cobra **US$ 0,15 por mil tweets**, pré-pago, sem mensalidade.
É 33× mais barato, e é a diferença entre a restrição do projeto — custo
recorrente perto de zero — valer ou não valer.

O custo da coleta aqui não é o tamanho da pauta: é **quantos perfis tier A e B
existem × quantos posts por ciclo**. Tier C nunca é coletado, só aparece como
alvo de aresta. É isso que mantém a conta pequena.

| Cenário | twitterapi.io | X oficial |
|---|---|---|
| 150 perfis tier A × 20 posts, semanal | **US$ 1,80/mês** | US$ 60/mês |
| 500 perfis tier A × 20 posts, semanal | **US$ 6,00/mês** | US$ 200/mês |
| Campanha do tamanho da semana da CPMI (136.980 posts) | **US$ 20,55** | US$ 684,90 |

Com `NABOTE_BUDGET_USD_PER_CYCLE=1.00`, o teto por ciclo é de **6.666 posts**
no agregador e de 200 posts na oficial.

## O que você está assumindo ao usar isto

Registrado aqui porque a decisão precisa ser auditável depois, e porque quem
ler este repositório daqui a seis meses merece saber o que foi pesado.

- **Não é crime.** `hiQ Labs v. LinkedIn` (9º Circuito, 2022): ler dado
  publicamente acessível não viola o CFAA. Não há equivalente criminal no
  Brasil para leitura de página pública.
- **Viola os termos da X, e a X perdeu quando tentou cobrar isso.**
  `X Corp. v. Bright Data`, maio de 2024, juiz Alsup: caso julgado
  improcedente. A Lei de Direito Autoral americana preempta as alegações
  contratuais da X, e a X não tem propriedade de fato sobre o conteúdo que os
  usuários dela tornaram público. **É decisão de primeira instância e
  recorrível** — é o melhor precedente que existe, não uma garantia.
- **A política de uso aceitável do próprio provedor** proíbe o cliente de usar
  o serviço violando os termos da X. Eles transferem o ônus para você.
  Leia antes de pôr cartão.
- **LGPD não muda com o provedor.** Dado público continua sendo dado pessoal
  (art. 7º §4º), e opinião política é dado sensível (art. 5º, II), que legítimo
  interesse não cobre. Essa restrição é a mesma vinda da API oficial, do
  agregador ou do arquivo do Zenodo — e já está resolvida no desenho, pela
  fronteira de publicação.

**O risco de verdade é de continuidade, não jurídico:** o provedor raspa a
plataforma e pode quebrar ou sumir sem aviso. É exatamente por isso que
`sources/` existe desde o passo 0 — veja *Se o provedor sumir*, no fim.

**O dia em que isso muda:** quando o NABOTE alimentar produto para cliente,
redistribuição deixa de ser hipótese e passa a valer a pena a licença oficial.
Planeje para esse dia, não para hoje.

---

## Parte 1 — o que fazer no provedor

Nada aqui exige conta na X, telefone verificado nem formulário de aprovação.

- [ ] **Criar a conta** em `twitterapi.io` — e-mail ou Google.
- [ ] **Copiar a chave** do painel. É uma string só, nada de OAuth, app, token
      de refresh ou URL de callback.
- [ ] **NÃO colocar crédito ainda.** A conta nasce com cerca de US$ 0,10 de
      crédito de teste, que dá uns 600 tweets. Valide a forma toda do dado com
      esse crédito antes de pagar qualquer coisa.
- [ ] **Não dar conta da X a eles.** Não é necessário, e é o único caminho pelo
      qual o seu perfil pessoal entraria na história.

## Parte 2 — o que fazer na máquina

```bash
cd ~/NABOTE
cp -n .env.example .env

read -rsp "Cole a chave e dê Enter: " K && \
  printf '\nNABOTE_X_PROVIDER=twitterapi_io\nNABOTE_X_API_KEY=%s\n' "$K" >> .env && \
  unset K && echo " — gravado"
```

`read -rs` lê sem imprimir na tela e, o que importa mais, **sem deixar a chave
no histórico do shell** — que é exatamente o que acontece se você a colar dentro
de um comando. As duas linhas equivalentes já existem comentadas no
`.env.example`; as novas vão para o fim do arquivo e são as que valem.

Confira que carregou e que o git não enxerga o arquivo:

```bash
set -a; source .env; set +a
echo "chave: ${#NABOTE_X_API_KEY} caracteres"   # tem que ser > 0
git check-ignore -v .env                         # aponta para a regra do .gitignore
git status --short                               # .env NÃO pode aparecer
```

`${#VAR}` imprime o comprimento, não o conteúdo: confirma que a chave está lá
sem colocá-la na tela.

Se `.env` aparecer no `git status`, pare e me chame antes de commitar
qualquer coisa.

## Parte 3 — validar sem gastar

Com o crédito de teste. O objetivo não é ver dado bonito: é descobrir o formato
exato dos campos aninhados antes de escrever o tradutor.

Um comando. A chave sai do `.env` e nunca aparece na tela nem no histórico
do shell:

```bash
cd ~/NABOTE
set -a; source .env; set +a
curl -s -i -H "X-API-Key: $NABOTE_X_API_KEY" "https://api.twitterapi.io/<CAMINHO>" | head -c 4000
```

| Pedaço | O que faz |
|---|---|
| `set -a; source .env; set +a` | lê o `.env` e deixa a chave disponível como variável, sem imprimir |
| `-s` | silencioso, sem barra de progresso |
| `-i` | inclui os cabeçalhos de resposta na saída, junto com o corpo |
| `-H "X-API-Key: $..."` | manda a chave; o `$` faz o shell substituir, então ela não fica escrita |
| `\| head -c 4000` | corta a saída para não inundar o terminal |

Base e cabeçalho, **confirmados** contra o catálogo oficial deles
(`kaitoInfra/twitterapi-io`, no GitHub — o proxy bloqueia `twitterapi.io` e
`docs.twitterapi.io`, mas o repositório da skill traz o mesmo conteúdo):

```
https://api.twitterapi.io     +     cabeçalho  x-api-key: <chave>
```

Os três endpoints que o NABOTE usa:

| Uso | Caminho e parâmetro |
|---|---|
| perfil → tabela `actor` | `/twitter/user/info?userName=` |
| baseline (timeline de um perfil) | `/twitter/user/last_tweets?userName=` + `cursor`, `includeReplies` |
| campanha (busca por termo) | `/twitter/tweet/advanced_search?query=` + `queryType`, `cursor` |
| saldo da conta | `/oapi/my/info` |

**O nome do parâmetro muda de endpoint para endpoint** e não há regra: é
`userName` num, `user_id` noutro, `username` todo minúsculo num terceiro.
Copie exato do catálogo, nunca normalize.

**O custo pode ser medido, e não estimado.** `/oapi/my/info` devolve
`{recharge_credits, total_bonus_credits}` — o saldo da conta. Lido antes e
depois de um run, a diferença é quanto aquele run gastou de fato, a 100.000
créditos por US$ 1,00. É isso que faz `collection_run.cost_usd` cumprir a regra
do projeto em vez de carregar uma multiplicação de tabela de preço.

**Cuidado com o mínimo por requisição:** US$ 0,00015 são cobrados mesmo quando
a resposta vem vazia. Um teto de gasto que só conta tweets subestima o custo de
uma coleta com muitas páginas vazias.

Antes de colar qualquer saída em qualquer lugar, **passe o olho**: a resposta
não deve conter a chave, mas o cabeçalho de requisição às vezes é ecoado em
mensagem de erro.

## Parte 4 — o que coletar para a implementação

Caminhos, parâmetros, paginação, formato de erro e envelope de resposta vieram
todos do catálogo oficial (acima). **O que o catálogo não traz é o objeto tweet
por dentro** — e é justamente ele que decide a tradução para
`NormalizedEvent`. Isso só sai de uma resposta real.

O que eu preciso ver, e por que:

- **como vêm retuíte, resposta, citação e menção.** É onde a tradução para
  `NormalizedEvent` se decide. O `x_parquet.py` levou três armadilhas neste
  ponto que só apareceram olhando dado real — campos aninhados em `repr` de
  Python em vez de JSON, data sem fuso, e `user` sendo o handle e não o id.
- **se o alvo traz id além do handle.** Se trouxer, ator tier C nasce com nome
  legível no relatório, como acontece com a base do Zenodo. Se não trouxer,
  nasce como número.
- **a forma da paginação.** É o que vai para `source_state`, e é por isso que
  o cursor precisa passar a ser por conta e não por fonte: com o Jetstream você
  lê um firehose só; aqui você pagina a timeline de cada perfil separadamente.
- **a forma do erro** — 401, 429, crédito esgotado. O teto de gasto tem que
  distinguir "acabou o crédito" de "o provedor caiu".

## Parte 5 — higiene da chave

- `.env` está no `.gitignore` (`.env` e `.env.*`, com exceção de
  `.env.example`). Não mexa nessas linhas.
- **Nunca** passe a chave como argumento de linha de comando — argumento
  aparece em `ps` e no histórico do shell. Sempre via variável de ambiente.
- Se a chave vazar: revogue no painel do provedor **primeiro**, gere outra
  depois. O crédito é pré-pago, então o prejuízo máximo é o saldo.
- Se a chave for commitada por acidente, trocar não basta — ela fica no
  histórico do git. Revogue, gere outra, e só então decida se vale reescrever
  o histórico.

Verificação de que nunca vazou:

```bash
git log -p --all -S 'twitterapi' -- . | head -40   # vazio é o resultado bom
```

## Parte 6 — o teto de gasto

```
NABOTE_BUDGET_USD_PER_CYCLE=1.00
```

O teto é aplicado **antes** da chamada, não depois: o run para quando o custo
acumulado do ciclo alcança o limite, e o que ficou de fora é registrado em
`collection_run` em vez de sumir. Orçamento é código, não disciplina — é o
mesmo princípio dos `--max-events` e `--max-seconds` que o `fetch` já tem, onde
hoje o custo é zero.

A US$ 0,15 por mil tweets, US$ 1,00 por ciclo é o teto de 6.666 posts.

## Se o provedor sumir

O procedimento inteiro é:

1. escrever `sources/<novo_provedor>.py` cumprindo o mesmo `Source`;
2. trocar `NABOTE_X_PROVIDER` no `.env`.

Nada fora de `sources/` sabe de onde o dado veio — `ingest`, `graph`, `radar` e
`dossie` só enxergam `NormalizedEvent`. A fixture gravada na Parte 4 continua
valendo como teste de regressão do tradutor antigo.

É a única parte deste documento que não é sobre o provedor atual, e é a que
mais importa.
