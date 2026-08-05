#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor JusBrasil — vigia uma pagina publica de perfil por CPF e avisa, por
NOTIFICACAO DE SISTEMA, se aparecer processo judicial (numero CNJ) no DOM.

Filosofia central (leia antes de mexer):
  Este monitor tem TRES estados, nunca dois. O erro fatal de um monitor e
  confundir "esta limpo" com "esta quebrado". Por isso:

    PROCESSOS_ENCONTRADOS  -> achou >=1 numero CNJ no DOM            -> ALERTA
    LIMPO                  -> nenhum CNJ E frase-sentinela presente   -> silencio
    INCONCLUSIVO           -> nem CNJ nem sentinela, ou challenge/    -> conta;
                              captcha/timeout/erro                       alerta so
                                                                         apos N seguidos

  NUNCA tratamos 403/challenge como "return calado". Silencio so e permitido
  no estado LIMPO, e confirmado por deteccao POSITIVA da sentinela.

Uso:
  python monitor.py                      # run normal (rede via Playwright)
  python monitor.py --testar-fixture X   # classifica um HTML de disco (sem rede)
  python monitor.py --ignorar CNJ        # marca CNJ como homonimo (nao alerta mais)
  python monitor.py --status             # mostra estado atual
  python monitor.py --notificar-teste alerta|manutencao|vida  # dispara notificacao
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state", "estado.json")
LOG_PATH = os.path.join(BASE, "logs", "monitor.log")
SNAP_DIR = os.path.join(BASE, "snapshots")
ICON_ALERTA = "dialog-warning"

# Estados
PROCESSOS = "PROCESSOS_ENCONTRADOS"
LIMPO = "LIMPO"
INCONCLUSIVO = "INCONCLUSIVO"


# --------------------------------------------------------------------------
# util
# --------------------------------------------------------------------------
def agora():
    return datetime.now(timezone.utc).astimezone()


def normalizar(texto):
    """minusculas, sem acento, espacos colapsados — pra comparar sentinela."""
    if texto is None:
        return ""
    t = unicodedata.normalize("NFKD", texto)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def strip_tags(html):
    """extrai texto visivel de forma tosca mas suficiente pra sentinela/partes."""
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", html)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return re.sub(r"\s+", " ", txt).strip()


def log(msg):
    linha = f"{agora().isoformat()}  {msg}"
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(linha + "\n")
    print(linha, file=sys.stderr)


def carregar_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def carregar_estado():
    if not os.path.exists(STATE_PATH):
        return {
            "ultimo_estado": None,
            "ultimo_timestamp": None,
            "inconclusivos_consecutivos": 0,
            "cnjs_vistos": [],
            "ignorados": [],
            "ultimo_sinal_de_vida": None,
            "ultima_checagem_ok": None,
        }
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def salvar_estado(estado):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------
# deteccao de challenge do Cloudflare
# --------------------------------------------------------------------------
# Marcadores que aparecem no TEXTO VISIVEL de uma pagina de bloqueio real.
# Se qualquer um deles esta visivel, e challenge de verdade — sem ressalva.
CHALLENGE_VISIVEL = [
    "just a moment",
    "verifying you are human",
    "verificando se voce e humano",
    "checking your browser",
    "verificando seu navegador",
    "enable javascript and cookies to continue",
    "needs to review the security of your connection",
    "attention required",
]

# Marcadores TECNICOS (scripts/atributos do Cloudflare). Aparecem TAMBEM em
# paginas legitimas servidas pelo CF (ex.: challenge-platform injetado por
# bot-management). So contam como challenge quando a pagina e um interstitio
# curto — isto e, quase sem texto visivel e sem os dados da pagina real.
CHALLENGE_TECNICO = [
    "cf-challenge",
    "cf_chl_opt",
    "challenge-platform",
    "turnstile",
    "cf-mitigated",
    "captcha",
]


def eh_challenge(html):
    n = normalizar(strip_tags(html))
    n_raw = normalizar(html)
    for m in CHALLENGE_VISIVEL:
        if m in n:
            return True, m
    # tecnicos so valem em interstitio curto (< 800 chars de texto visivel)
    if len(n) < 800:
        for m in CHALLENGE_TECNICO:
            if m in n_raw:
                return True, m
    return False, None


