-- =============================================================================
-- 002 — exclusões honradas e retomada de coleta
--
-- EXCLUSÕES. O AT Protocol é descentralizado: apagar um post no Bluesky não
-- remove cópias que terceiros já baixaram. As Diretrizes de Desenvolvedor
-- exigem que quem guardou apague, e a LGPD reforça. Mas o firehose emite o
-- evento de exclusão, então dá para honrar de verdade.
--
-- A regra adotada (opção conservadora; ver README):
--   - o CONTEÚDO sai: `text` vira NULL e a linha de `raw_payload` é removida;
--   - a ARESTA fica, com `deleted_at` preenchido. A interação é um fato de
--     rede com data, não a expressão do usuário — e sem ela a série temporal
--     de uma janela já fechada passaria a mentir retroativamente.
--
-- Ampliar isso para reter conteúdo de figura pública é uma condição no código
-- de ingestão, não uma mudança de schema: `actor.is_public_figure` já existe.
--
-- CURSOR. `time_us` do último evento processado. É o que permite `fetch`
-- retomar de onde parou depois de uma queda, em vez de perder a janela.
-- =============================================================================

ALTER TABLE post ADD COLUMN deleted_at TEXT;

CREATE INDEX idx_post_deleted ON post (deleted_at) WHERE deleted_at IS NOT NULL;

CREATE TABLE source_state (
  source     TEXT PRIMARY KEY,
  cursor     TEXT,
  updated_at TEXT NOT NULL
) STRICT;

-- Posts vivos: o recorte padrão de quase toda análise.
CREATE VIEW v_post_ativo AS
SELECT * FROM post WHERE deleted_at IS NULL;
