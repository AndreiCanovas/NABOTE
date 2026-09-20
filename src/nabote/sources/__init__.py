"""Fontes de eventos.

A abstração existe por uma razão concreta de risco (plano, frente 09): o
agregador de X que o projeto vai usar opera em zona cinzenta e pode sumir sem
aviso. Trocar de provedor tem de caber numa tarde, e isso só é verdade se
nada fora deste pacote souber de onde o dado veio.
"""

from .base import Source
from .fixture import FixtureSource
from .jetstream import JetstreamSource
from .x_api import XApiSource
from .x_parquet import XParquetSource

__all__ = ["Source", "FixtureSource", "JetstreamSource", "XApiSource",
           "XParquetSource"]