def extrair_total_lawsuits(html):
    """
    Le o contador autoritativo do JSON embutido (__NEXT_DATA__):
      "lawsuitsAggregations":{...,"totalLawsuits":N,...}
    Retorna int N, ou None se o campo nao existir (layout mudou/pagina diferente).
    Este e o sinal mais robusto: a frase-sentinela visivel nao renderiza em
    headless sem login, mas este contador vem sempre no payload da pagina.
    """
    m = re.search(r'"totalLawsuits"\s*:\s*(\d+)', html)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# decodificador de CNJ (Resolucao 65/2008 CNJ) — estavel, nao depende do HTML
# NNNNNNN-DD.AAAA.J.TR.OOOO
# --------------------------------------------------------------------------
SEGMENTOS = {
    "1": "STF", "2": "CNJ", "3": "STJ", "4": "Justica Federal",
    "5": "Justica do Trabalho", "6": "Justica Eleitoral",
    "7": "Justica Militar da Uniao", "8": "Justica Estadual",
    "9": "Justica Militar Estadual",
}


def decodificar_cnj(cnj):
    m = re.match(r"(\d{7})-(\d{2})\.(\d{4})\.(\d)\.(\d{2})\.(\d{4})", cnj)
    if not m:
        return {"cnj": cnj, "ano": None, "segmento": None, "tribunal_codigo": None}
    _, _, ano, j, tr, _ = m.groups()
    return {
        "cnj": cnj,
        "ano": ano,
        "segmento": SEGMENTOS.get(j, f"segmento {j}"),
        "tribunal_codigo": tr,
    }


# --------------------------------------------------------------------------
# CLASSIFICACAO — funcao pura, testavel sem rede. O coracao do monitor.
# --------------------------------------------------------------------------
def classificar(html, config):
    """
    Retorna (estado, dados).
      dados["cnjs"]     -> lista de dicts (cnj + decodificacao + contexto)
      dados["motivo"]   -> por que caiu nesse estado
      dados["challenge"]-> marcador de challenge, se houver
    """
    dados = {"cnjs": [], "motivo": "", "challenge": None}

    if not html or not html.strip():
        dados["motivo"] = "html vazio"
        return INCONCLUSIVO, dados

    challenge, marcador = eh_challenge(html)
    texto = strip_tags(html)
    n_texto = normalizar(texto)

    # 1) DETECCAO POSITIVA de processos: regex CNJ no DOM inteiro.
    cnj_re = re.compile(config["cnj_regex"])
    achados = []
    vistos_local = set()
    for m in cnj_re.finditer(html):
        cnj = m.group(0)
        if cnj in vistos_local:
            continue
        vistos_local.add(cnj)
        info = decodificar_cnj(cnj)
        info["contexto"] = extrair_contexto(texto, cnj)
        achados.append(info)

    total = extrair_total_lawsuits(html)
    dados["total_lawsuits"] = total

    if achados:
        dados["cnjs"] = achados
        dados["motivo"] = f"{len(achados)} numero(s) CNJ no DOM"
        if total is not None:
            dados["motivo"] += f" (totalLawsuits={total})"
        if challenge:
            dados["challenge"] = marcador
        return PROCESSOS, dados

    # 2) Sinal AUTORITATIVO do payload (__NEXT_DATA__). Se o contador existe, a
    #    pagina real carregou de fato — isso vence marcadores tecnicos do CF
    #    (challenge-platform aparece ate em paginas legitimas). So confiamos aqui
    #    quando NAO ha challenge VISIVEL forte (interstitio de bloqueio real).
    challenge_visivel = any(m in normalizar(strip_tags(html)) for m in CHALLENGE_VISIVEL)
    if total is not None and not challenge_visivel:
        if total > 0:
            dados["motivo"] = (f"totalLawsuits={total} no payload, mas nenhum CNJ "
                               "formatado no DOM (lista pode exigir paginacao/login)")
            return PROCESSOS, dados
        dados["motivo"] = "totalLawsuits=0 no payload (contador autoritativo)"
        return LIMPO, dados

    # 3) Sem dado autoritativo: se e challenge, e INCONCLUSIVO — nunca LIMPO.
    if challenge:
        dados["challenge"] = marcador
        dados["motivo"] = f"challenge/captcha detectado ({marcador})"
        return INCONCLUSIVO, dados

    # 4) LIMPO por DETECCAO POSITIVA da frase-sentinela visivel.
    sentinelas = [config["sentinela_limpo"]] + config.get("sentinelas_alternativas", [])
    for s in sentinelas:
        if normalizar(s) in n_texto:
            dados["motivo"] = f"sentinela presente: '{s}'"
            return LIMPO, dados

    # 4) Nem CNJ, nem contador, nem sentinela -> INCONCLUSIVO (layout mudou?)
    dados["motivo"] = "sem CNJ, sem contador e sem frase-sentinela (layout pode ter mudado)"
    return INCONCLUSIVO, dados


