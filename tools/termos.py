"""Quais termos existem em quantas semanas — direto do zip, sem carregar nada."""
import sys, collections, datetime
sys.path.insert(0, "src")
from nabote.sources.x_parquet import members_of, parse_member_name

def segunda(d):
    x = datetime.date.fromisoformat(d)
    return (x - datetime.timedelta(days=x.weekday())).isoformat()

sem = collections.defaultdict(set)
for m in members_of(sys.argv[1]):
    d, t = parse_member_name(m)
    if d:
        sem[segunda(d)].add(t)

por_termo = collections.defaultdict(list)
for w, ts in sem.items():
    for t in ts:
        por_termo[t].append(w)

print(f"{len(sem)} semanas · {sum(len(v) for v in sem.values())} pares (semana, termo)\n")
print("TERMOS EM 2+ SEMANAS")
print(f"{'sem':>3}  {'termo':<32}  janelas")
print("-" * 92)
for t, ws in sorted(por_termo.items(), key=lambda x: (-len(x[1]), x[0])):
    if len(ws) >= 2:
        print(f"{len(ws):>3}  {t:<32}  {' '.join(sorted(set(ws)))}")
unicos = sorted(t for t, v in por_termo.items() if len(v) == 1)
print(f"\n{len(unicos)} termos aparecem numa semana só.")

# Variações da mesma pauta escondidas entre os únicos: na base real
# "CPMI do Golpe" aparece numa semana só e por isso não entrava na lista acima,
# embora seja a MESMA pauta de "CPMI" e "#CPMIdoGolpe". Sem este bloco, um
# rótulo da pauta fica de fora do escopo e o volume da semana sai menor.
def chave(t):
    return "".join(c for c in t.lower() if c.isalnum())

fam = collections.defaultdict(list)
for t in por_termo:
    fam[chave(t)].append(t)
prefixo = collections.defaultdict(set)
for k, ts in fam.items():
    for outro in fam:
        if outro != k and (k.startswith(outro) or outro.startswith(k)):
            prefixo[min(k, outro)].update(fam[k] + fam[outro])
if prefixo:
    print("\nPOSSÍVEIS VARIAÇÕES DA MESMA PAUTA (conferir à mão)")
    for _, ts in sorted(prefixo.items()):
        semanas = sorted({w for t in ts for w in por_termo[t]})
        print(f"  {len(semanas):>2} semanas  " + " · ".join(sorted(ts)))
        print(f"            {' '.join(semanas)}")
