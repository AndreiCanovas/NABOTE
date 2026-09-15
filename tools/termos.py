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
print(f"\n{sum(1 for v in por_termo.values() if len(v)==1)} termos aparecem numa semana só.")