def extrair_contexto(texto, cnj, janela=160):
    """pega um trecho de texto ao redor do CNJ, best-effort, pra mostrar partes."""
    i = texto.find(cnj)
    if i < 0:
        return ""
    ini = max(0, i - janela)
    fim = min(len(texto), i + len(cnj) + janela)
    trecho = texto[ini:fim].strip()
    return ("..." if ini > 0 else "") + trecho + ("..." if fim < len(texto) else "")


# --------------------------------------------------------------------------
# NOTIFICACOES
# --------------------------------------------------------------------------
def _notify_send(titulo, corpo, urgencia="normal", icone=ICON_ALERTA):
    if not shutil.which("notify-send"):
        log("AVISO: notify-send indisponivel; nao consegui notificar no desktop")
        return False
    try:
        subprocess.run(
            ["notify-send", "-u", urgencia, "-i", icone,
             "-a", "Monitor JusBrasil", titulo, corpo],
            check=False, timeout=15,
        )
        return True
    except Exception as e:
        log(f"ERRO notify-send: {e}")
        return False


def _zenity_warning(titulo, corpo):
    if not shutil.which("zenity"):
        log("zenity indisponivel; usei apenas notify-send (fallback registrado)")
        return False
    try:
        # nao bloquear o run: dispara e segue
        subprocess.Popen(
            ["zenity", "--warning", "--no-markup", "--title", titulo,
             "--width", "460", "--text", corpo],
        )
        return True
    except Exception as e:
        log(f"ERRO zenity: {e}")
        return False


def notificar_alerta(titulo, corpo):
    """Alerta critico: persiste no GNOME ate dispensar + modal zenity."""
    _notify_send(titulo, corpo, urgencia="critical", icone=ICON_ALERTA)
    _zenity_warning(titulo, corpo)


def notificar_manutencao(titulo, corpo):
    """Manutencao: -u normal, sem modal."""
    _notify_send(titulo, corpo, urgencia="normal", icone="dialog-information")


def notificar_vida(titulo, corpo):
    """Sinal de vida discreto: -u low."""
    _notify_send(titulo, corpo, urgencia="low", icone="emblem-default")


# --------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------
def salvar_snapshot(html, estado, config):
    os.makedirs(SNAP_DIR, exist_ok=True)
    ts = agora().strftime("%Y%m%d-%H%M%S")
    nome = f"{ts}_{estado}.html"
    caminho = os.path.join(SNAP_DIR, nome)
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(html or "")
    # rotacao: manter os N mais recentes
    manter = config.get("snapshots_manter", 10)
    arquivos = sorted(
        [os.path.join(SNAP_DIR, x) for x in os.listdir(SNAP_DIR) if x.endswith(".html")]
    )
    for velho in arquivos[:-manter]:
        try:
            os.remove(velho)
        except OSError:
            pass
    return caminho


# --------------------------------------------------------------------------
# fetch via Playwright (unico ponto que toca a rede)
# --------------------------------------------------------------------------
def buscar_html(config):
    """Retorna html (str). Levanta excecao em timeout/erro de navegacao."""
    from playwright.sync_api import sync_playwright

    url = config["url"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent=config["user_agent"],
                locale="pt-BR",
                viewport={"width": 1366, "height": 768},
            )
            page = ctx.new_page()
            # 'networkidle' nunca dispara aqui: o Cloudflare mantem a rede ocupada
            # com beacons. Usamos domcontentloaded e depois esperamos ATIVAMENTE
            # o challenge resolver, verificando o conteudo.
            page.goto(url, wait_until="domcontentloaded",
                      timeout=config["navegacao_timeout_ms"])
            # Espera ate ~30s pelo desafio: para assim que o DOM deixar de ser
            # challenge (aparecer CNJ ou a sentinela). Se nao resolver, devolve
            # o que tiver -> classificar() cai em INCONCLUSIVO (nao burlamos).
            limite = 30
            for _ in range(limite):
                html = page.content()
                ch, _m = eh_challenge(html)
                tem_cnj = re.search(config["cnj_regex"], html)
                tem_sent = normalizar(config["sentinela_limpo"]) in normalizar(strip_tags(html))
                tem_contador = extrair_total_lawsuits(html) is not None
                if (not ch) and (tem_cnj or tem_sent or tem_contador
                                 or len(strip_tags(html)) > 1200):
                    break
                page.wait_for_timeout(1000)
            return page.content()
        finally:
            browser.close()


