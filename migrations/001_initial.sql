-- =============================================================================
-- NABOTE — schema inicial
--
-- Invariantes que este schema existe para proteger (ver plano, frente 03):
--   1. ATOR É AGNÓSTICO DE PLATAFORMA. A chave natural é (platform,
--      platform_user_id) — nunca o handle, que muda. É isso que permite Bluesky
--      e X coexistirem e outra plataforma entrar depois sem migração.
--   2. POST É APPEND-ONLY. `interaction` é DERIVADA de `post` e sempre
--      recomputável. Nunca transforme destrutivamente.
--   3. JANELA E ESCOPO EM TUDO QUE É ANALÍTICO. `window_start` dá evolução
--      temporal; `scope` permite que o mesmo ator tenha métricas diferentes no
--      grafo global e dentro de cada tema. Faltando um dos dois, é rewrite.
--   4. PAYLOAD BRUTO É ARQUIVO. Post apagado não volta.
--   5. CUSTO E INTENSIDADE SÃO COLUNAS. `cost_usd` decide upgrade de tier;
--      `kind` separa baseline de campanha — sem ele você não distingue mudança
--      no discurso de mudança na sua própria intensidade de coleta.
--
-- Convenções:
--   - Timestamps: TEXT ISO-8601 UTC ('2026-09-22T06:14:00Z')
--   - Datas / janelas: TEXT 'YYYY-MM-DD' (segunda-feira da janela)
--   - Booleanos: INTEGER 0/1 com CHECK
--   - Tabelas STRICT: o tipo declarado é aplicado de verdade (SQLite >= 3.37)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- ATORES
-- -----------------------------------------------------------------------------
CREATE TABLE actor (
  actor_id             INTEGER PRIMARY KEY,
  platform             TEXT    NOT NULL,
  -- id estável da plataforma. NÃO é o handle: handle muda, id não.
  platform_user_id     TEXT    NOT NULL,
  handle               TEXT,
  display_name         TEXT,
  bio                  TEXT,
  account_created_at   TEXT,
  -- A = coleta semanal, B = mensal, C = nunca coletado (só alvo de aresta).
  -- Tier C é o que permite medir influência sem pagar para coletar o influente.
  tier                 TEXT    NOT NULL DEFAULT 'C',
  -- Fronteira de publicação (frente 09): escore individual só sai em
  -- entregável para figura pública, por critério objetivo e registrado.
  is_public_figure     INTEGER NOT NULL DEFAULT 0,
  public_figure_reason TEXT,
  first_seen_at        TEXT    NOT NULL,
  last_seen_at         TEXT    NOT NULL,
  UNIQUE (platform, platform_user_id),
  CHECK (tier IN ('A','B','C')),
  CHECK (is_public_figure IN (0,1)),
  CHECK (is_public_figure = 0 OR public_figure_reason IS NOT NULL)
) STRICT;

CREATE INDEX idx_actor_handle ON actor (platform, handle);
CREATE INDEX idx_actor_tier   ON actor (tier);

CREATE TABLE actor_snapshot (
  actor_id        INTEGER NOT NULL REFERENCES actor(actor_id) ON DELETE CASCADE,
  snapshot_date   TEXT    NOT NULL,
  followers_count INTEGER,
  following_count INTEGER,
  posts_count     INTEGER,
  PRIMARY KEY (actor_id, snapshot_date)
) STRICT;

-- -----------------------------------------------------------------------------
-- PROCEDÊNCIA E CUSTO
-- -----------------------------------------------------------------------------
CREATE TABLE collection_run (
  run_id         INTEGER PRIMARY KEY,
  source         TEXT    NOT NULL,
  -- baseline = fino e contínuo, mantém a série viva, nunca desliga.
  -- campanha  = profundo e temporário, responde a uma pergunta concreta.
  kind           TEXT    NOT NULL,
  campaign_label TEXT,
  query          TEXT,
  started_at     TEXT    NOT NULL,
  ended_at       TEXT,
  items_fetched  INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL    NOT NULL DEFAULT 0.0,
  status         TEXT    NOT NULL DEFAULT 'running',
  error          TEXT,
  CHECK (kind IN ('baseline','campanha')),
  CHECK (status IN ('running','ok','partial','failed')),
  -- campanha sem rótulo é dado que você não consegue interpretar depois
  CHECK (kind <> 'campanha' OR campaign_label IS NOT NULL),
  CHECK (cost_usd >= 0)
) STRICT;

