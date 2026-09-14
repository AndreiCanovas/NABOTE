"""Inspeção de bases externas antes de escrever adaptador.

Escrever um parser contra um formato que você não viu é a forma mais confiável
de perder um dia. Este módulo existe para que a primeira pergunta sobre
qualquer base nova — quais colunas, quais tipos, o que vem preenchido — tenha
resposta em um comando.

Lê parquet de dentro de um .zip sem descompactar: os arquivos do dataset
brasileiro têm de 0,4 a 23 MB cada, e não faz sentido expandir 2,5 GB no disco
só para olhar o cabeçalho.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any


def list_zip_members(path: Path | str, suffix: str = ".parquet") -> list[tuple[str, int]]:
    """(nome, tamanho descomprimido) de cada membro, em ordem."""
    with zipfile.ZipFile(path) as zf:
        return sorted(
            ((i.filename, i.file_size) for i in zf.infolist()
             if i.filename.endswith(suffix) and not i.is_dir()),
            key=lambda pair: pair[0],
        )


def read_parquet_member(path: Path | str, member: str):
    """Carrega um membro do zip em memória e devolve a tabela pyarrow."""
    import pyarrow.parquet as pq

    with zipfile.ZipFile(path) as zf:
        with zf.open(member) as handle:
            return pq.read_table(io.BytesIO(handle.read()))


def read_parquet_file(path: Path | str):
    import pyarrow.parquet as pq

    return pq.read_table(path)


def _short(value: Any, width: int = 62) -> str:
    text = "∅" if value is None else str(value).replace("\n", "⏎")
    return text if len(text) <= width else text[: width - 1] + "…"


def describe(table, sample_rows: int = 3) -> str:
    """Relatório de esquema + amostra, pensado para ser colado numa conversa."""
    linhas: list[str] = []
    total = table.num_rows
    linhas.append(f"linhas: {total:,}".replace(",", ".") + f"   colunas: {table.num_columns}")
    linhas.append("")
    linhas.append(f"{'coluna':<28}{'tipo':<26}{'nulos':>10}  exemplo")
    linhas.append("-" * 104)

    for name in table.schema.names:
        coluna = table.column(name)
        nulos = coluna.null_count
        pct = f"{nulos * 100 // total}%" if total else "—"
        exemplo = None
        for i in range(min(total, 50)):
            valor = coluna[i].as_py()
            if valor is not None:
                exemplo = valor
                break
        tipo = str(table.schema.field(name).type)
        linhas.append(f"{name[:27]:<28}{tipo[:25]:<26}{pct:>10}  {_short(exemplo)}")

    if sample_rows and total:
        linhas.append("")
        linhas.append(f"primeiras {min(sample_rows, total)} linhas completas:")
        for i in range(min(sample_rows, total)):
            linhas.append("")
            linhas.append(f"  --- linha {i} ---")
            for name in table.schema.names:
                linhas.append(f"  {name[:26]:<27} {_short(table.column(name)[i].as_py(), 72)}")

    return "\n".join(linhas)
