# Monitor de processos por CPF

Vigia fontes públicas e **avisa por notificação de sistema** se aparecer
processo judicial (número CNJ) vinculado ao CPF.

Duas categorias de fonte, configuradas em `config.json` → `fontes`:

- **`jusbrasil`** (`tipo: "auto"`): página pública de perfil no JusBrasil.
  100% automática, headless, roda ao ligar a máquina e todo dia ao meio-dia e
  às 18h via **systemd user timer** (não cron — cron não tem `DISPLAY`/DBUS
  e as notificações não apareceriam na tela).
- **`tjsp_esaj`** e **`trf3_pje`** (`tipo: "manual_captcha"`): consulta por
  CPF no e-SAJ (TJ-SP) e no PJe (TRF-3) — fontes autoritativas, mas exigem
  **reCAPTCHA em toda consulta**, então rodam **semi-automáticas**: o script
  abre um navegador visível, você resolve o captcha e busca manualmente, e
  o script retoma para ler e classificar o resultado
  (`python monitor.py --checar-tribunais`, veja seção própria abaixo).

URL/fontes monitoradas: definidas em `config.json` (veja `config.example.json`
para o formato).

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

# 3) Classificar um HTML avulso de disco (fonte default: jusbrasil)
./.venv/bin/python monitor.py --testar-fixture fixtures/com_processo.html
./.venv/bin/python monitor.py --testar-fixture fixtures/tjsp_com_processo.html --fonte tjsp_esaj
./.venv/bin/python monitor.py --testar-fixture fixtures/trf3_limpo.html --fonte trf3_pje

# 4) Rodar de verdade contra a URL do JusBrasil (usa Playwright/Chromium, headless)
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
`ignorados`/`cnjs_vistos` são compartilhados entre todas as fontes (um CNJ é
globalmente único, então ignorar/já ter visto vale para JusBrasil, e-SAJ e PJe).

## Fontes adicionais (TJ-SP / TRF-3) — semi-automático

O e-SAJ (TJ-SP) e o PJe (TRF-3) permitem busca pública por CPF sem login, mas
exigem **reCAPTCHA em toda consulta** — diferente do JusBrasil, não dá pra
esperar o desafio resolver sozinho em background. Em vez de integrar um
serviço pago de resolução de captcha (o que colidiria com a filosofia deste
projeto de não entrar em arms race anti-bot), o fluxo é **semi-automático**:

```bash
./.venv/bin/python monitor.py --checar-tribunais
```

Isso abre, uma de cada vez, um navegador **visível** para cada fonte
`manual_captcha` (`tjsp_esaj`, depois `trf3_pje`). Você:
1. Preenche o CPF no formulário de busca e resolve o captcha.
2. Aperta o botão de busca no navegador.
3. Volta ao terminal e aperta **Enter** quando o resultado estiver na tela.

O script então lê o HTML, classifica (mesma lógica de 3 estados, mesmos
`processar()`/anti-spam/sinal-de-vida), salva snapshot em
`snapshots/tjsp_esaj/` ou `snapshots/trf3_pje/`, e atualiza o estado dessa
fonte em `state/estado.json`.

**Lembrete automático:** como esse passo é manual, a run automática do
JusBrasil (systemd timer) também verifica há quantos dias cada fonte manual
não é checada; se passar de `tribunais_lembrete_dias` (padrão 14) sem
checagem, dispara uma notificação de manutenção pedindo para rodar
`--checar-tribunais`.

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
├── config.json            # fontes (jusbrasil/tjsp_esaj/trf3_pje), regex CNJ, limiares
├── logs/monitor.log       # append, timestamp + estado de cada run
├── state/estado.json      # cnjs_vistos/ignorados (globais) + estado por fonte
├── snapshots/
│   ├── jusbrasil/         # HTML por run (últimos 10, com rotação)
│   ├── tjsp_esaj/
│   └── trf3_pje/
├── fixtures/              # HTMLs de teste dos 3 estados, por fonte
├── tests/                 # test_classificar.py, test_estado.py
└── README.md
```

## Configuração (`config.json`)

Chaves compartilhadas (topo do arquivo):
- `cnj_regex`: regex do número CNJ (Resolução 65), igual para todas as fontes.
- `inconclusivo_limite_alerta` (3): quantos INCONCLUSIVOs seguidos até alertar manutenção.
- `sinal_de_vida_dias` (7): a cada quantos dias mandar o "monitor ativo" (por fonte).
- `tribunais_lembrete_dias` (14): a cada quantos dias lembrar de rodar `--checar-tribunais`.
- `snapshots_manter` (10): quantos snapshots reter por fonte.
- `navegacao_timeout_ms` (45000): timeout de navegação do Playwright.

Lista `fontes`, cada item com `id`/`nome`/`tipo` (`"auto"` ou `"manual_captcha"`)
e sua própria `url`/`url_busca` + `sentinela_limpo`/`sentinelas_alternativas`.
Ver `config.example.json` para o formato completo.

---

## Três avisos honestos

1. **JusBrasil é fonte secundária.** Agrega com atraso e mistura homônimos. Um
   processo real pode aparecer lá semanas depois — ou nunca. Por isso o e-SAJ
   (TJ-SP) e o PJe (TRF-3) foram adicionados como fontes autoritativas —
   ainda assim, este monitor é uma rede de segurança conveniente, não
   cobertura completa (só cobre TJ-SP/TRF-3; uma execução em outro estado ou
   tribunal não é vigiada).
2. **A parte frágil é o parsing, não o Cloudflare/captcha.** O regex de CNJ é
   estável (Resolução 65 do CNJ) e o contador `totalLawsuits` do JusBrasil
   também. Mas extrair partes/classe/assunto depende do HTML de cada site,
   que muda sem aviso. É para isso que existem o estado INCONCLUSIVO e os
   snapshots: quando quebrar, você é avisado e tem o HTML para corrigir — em
   vez de descobrir meses depois.
3. **As sentinelas de e-SAJ/PJe em `config.json` são estimativas.** Foram
   escritas a partir de pesquisa, não de uma captura ao vivo (a busca por CPF
   nesses portais exige reCAPTCHA, então não dava pra confirmar a frase exata
   de "nenhum processo encontrado" sem rodar o fluxo manual uma vez). Depois
   do primeiro `--checar-tribunais` real, confira o snapshot salvo em
   `snapshots/tjsp_esaj/`/`snapshots/trf3_pje/` contra a `sentinela_limpo`
   configurada e ajuste se divergir.

Sem arms race anti-bot: Chromium headless normal, user-agent realista, 1
requisição por vez. Se o Cloudflare/reCAPTCHA endurecer, o monitor prefere
avisar que ficou **cego** (INCONCLUSIVO) a tentar burlar.
