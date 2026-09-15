-- E-I sozinho não é interpretável: ele depende do TAMANHO da comunidade.
--
-- Comunidade maior captura mais arestas dentro de si por construção, então
-- parece mais fechada mesmo sem nenhuma diferença de comportamento. No dado
-- real da base histórica do X, tamanho e E-I correlacionam −0,73 DENTRO de uma
-- única janela — ou seja, nem é efeito de densidade de coleta, é efeito do
-- tamanho. Comparar o E-I de duas comunidades de tamanhos diferentes, sem
-- correção, compara principalmente os tamanhos delas.
--
-- A correção é um modelo nulo: embaralhar as arestas preservando o grau de cada
-- nó e a partição, e medir quanto do fechamento observado sobra acima do que o
-- acaso já produziria. `ei_z` é esse excedente em desvios-padrão.
--
--   ei_z ≈ 0    fechamento igual ao do acaso — não há achado
--   ei_z ≪ 0    fechada ALÉM do que o tamanho explica — câmara de eco de fato
--
-- Colunas nulas quando o modelo nulo não foi rodado (`analyze --null 0`).
ALTER TABLE community ADD COLUMN ei_null REAL;
ALTER TABLE community ADD COLUMN ei_z    REAL;
ALTER TABLE community ADD COLUMN null_trials INTEGER;
