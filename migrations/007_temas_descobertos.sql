-- =============================================================================
-- 007 — tema descoberto no texto, confirmado por curadoria
--
-- Até aqui "pauta" era `collection_run.campaign_label`: o Trending Topic que
-- originou o arquivo de 2023. Funcionava porque naquele arquivo o assunto ERA
-- o critério de coleta. Lendo a timeline das sementes não existe critério
-- nenhum — 529 posts chegam sem rótulo, e o radar fica mudo.
--
-- O tema passa a sair do texto. O caminho é: termo -> coocorrência -> grupo ->
-- tema. E o grupo vira tema quando UMA PESSOA confirma, o que não é burocracia:
--
-- POR QUE A CONFIRMAÇÃO É O EIXO. Agrupamento não devolve os mesmos grupos em
-- duas rodadas. Sem um passo humano, "CPMI subiu 40%" compararia o grupo 3
-- desta semana com o grupo 7 da anterior — números que não falam da mesma
-- coisa. Confirmar é o que dá IDENTIDADE ESTÁVEL ao tema: a partir daí ele tem
-- um id, um nome que você escolheu, e um conjunto de termos que casa os posts
-- das próximas semanas. O dicionário existe, mas ninguém o escreve à mão: ele
-- é o rastro das confirmações.
--
-- E o que NÃO casou com tema confirmado é o material de onde sai a proposta
-- seguinte. É ali que pauta nova aparece — que era o buraco de um léxico
-- curado, onde assunto novo simplesmente não existe até alguém digitá-lo.
--
-- `terms` é JSON e não tabela filha por uma razão de uso: o conjunto de termos
-- é lido e escrito sempre inteiro, nunca consultado por termo isolado. Tabela
-- filha aqui seria normalizar o que nunca se consulta separado. (Sem CHECK de
-- json_valid pelo mesmo motivo da 006: a função só vem por padrão a partir do
-- SQLite 3.38 e `db.MIN_SQLITE` é 3.37 — um CHECK quebraria a ABERTURA do
-- banco. A validação fica na fronteira Python, que é a única que escreve.)
-- =============================================================================

ALTER TABLE topic ADD COLUMN terms TEXT;

-- proposto   = saiu do agrupamento, ninguém olhou ainda
-- confirmado = tem nome de gente e casa posts das próximas janelas
-- descartado = olhado e recusado; fica no banco para não ser reproposto
ALTER TABLE topic ADD COLUMN status TEXT NOT NULL DEFAULT 'proposto';

-- A janela em que o tema foi proposto. Responde "desde quando este assunto
-- existe para nós", que é diferente de quando ele apareceu no mundo.
ALTER TABLE topic ADD COLUMN window_start TEXT;

CREATE INDEX idx_topic_status ON topic (status, method, version);
