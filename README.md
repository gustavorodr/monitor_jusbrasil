# Monitor JusBrasil — processos por CPF

Vigia uma página pública de perfil no JusBrasil e **avisa por notificação de
sistema** se aparecer processo judicial (número CNJ) vinculado ao perfil.
Roda ao ligar a máquina e todo dia ao meio-dia e às 18h, via **systemd user
timer** (não cron — cron não tem `DISPLAY`/DBUS e as notificações não
apareceriam na tela).

URL monitorada: definida em `config.json` (veja `config.example.json` para o formato).

---

## Como usar no dia a dia

> Todos os comandos assumem que você está em `~/monitor_jusbrasil/`.

- **Ver estado atual e logs:**
  ```bash
  ./.venv/bin/python monitor.py --status     # estado atual (JSON)
  tail -f logs/monitor.log                   # acompanha cada run ao vivo
  ```

- **Marcar homônimo (parar de alertar sobre um CNJ que não é seu):**
  ```bash
  ./.venv/bin/python monitor.py --ignorar 0801234-56.2023.8.26.0100
  ```

- **Desligar o monitor:**
  ```bash
  systemctl --user disable --now monitor-jusbrasil.timer
  ```

(Detalhes e mais comandos nas seções abaixo.)

---

## Os TRÊS estados (nunca dois)

O erro fatal de um monitor é confundir **"está limpo"** com **"está quebrado"**.
Por isso o script tem três estados, e o silêncio só é permitido no primeiro:

| Estado | Como é detectado | Ação |
|---|---|---|
| **LIMPO** | `totalLawsuits:0` no payload JSON da página **ou** a frase-sentinela "nenhum processo com o CPF identificado" | silêncio (+ sinal de vida semanal) |
| **PROCESSOS_ENCONTRADOS** | ≥1 número CNJ no DOM (regex `\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}`) **ou** `totalLawsuits>0` | **ALERTA** crítico + modal |
| **INCONCLUSIVO** | challenge/captcha do Cloudflare, timeout, erro, HTML vazio, ou página sem CNJ/contador/sentinela (layout mudou) | conta; alerta de **manutenção** após 3 seguidos |

> **Por que não uso "a frase sumiu" como gatilho:** daria falso positivo a cada
> mudança de layout/banner. Uso **detecção positiva**: um número CNJ, ou o
> contador autoritativo `totalLawsuits` que vem no JSON embutido da própria
> página (`__NEXT_DATA__`). É o sinal mais robusto — a frase visível
> "nenhum processo" nem sequer renderiza em navegador headless sem login.

> **O bug que este projeto evita:** a versão ingênua faz `if status != 200:
> return` e, como o site devolve 403, vira um `return` calado todo dia — falso
> negativo silencioso. Aqui, 403/challenge/timeout caem em **INCONCLUSIVO**, que
> é distinto de LIMPO e dispara alerta de manutenção se persistir.

---

## Como testar

```bash
cd ~/monitor_jusbrasil

# 1) Testes offline da lógica dos 3 estados (fixtures em disco, sem rede)
./.venv/bin/python tests/test_classificar.py

# 2) Testes da máquina de estados / anti-spam (sem rede, sem notificar de verdade)
./.venv/bin/python tests/test_estado.py

# 3) Classificar um HTML avulso de disco
./.venv/bin/python monitor.py --testar-fixture fixtures/com_processo.html

# 4) Rodar de verdade contra a URL (usa Playwright/Chromium)
./.venv/bin/python monitor.py

# 5) Disparar as notificações de teste (aparecem na tela)
./.venv/bin/python monitor.py --notificar-teste alerta       # crítico + modal
./.venv/bin/python monitor.py --notificar-teste manutencao   # normal
./.venv/bin/python monitor.py --notificar-teste vida         # discreto
```

## Ver logs, estado e snapshots

```bash
tail -f ~/monitor_jusbrasil/logs/monitor.log      # log append, 1 linha por run
./.venv/bin/python monitor.py --status            # estado atual em JSON
ls -lt ~/monitor_jusbrasil/snapshots/             # HTML salvo por run (últimos 10)
journalctl --user -u monitor-jusbrasil.service -n 30   # o que o timer executou
```

