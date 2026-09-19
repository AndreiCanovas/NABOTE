-- =============================================================================
-- 006 — recorte da coleta e cursor por conta
--
-- As duas mudanças que a coleta por API exige e o firehose não exigia. Elas
-- vêm juntas porque têm a mesma causa: ler um firehose é passivo, e ler uma
-- API é escolher.
--
-- RECORTE (`collection_run.frame`). A tabela registra o que um run FEZ —
-- quantos itens, quanto custou, se falhou. Não registra o que ele SAIU PARA
-- FAZER. No firehose isso não faz falta: você cobre o que passou, e o cursor
-- já diz de onde até onde. Na coleta por conta faz toda a diferença — dois
-- runs com o mesmo `items_fetched` podem ser 150 perfis rasos ou 10 perfis
-- fundos, e a leitura a jusante muda por inteiro.
--
-- E há o caso que obriga: quando o teto de gasto corta o run no meio, o que
-- ficou de fora precisa estar escrito. Sem isso, "16.290 atores na janela" é
-- um número sem denominador — não dá para saber se cobriu 150 de 150 sementes
-- ou 90 de 150. A regra do projeto é que número em relatório é medido, e
-- número cuja cobertura é desconhecida não é medido: é encontrado.
--
-- POR QUE JSON E NÃO COLUNAS. Cada fonte recorta o mundo no vocabulário dela:
-- firehose tem cursor de tempo, coleta por conta tem lista de contas e
-- profundidade, busca tem termo e janela de data. Colunas discretas seriam
-- quase todas NULL, e a próxima fonte pediria mais duas. O recorte é da
-- fonte, e `sources/` existe justamente para que o pipeline não saiba de onde
-- o dado veio.
--
-- POR QUE SEM CHECK. Todo invariante deste schema é um CHECK, e a ausência
-- aqui é decisão, não esquecimento: `json_valid()` só está disponível por
-- padrão a partir do SQLite 3.38, e `db.MIN_SQLITE` é 3.37 — um CHECK que
-- referencia a função quebraria a ABERTURA do banco, não só a escrita, numa
-- versão que o projeto diz aceitar. A validação fica na fronteira Python, que
-- é a única coisa que escreve a coluna: `start_run` e `finish_run` recebem
-- dict e serializam, então JSON inválido não nasce pelo caminho normal.
--
-- CURSOR POR CONTA. `source_state` tinha `source` como chave primária, porque
-- com o Jetstream existe um cursor só: um WebSocket, uma posição. Lendo uma
-- API você pagina a timeline de CADA perfil separadamente, e "onde eu parei"
-- passa a ser uma resposta por conta. Com a chave antiga, o segundo perfil
-- sobrescreveria o cursor do primeiro em silêncio, e a retomada voltaria a
-- coletar de novo o que já tinha sido pago.
--
-- SQLite não altera chave primária no lugar, então a tabela é reconstruída.
-- É seguro sem desligar `foreign_keys`: nada referencia `source_state`.
-- =============================================================================

ALTER TABLE collection_run ADD COLUMN frame TEXT;

CREATE TABLE source_state_novo (
  source     TEXT NOT NULL,
  -- '' = a fonte tem um cursor só (firehose). NÃO é NULL: NULL não se compara
  -- por igualdade, então não casaria no ON CONFLICT do upsert, e cada gravação
  -- inseriria uma linha nova em vez de atualizar a que existe.
  account    TEXT NOT NULL DEFAULT '',
  cursor     TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (source, account)
) STRICT;

INSERT INTO source_state_novo (source, account, cursor, updated_at)
SELECT source, '', cursor, updated_at FROM source_state;

DROP TABLE source_state;

ALTER TABLE source_state_novo RENAME TO source_state;