CREATE INDEX idx_run_started ON collection_run (started_at);

-- Arquivo imutável. Gravado ANTES de qualquer parsing: se o parser quebrar,
-- o dado já está salvo e o reprocessamento não custa nada.
CREATE TABLE raw_payload (
  run_id           INTEGER NOT NULL REFERENCES collection_run(run_id) ON DELETE CASCADE,
  platform         TEXT    NOT NULL,
  platform_post_id TEXT    NOT NULL,
  fetched_at       TEXT    NOT NULL,
  payload_gz       BLOB    NOT NULL,
  PRIMARY KEY (run_id, platform, platform_post_id)
) STRICT;

-- -----------------------------------------------------------------------------
-- CONTEÚDO
-- -----------------------------------------------------------------------------
CREATE TABLE post (
  post_id                 INTEGER PRIMARY KEY,
  platform                TEXT    NOT NULL,
  platform_post_id        TEXT    NOT NULL,
  actor_id                INTEGER NOT NULL REFERENCES actor(actor_id),
  created_at              TEXT    NOT NULL,
  lang                    TEXT,
  text                    TEXT,
  post_type               TEXT    NOT NULL,
  -- parent_post_id fica NULL quando o post-alvo não foi coletado — o que é o
  -- caso normal. parent_actor_id ainda assim é conhecido, e é exatamente por
  -- isso que um ator Tier C aparece no grafo sem nunca ter sido coletado.
  parent_post_id          INTEGER REFERENCES post(post_id),
  parent_platform_post_id TEXT,
  parent_actor_id         INTEGER REFERENCES actor(actor_id),
  like_count              INTEGER,
  repost_count            INTEGER,
  reply_count             INTEGER,
  collected_at            TEXT    NOT NULL,
  run_id                  INTEGER NOT NULL REFERENCES collection_run(run_id),
  -- torna a ingestão idempotente: reprocessar não duplica
  UNIQUE (platform, platform_post_id),
  CHECK (post_type IN ('original','repost','reply','quote'))
) STRICT;

CREATE INDEX idx_post_actor_time ON post (actor_id, created_at);
CREATE INDEX idx_post_created    ON post (created_at);
CREATE INDEX idx_post_run        ON post (run_id);
CREATE INDEX idx_post_parent     ON post (parent_actor_id);

-- -----------------------------------------------------------------------------
-- ARESTAS — derivadas de post, sempre recomputáveis
-- -----------------------------------------------------------------------------
CREATE TABLE interaction (
  interaction_id INTEGER PRIMARY KEY,
  post_id        INTEGER NOT NULL REFERENCES post(post_id) ON DELETE CASCADE,
  src_actor_id   INTEGER NOT NULL REFERENCES actor(actor_id),
  dst_actor_id   INTEGER NOT NULL REFERENCES actor(actor_id),
  kind           TEXT    NOT NULL,
  occurred_at    TEXT    NOT NULL,
  UNIQUE (post_id, kind, dst_actor_id),
  CHECK (kind IN ('repost','reply','quote','mention')),
  -- autointeração polui todas as métricas de centralidade
  CHECK (src_actor_id <> dst_actor_id)
) STRICT;

CREATE INDEX idx_int_dst  ON interaction (dst_actor_id, kind);
CREATE INDEX idx_int_src  ON interaction (src_actor_id, kind);
CREATE INDEX idx_int_time ON interaction (occurred_at);

