"""wxbot — Polymarket daily-temperature trading toolkit.

Reverse-engineered from a profitable temperature trader (0xB901..09fc4):
buy the meteorologically-favored high-temperature bucket cheaply and harvest
its convergence to $1.00, concentrating on low-variance / thinly-contested
cities and exploiting the intraday collapse of uncertainty as the day's high
becomes observable on the resolution station.

Pipeline: clients (Polymarket + weather) -> parse -> model -> backtest / scan
-> execution (paper by default).
"""
