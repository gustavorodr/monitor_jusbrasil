#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testa a funcao pura classificar() contra fixtures em disco (sem rede)."""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import monitor  # noqa: E402

FIX = os.path.join(BASE, "fixtures")
config_geral = monitor.carregar_config()
config = monitor.config_da_fonte(config_geral, monitor.obter_fonte(config_geral, "jusbrasil"))

CASOS = [
    ("limpo.html", monitor.LIMPO),                # sentinela visivel
    ("limpo_json.html", monitor.LIMPO),           # totalLawsuits=0 (pagina real headless)
    ("com_processo.html", monitor.PROCESSOS),     # CNJ no DOM
    ("processo_json.html", monitor.PROCESSOS),    # totalLawsuits>0 + CNJ
    ("challenge.html", monitor.INCONCLUSIVO),     # bloqueio Cloudflare real
]


def ler(nome):
    with open(os.path.join(FIX, nome), encoding="utf-8") as f:
        return f.read()


def main():
    falhas = 0

    for nome, esperado in CASOS:
        estado, dados = monitor.classificar(ler(nome), config)
        ok = estado == esperado
        falhas += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FALHA'}] {nome:20s} esperado={esperado:22s} "
              f"obtido={estado:22s}  ({dados['motivo']})")
        if nome == "com_processo.html":
            assert len(dados["cnjs"]) == 2, "deveria extrair 2 CNJs"
            cnjs = {c["cnj"] for c in dados["cnjs"]}
            assert "0801234-56.2023.8.26.0100" in cnjs
            assert "5001234-89.2024.4.03.6100" in cnjs
            seg = {c["cnj"]: c["segmento"] for c in dados["cnjs"]}
            assert seg["0801234-56.2023.8.26.0100"] == "Justica Estadual"
            assert seg["5001234-89.2024.4.03.6100"] == "Justica Federal"
            print("       -> 2 CNJs, segmentos decodificados corretamente")

    # Caso extra: layout mudou (sem CNJ, sem sentinela) -> INCONCLUSIVO,
    # e NAO LIMPO. Este e o bug de falha silenciosa que estamos evitando.
    html_estranho = "<html><body><h1>Pagina reformulada</h1><p>Nada aqui.</p></body></html>"
    estado, dados = monitor.classificar(html_estranho, config)
    ok = estado == monitor.INCONCLUSIVO
    falhas += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FALHA'}] {'layout_mudou':20s} "
          f"esperado={monitor.INCONCLUSIVO:22s} obtido={estado:22s}  ({dados['motivo']})")

    # Caso extra: 403 = html vazio -> INCONCLUSIVO, nunca LIMPO
    estado, _ = monitor.classificar("", config)
    ok = estado == monitor.INCONCLUSIVO
    falhas += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FALHA'}] {'html_vazio(403)':20s} "
          f"esperado={monitor.INCONCLUSIVO:22s} obtido={estado:22s}")

    # Regressao: 'challenge-platform' aparece em paginas LEGITIMAS do Cloudflare.
    # Numa pagina com dados reais (totalLawsuits) NAO pode ser tratado como challenge.
    html_cf_legit = ('<html><body>' + ('<p>conteudo real da pagina. </p>' * 60) +
                     '<script src="/cdn-cgi/challenge-platform/v1"></script>'
                     '<script id="__NEXT_DATA__">{"totalLawsuits":0}</script></body></html>')
    estado, dados = monitor.classificar(html_cf_legit, config)
    ok = estado == monitor.LIMPO
    falhas += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FALHA'}] {'cf_script_nao_bloqueia':20s} "
          f"esperado={monitor.LIMPO:22s} obtido={estado:22s}")

    # Caso extra: CNJ presente MESMO com sentinela ainda na pagina -> PROCESSOS
    # (deteccao positiva de CNJ tem prioridade)
    html_misto = ("<html><body><p>Nenhum processo com o CPF identificado</p>"
                  "<p>0801234-56.2023.8.26.0100</p></body></html>")
    estado, _ = monitor.classificar(html_misto, config)
    ok = estado == monitor.PROCESSOS
    falhas += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FALHA'}] {'cnj_vence_sentinela':20s} "
          f"esperado={monitor.PROCESSOS:22s} obtido={estado:22s}")

    # Fontes semi-automaticas (e-SAJ/PJe): fixtures sinteticas best-effort,
    # cada uma com a sentinela/regex da sua propria fonte via config_da_fonte.
    CASOS_FONTE = [
        ("tjsp_esaj", "tjsp_limpo.html", monitor.LIMPO),
        ("tjsp_esaj", "tjsp_com_processo.html", monitor.PROCESSOS),
        ("trf3_pje", "trf3_limpo.html", monitor.LIMPO),
        ("trf3_pje", "trf3_com_processo.html", monitor.PROCESSOS),
    ]
    for fonte_id, nome, esperado in CASOS_FONTE:
        cfg_fonte = monitor.config_da_fonte(config_geral, monitor.obter_fonte(config_geral, fonte_id))
        estado, dados = monitor.classificar(ler(nome), cfg_fonte)
        ok = estado == esperado
        falhas += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FALHA'}] {fonte_id}/{nome:20s} esperado={esperado:22s} "
              f"obtido={estado:22s}  ({dados['motivo']})")

    print()
    if falhas:
        print(f"RESULTADO: {falhas} FALHA(S)")
        return 1
    print("RESULTADO: TODOS OS TESTES PASSARAM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