# --------------------------------------------------------------------------
# maquina de estados + anti-spam + sinal de vida
# --------------------------------------------------------------------------
def dias_desde(iso):
    if not iso:
        return None
    try:
        return (agora() - datetime.fromisoformat(iso)).days
    except Exception:
        return None


def processar(estado_novo, dados, config, st):
    """Decide notificacoes com base na transicao. Muta e retorna st."""
    anterior = st.get("ultimo_estado")
    ignorados = set(st.get("ignorados", []))
    vistos = set(st.get("cnjs_vistos", []))
    agora_iso = agora().isoformat()

    if estado_novo == PROCESSOS:
        st["inconclusivos_consecutivos"] = 0
        st["ultima_checagem_ok"] = agora_iso
        # filtra homonimos ja marcados como ignorados
        relevantes = [c for c in dados["cnjs"] if c["cnj"] not in ignorados]
        novos = [c for c in relevantes if c["cnj"] not in vistos]

        # atualiza conjunto de vistos (inclui os ja conhecidos, exclui ignorados)
        for c in dados["cnjs"]:
            if c["cnj"] not in ignorados:
                vistos.add(c["cnj"])
        st["cnjs_vistos"] = sorted(vistos)

        if novos:
            corpo = montar_corpo_alerta(novos)
            titulo = f"Processo vinculado a este perfil ({len(novos)} novo(s))"
            notificar_alerta(titulo, corpo)
            log(f"ALERTA disparado: {len(novos)} CNJ novo(s): "
                + ", ".join(c["cnj"] for c in novos))
        elif not relevantes:
            log("PROCESSOS_ENCONTRADOS, mas todos os CNJ estao em 'ignorados' "
                "(homonimos). Sem alerta.")
        else:
            log("PROCESSOS_ENCONTRADOS, mas nenhum CNJ novo desde a ultima vez. "
                "Sem alerta (anti-spam).")

    elif estado_novo == LIMPO:
        st["inconclusivos_consecutivos"] = 0
        st["ultima_checagem_ok"] = agora_iso
        if anterior == PROCESSOS:
            notificar_manutencao(
                "Perfil voltou a ficar limpo",
                "Nao ha mais numeros CNJ na pagina; a sentinela de "
                '"nenhum processo" reapareceu.')
            log("TRANSICAO PROCESSOS -> LIMPO notificada.")
        else:
            log("LIMPO. Silencio (comportamento esperado).")

    elif estado_novo == INCONCLUSIVO:
        st["inconclusivos_consecutivos"] = st.get("inconclusivos_consecutivos", 0) + 1
        n = st["inconclusivos_consecutivos"]
        limite = config["inconclusivo_limite_alerta"]
        log(f"INCONCLUSIVO ({dados['motivo']}). Consecutivos: {n}/{limite}.")
        if n == limite or (n > limite and anterior != INCONCLUSIVO):
            notificar_manutencao(
                "Monitor JusBrasil pode estar CEGO",
                f"O monitor esta INCONCLUSIVO ha {n} execucoes seguidas "
                f"({dados['motivo']}).\n"
                "O Cloudflare pode ter endurecido ou o layout mudou. "
                "Confira os snapshots e o log.")
            log(f"MANUTENCAO disparada: cego ha {n} execucoes.")

    st["ultimo_estado"] = estado_novo
    st["ultimo_timestamp"] = agora_iso

    # sinal de vida semanal, so quando NAO ha nada errado (LIMPO)
    if estado_novo == LIMPO:
        d = dias_desde(st.get("ultimo_sinal_de_vida"))
        if d is None or d >= config.get("sinal_de_vida_dias", 7):
            ok = st.get("ultima_checagem_ok") or agora_iso
            notificar_vida(
                "Monitor JusBrasil ativo",
                f"Nenhum processo vinculado. Ultima checagem OK: {ok[:16]}.")
            st["ultimo_sinal_de_vida"] = agora_iso
            log("Sinal de vida semanal enviado.")

    return st