-- Agregado por janela. É ISTO que alimenta o igraph.
-- scope='global' é o grafo estrutural; 'topic:<id>' é o recorte temático.
CREATE TABLE edge_window (
  window_start TEXT    NOT NULL,
  scope        TEXT    NOT NULL DEFAULT 'global',
  src_actor_id INTEGER NOT NULL REFERENCES actor(actor_id),
  dst_actor_id INTEGER NOT NULL REFERENCES actor(actor_id),
  kind         TEXT    NOT NULL,
  weight       REAL    NOT NULL,
  PRIMARY KEY (window_start, scope, src_actor_id, dst_actor_id, kind),
  CHECK (kind IN ('repost','reply','quote','mention')),
  CHECK (weight > 0)
) STRICT;

CREATE INDEX idx_edgew_dst ON edge_window (window_start, scope, dst_actor_id);

-- -----------------------------------------------------------------------------
-- CLASSIFICAÇÃO
-- -----------------------------------------------------------------------------
-- NUNCA renumere tópico existente. Reajuste de clustering = nova `version`,
-- mantendo a antiga. Renumerar quebra a série temporal em silêncio.
CREATE TABLE topic (
  topic_id    INTEGER PRIMARY KEY,
  label       TEXT    NOT NULL,
  description TEXT,
  method      TEXT    NOT NULL,
  version     INTEGER NOT NULL,
  created_at  TEXT    NOT NULL,
  UNIQUE (label, version)
) STRICT;

CREATE TABLE post_topic (
  post_id        INTEGER NOT NULL REFERENCES post(post_id) ON DELETE CASCADE,
  topic_id       INTEGER NOT NULL REFERENCES topic(topic_id),
  score          REAL    NOT NULL,
  method_version INTEGER NOT NULL,
  PRIMARY KEY (post_id, topic_id, method_version)
) STRICT;

CREATE INDEX idx_post_topic_topic ON post_topic (topic_id, method_version);

CREATE TABLE actor_topic_window (
  actor_id     INTEGER NOT NULL REFERENCES actor(actor_id),
  topic_id     INTEGER NOT NULL REFERENCES topic(topic_id),
  window_start TEXT    NOT NULL,
  share        REAL    NOT NULL,
  volume       INTEGER NOT NULL,
  PRIMARY KEY (actor_id, topic_id, window_start)
) STRICT;

-- O nível 1 do plano: o que cada comunidade discute.
-- `lift` = P(tópico|comunidade) / P(tópico) — separa "eles falam disso" de
-- "só eles falam disso". `top2_share` distingue pauta de comunidade de duas
-- contas martelando.
CREATE TABLE topic_community_window (
  topic_id          INTEGER NOT NULL REFERENCES topic(topic_id),
  community_id      INTEGER NOT NULL,
  window_start      TEXT    NOT NULL,
  scope             TEXT    NOT NULL DEFAULT 'global',
  share             REAL    NOT NULL,
  lift              REAL,
  volume            INTEGER NOT NULL,
  top2_share        REAL,
  first_seen_window TEXT,
  PRIMARY KEY (topic_id, community_id, window_start, scope)
) STRICT;

-- Posicionamento vem do grafo (análise de correspondência sobre a matriz de
-- retuítes), não do texto. `method` registra qual, para não comparar escalas
-- incompatíveis entre campanhas.
CREATE TABLE actor_position (
  actor_id     INTEGER NOT NULL REFERENCES actor(actor_id),
  axis         TEXT    NOT NULL,
  window_start TEXT    NOT NULL,
  scope        TEXT    NOT NULL DEFAULT 'global',
  score        REAL    NOT NULL,
  ci_low       REAL,
  ci_high      REAL,
  method       TEXT    NOT NULL,
  is_anchor    INTEGER NOT NULL DEFAULT 0,
  computed_at  TEXT    NOT NULL,
  PRIMARY KEY (actor_id, axis, window_start, scope),
  CHECK (is_anchor IN (0,1))
) STRICT;

-- -----------------------------------------------------------------------------
-- GRAFO
-- -----------------------------------------------------------------------------
CREATE TABLE actor_metric (
  actor_id      INTEGER NOT NULL REFERENCES actor(actor_id),
  window_start  TEXT    NOT NULL,
  scope         TEXT    NOT NULL DEFAULT 'global',
  metric        TEXT    NOT NULL,
  value         REAL    NOT NULL,
  graph_version INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (actor_id, window_start, scope, metric, graph_version)
) STRICT;

