-- Substitui o modelo nulo do E-I, que não funcionou, pelo conserto que funciona.
--
-- O QUE QUEBROU. A migração 003 introduziu um z-score contra um grafo
-- embaralhado. No dado real ele saiu inútil: z variando de −7,5 a +26,1, e só
-- 38 de 1009 comunidades recebendo valor. A causa é estrutural, não de ajuste —
-- a rede tem grau médio ~2, e num grafo tão esparso o Leiden acha, no
-- embaralhado, uma partição com E-I −1,0000 exato e desvio 0,00000. O nulo
-- satura, o z ou não existe (desvio zero) ou explode (desvio ~1e-5).
--
-- O QUE ERA O PROBLEMA DE VERDADE. O E-I estava sendo calculado como média por
-- nó sobre TODOS os atores. Numa rede de audiência-em-torno-de-hub, a maioria
-- amplificou uma vez só: grau 1, E-I −1 obrigatório. Essa gente não escolheu
-- ficar dentro do grupo — não houve segunda oportunidade de atravessar. A média
-- tende a −1 conforme a audiência cresce, e é daí que vinha a correlação de
-- −0,73 entre tamanho e E-I.
--
-- O CONSERTO. `ei_choice` é a média do E-I apenas entre atores com força ≥ 2,
-- isto é, quem teve mais de uma chance de atravessar. Em teste com audiências
-- de 100 a 4.000 e comportamento relativo idêntico, a correlação com o tamanho
-- cai de −0,63 para −0,21 e os valores se agrupam em torno de −0,82 em vez de
-- todos virarem −1,00.
--
-- `choice_actors` guarda quantos atores entraram na conta: comunidade onde
-- quase ninguém teve escolha tem `ei_choice` frágil, e isso precisa ser visível.
ALTER TABLE community DROP COLUMN ei_null;
ALTER TABLE community DROP COLUMN ei_z;
ALTER TABLE community DROP COLUMN null_trials;
ALTER TABLE community ADD COLUMN ei_choice    REAL;
ALTER TABLE community ADD COLUMN choice_actors INTEGER;