def montar_corpo_alerta(cnjs):
    linhas = [
        "Apareceu processo vinculado a este perfil no JusBrasil.",
        "CONFIRA se nao e homonimo (o site mistura pessoas de mesmo nome).",
        "",
    ]
    for c in cnjs:
        linhas.append(f"CNJ: {c['cnj']}")
        linhas.append(f"  Justica: {c.get('segmento')}  | Tribunal (cod): "
                      f"{c.get('tribunal_codigo')}  | Ano: {c.get('ano')}")
        ctx = c.get("contexto")
        if ctx:
            linhas.append(f"  Contexto/partes: {ctx[:220]}")
        linhas.append("")
    linhas.append("Para ignorar um homonimo: "
                  "python monitor.py --ignorar «numero-CNJ»")
    return "\n".join(linhas)


# --------------------------------------------------------------------------
# comandos CLI
# --------------------------------------------------------------------------
def cmd_run(config):
    st = carregar_estado()
    html = None
    try:
        html = buscar_html(config)
        estado, dados = classificar(html, config)
    except Exception as e:
        # QUALQUER falha de rede/navegacao/timeout -> INCONCLUSIVO, nunca return calado
        estado, dados = INCONCLUSIVO, {
            "cnjs": [], "challenge": None,
            "motivo": f"excecao no fetch: {type(e).__name__}: {e}"}
        log(f"Fetch falhou: {e}")

    snap = salvar_snapshot(html, estado, config)
    log(f"Run -> {estado} | {dados['motivo']} | snapshot={os.path.basename(snap)}")
    st = processar(estado, dados, config, st)
    salvar_estado(st)
    print(f"Estado: {estado} — {dados['motivo']}")
    return 0


def cmd_testar_fixture(caminho, config):
    with open(caminho, encoding="utf-8") as f:
        html = f.read()
    estado, dados = classificar(html, config)
    print(f"[{os.path.basename(caminho)}] -> {estado}")
    print(f"  motivo: {dados['motivo']}")
    if dados["cnjs"]:
        for c in dados["cnjs"]:
            print(f"  CNJ {c['cnj']}  ({c.get('segmento')}, trib {c.get('tribunal_codigo')})")
    return estado


def cmd_ignorar(cnj, config):
    st = carregar_estado()
    ig = set(st.get("ignorados", []))
    ig.add(cnj)
    st["ignorados"] = sorted(ig)
    # tambem remove dos vistos pra nao re-alertar caso saia e volte
    st["cnjs_vistos"] = sorted(set(st.get("cnjs_vistos", [])) - {cnj})
    salvar_estado(st)
    print(f"CNJ {cnj} marcado como homonimo/ignorado. Nao alertarei mais sobre ele.")
    log(f"CNJ {cnj} adicionado a lista de ignorados.")
    return 0


def cmd_status():
    st = carregar_estado()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def cmd_notificar_teste(tipo):
    if tipo == "alerta":
        notificar_alerta(
            "TESTE — Processo vinculado a este perfil",
            montar_corpo_alerta([decodificar_cnj("1234567-89.2024.8.26.0100")
                                 | {"contexto": "Fulano de Tal x Empresa XPTO Ltda"}]))
    elif tipo == "manutencao":
        notificar_manutencao(
            "TESTE — Monitor pode estar CEGO",
            "Isto e um teste da notificacao de manutencao (-u normal, sem modal).")
    elif tipo == "vida":
        notificar_vida("TESTE — Monitor ativo",
                       "Isto e um teste do sinal de vida semanal (-u low).")
    else:
        print("tipo invalido: use alerta|manutencao|vida", file=sys.stderr)
        return 2
    print(f"Notificacao de teste '{tipo}' disparada.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Monitor JusBrasil por CPF (3 estados).")
    ap.add_argument("--testar-fixture", metavar="HTML")
    ap.add_argument("--ignorar", metavar="CNJ")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--notificar-teste", choices=["alerta", "manutencao", "vida"])
    args = ap.parse_args()

    config = carregar_config()

    if args.status:
        return cmd_status()
    if args.notificar_teste:
        return cmd_notificar_teste(args.notificar_teste)
    if args.ignorar:
        return cmd_ignorar(args.ignorar, config)
    if args.testar_fixture:
        cmd_testar_fixture(args.testar_fixture, config)
        return 0
    return cmd_run(config)


if __name__ == "__main__":
    sys.exit(main())