-- suporta "top N por métrica nesta janela e escopo", que é a query mais comum
CREATE INDEX idx_metric_lookup ON actor_metric (window_start, scope, metric, value DESC);

CREATE TABLE community (
  window_start  TEXT    NOT NULL,
  scope         TEXT    NOT NULL DEFAULT 'global',
  community_id  INTEGER NOT NULL,
  graph_version INTEGER NOT NULL DEFAULT 1,
  label         TEXT,
  size          INTEGER NOT NULL,
  ei_mean       REAL,
  PRIMARY KEY (window_start, scope, community_id, graph_version)
) STRICT;

-- um ator pertence a exatamente uma comunidade por (janela, escopo, versão)
CREATE TABLE actor_community (
  actor_id      INTEGER NOT NULL REFERENCES actor(actor_id),
  window_start  TEXT    NOT NULL,
  scope         TEXT    NOT NULL DEFAULT 'global',
  community_id  INTEGER NOT NULL,
  graph_version INTEGER NOT NULL DEFAULT 1,
  membership    REAL,
  PRIMARY KEY (actor_id, window_start, scope, graph_version)
) STRICT;

CREATE INDEX idx_actor_comm ON actor_community (window_start, scope, community_id);

-- -----------------------------------------------------------------------------
-- FRONTEIRA DE PUBLICAÇÃO (frente 09)
-- Responde "de onde veio esse número" seis meses depois, e sustenta qualquer
-- contestação. O filtro em si vive no código de exportação; aqui fica o registro.
-- -----------------------------------------------------------------------------
CREATE TABLE export_log (
  export_id                  INTEGER PRIMARY KEY,
  exported_at                TEXT    NOT NULL,
  report_kind                TEXT    NOT NULL,
  scope                      TEXT    NOT NULL,
  window_start               TEXT    NOT NULL,
  destination                TEXT,
  includes_individual_scores INTEGER NOT NULL DEFAULT 0,
  actor_count                INTEGER,
  notes                      TEXT,
  CHECK (report_kind IN ('radar','dossie')),
  CHECK (includes_individual_scores IN (0,1))
) STRICT;

-- -----------------------------------------------------------------------------
-- VIEWS de conveniência para o notebook (frente 08)
-- -----------------------------------------------------------------------------
CREATE VIEW v_actor_current AS
SELECT a.actor_id, a.platform, a.handle, a.display_name, a.tier,
       a.is_public_figure, s.snapshot_date, s.followers_count, s.following_count
FROM actor a
LEFT JOIN actor_snapshot s
  ON s.actor_id = a.actor_id
 AND s.snapshot_date = (SELECT MAX(snapshot_date) FROM actor_snapshot
                        WHERE actor_id = a.actor_id);

CREATE VIEW v_top_atores AS
SELECT m.window_start, m.scope, m.metric, m.value,
       a.actor_id, a.handle, a.tier, a.is_public_figure,
       c.community_id
FROM actor_metric m
JOIN actor a ON a.actor_id = m.actor_id
LEFT JOIN actor_community c
  ON c.actor_id = m.actor_id
 AND c.window_start = m.window_start
 AND c.scope = m.scope
ORDER BY m.value DESC;

CREATE VIEW v_arestas_janela AS
SELECT e.window_start, e.scope, e.kind, e.weight,
       s.handle AS src_handle, d.handle AS dst_handle,
       e.src_actor_id, e.dst_actor_id
FROM edge_window e
JOIN actor s ON s.actor_id = e.src_actor_id
JOIN actor d ON d.actor_id = e.dst_actor_id;

CREATE VIEW v_custo_por_run AS
SELECT kind, campaign_label, status,
       COUNT(*) AS runs,
       SUM(items_fetched) AS items,
       ROUND(SUM(cost_usd), 4) AS cost_usd
FROM collection_run
GROUP BY kind, campaign_label, status;
