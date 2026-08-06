#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testa a maquina de estados/anti-spam SEM rede e SEM disparar notificacoes
de verdade (as funcoes de notificacao sao substituidas por gravadores)."""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import monitor  # noqa: E402

config = monitor.carregar_config()
CHAMADAS = []
monitor.notificar_alerta = lambda t, c: CHAMADAS.append(("alerta", t))
monitor.notificar_manutencao = lambda t, c: CHAMADAS.append(("manutencao", t))
monitor.notificar_vida = lambda t, c: CHAMADAS.append(("vida", t))


def estado_zerado():
    return {"ultimo_estado": None, "ultimo_timestamp": None,
            "inconclusivos_consecutivos": 0, "cnjs_vistos": [],
            "ignorados": [], "ultimo_sinal_de_vida": None,
            "ultima_checagem_ok": None}


def d_proc(cnjs):
    return {"cnjs": [monitor.decodificar_cnj(c) | {"contexto": ""} for c in cnjs],
            "motivo": "teste", "challenge": None}


def d_inc():
    return {"cnjs": [], "motivo": "challenge teste", "challenge": "x"}


falhas = 0


def checa(cond, desc):
    global falhas
    print(f"[{'PASS' if cond else 'FALHA'}] {desc}")
    if not cond:
        falhas += 1


# 1) INCONCLUSIVO: nao alerta nas 2 primeiras, alerta manutencao na 3a.
st = estado_zerado()
CHAMADAS.clear()
monitor.processar(monitor.INCONCLUSIVO, d_inc(), config, st)
monitor.processar(monitor.INCONCLUSIVO, d_inc(), config, st)
checa(not any(k == "manutencao" for k, _ in CHAMADAS),
      "INCONCLUSIVO nao alerta nas 2 primeiras vezes")
monitor.processar(monitor.INCONCLUSIVO, d_inc(), config, st)
checa(any(k == "manutencao" for k, _ in CHAMADAS),
      "INCONCLUSIVO dispara MANUTENCAO na 3a vez consecutiva")

# 2) CNJ novo dispara alerta; mesmo CNJ de novo NAO re-alerta (anti-spam).
st = estado_zerado()
CHAMADAS.clear()
monitor.processar(monitor.PROCESSOS, d_proc(["0801234-56.2023.8.26.0100"]), config, st)
checa(sum(1 for k, _ in CHAMADAS if k == "alerta") == 1, "CNJ novo dispara alerta")
monitor.processar(monitor.PROCESSOS, d_proc(["0801234-56.2023.8.26.0100"]), config, st)
checa(sum(1 for k, _ in CHAMADAS if k == "alerta") == 1,
      "mesmo CNJ na 2a run NAO re-alerta (anti-spam)")

# 3) Um 2o CNJ novo dispara novo alerta.
monitor.processar(monitor.PROCESSOS,
                  d_proc(["0801234-56.2023.8.26.0100", "5001234-89.2024.4.03.6100"]),
                  config, st)
checa(sum(1 for k, _ in CHAMADAS if k == "alerta") == 2,
      "surgindo um 2o CNJ novo, alerta de novo")

# 4) Homonimo em 'ignorados' nunca alerta.
st = estado_zerado()
st["ignorados"] = ["0801234-56.2023.8.26.0100"]
CHAMADAS.clear()
monitor.processar(monitor.PROCESSOS, d_proc(["0801234-56.2023.8.26.0100"]), config, st)
checa(not any(k == "alerta" for k, _ in CHAMADAS),
      "CNJ em 'ignorados' (homonimo) nunca alerta")

# 5) LIMPO nunca notifica (nem 1a vez, nem apos PROCESSOS) — so alerta
# quando ha processo encontrado.
st = estado_zerado()
CHAMADAS.clear()
monitor.processar(monitor.LIMPO, {"cnjs": [], "motivo": "limpo", "challenge": None}, config, st)
checa(not CHAMADAS, "LIMPO nunca notifica (nem sinal de vida)")
monitor.processar(monitor.PROCESSOS, d_proc(["0801234-56.2023.8.26.0100"]), config, st)
CHAMADAS.clear()
monitor.processar(monitor.LIMPO, {"cnjs": [], "motivo": "limpo", "challenge": None}, config, st)
checa(not CHAMADAS, "transicao PROCESSOS -> LIMPO tambem nao notifica")

print()
print("RESULTADO:", "TODOS OS TESTES PASSARAM" if not falhas else f"{falhas} FALHA(S)")
sys.exit(1 if falhas else 0)