**Snapshots** guardam o HTML de cada run (rotação: 10 mais recentes). Quando o
layout do JusBrasil mudar e o monitor ficar INCONCLUSIVO, use-os para diffar e
entender o que quebrou:
```bash
cd ~/monitor_jusbrasil/snapshots && diff <(ls -t | sed -n 2p | xargs cat) <(ls -t | sed -n 1p | xargs cat)
```

## Marcar homônimo (parar de alertar sobre um CNJ)

O JusBrasil mistura pessoas de mesmo nome. Se um processo alertado **não é seu**:

```bash
./.venv/bin/python monitor.py --ignorar 0801234-56.2023.8.26.0100
```

O CNJ vai para a lista `ignorados` em `state/estado.json` e nunca mais gera
alerta. As notificações **nunca afirmam que o processo é seu** — sempre pedem
para conferir se não é homônimo, mostrando as partes/contexto que der para extrair.

## Agendamento (systemd user timer)

```bash
systemctl --user list-timers monitor-jusbrasil.timer   # ver próximos disparos
systemctl --user start monitor-jusbrasil.service       # rodar agora, manualmente
```
- Roda **ao ligar a máquina** (`OnStartupSec=2min`) e todo dia às **12h** e **18h**.
- `Persistent=true`: se a máquina estava desligada no horário, roda ao voltar.
- `RandomizedDelaySec=300`: folga de até 5 min para não bater no segundo exato.
- `loginctl enable-linger` já foi habilitado (o gerenciador --user sobe no boot).

> **Nota honesta sobre notificações no boot:** o gatilho de boot só mostra popup
> se houver sessão gráfica ativa (você logado). Sem login, a run acontece e é
> registrada no log/estado, mas um popup não teria onde aparecer — as runs das
> 12h/18h com você usando a máquina cobrem isso, e `Persistent=true` recupera
> horários perdidos assim que você loga.

Se as notificações não aparecerem quando disparadas pelo timer, reimporte o
ambiente gráfico para o systemd --user:
```bash
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS
```

## Desligar

```bash
systemctl --user disable --now monitor-jusbrasil.timer   # para e desabilita
# opcional, remover de vez:
rm ~/.config/systemd/user/monitor-jusbrasil.{service,timer}
systemctl --user daemon-reload
loginctl disable-linger $USER    # se não usar mais nenhum serviço --user no boot
```

---

## Estrutura

```
~/monitor_jusbrasil/
├── .venv/                 # Playwright + Chromium isolados
├── monitor.py             # tudo: fetch, classificação, estados, notificações
├── config.json            # url, sentinela, regex CNJ, limiares
├── logs/monitor.log       # append, timestamp + estado de cada run
├── state/estado.json      # último estado, CNJs vistos, ignorados, sinal de vida
├── snapshots/             # HTML por run (últimos 10, com rotação)
├── fixtures/              # HTMLs de teste dos 3 estados
├── tests/                 # test_classificar.py, test_estado.py
└── README.md
```

## Configuração (`config.json`)

- `inconclusivo_limite_alerta` (3): quantos INCONCLUSIVOs seguidos até alertar manutenção.
- `sinal_de_vida_dias` (7): a cada quantos dias mandar o "monitor ativo".
- `snapshots_manter` (10): quantos snapshots reter.
- `navegacao_timeout_ms` (45000): timeout de navegação do Playwright.

---

## Dois avisos honestos

1. **JusBrasil é fonte secundária.** Agrega com atraso e mistura homônimos. Um
   processo real pode aparecer lá semanas depois — ou nunca. Para garantia, a
   fonte autoritativa é o portal do tribunal (e-SAJ/PJe do seu TJ, TRT, TRF).
   Este monitor é uma rede de segurança conveniente, não cobertura completa.
2. **A parte frágil é o parsing, não o Cloudflare.** O regex de CNJ é estável
   (Resolução 65 do CNJ) e o contador `totalLawsuits` também. Mas extrair partes
   e tribunal depende do HTML deles, que muda sem aviso. É para isso que existem
   o estado INCONCLUSIVO e os snapshots: quando quebrar, você é avisado e tem o
   HTML para corrigir — em vez de descobrir meses depois.

Sem arms race anti-bot: Chromium headless normal, user-agent realista, 1
requisição por vez. Se o Cloudflare endurecer, o monitor prefere avisar que
ficou **cego** (INCONCLUSIVO) a tentar burlar.
```
