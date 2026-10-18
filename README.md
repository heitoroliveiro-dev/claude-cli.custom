# claude-cli.custom

Status line customizada para o [Claude Code](https://claude.com/claude-code): duas linhas com identidade, branch, modelo e barras de progresso de contexto, rate limits e custo da sessão — num único script Python, sem dependências.

```
home@Linux04:~/Projetos/cli-custom │ ⎇ main* │ ◆ Opus 5.5
ctx ██████▍░░░ 63%/200k │ 5h ██▌░░░ 41% ↻2h12 │ 7d █████▎ 88% ↻2d12h │ $1.23 ⏱ 12m +156 -23
```

No terminal as barras mudam de cor conforme o uso: verde (< 50%), amarelo (< 80%) e vermelho.

## Benefícios

- **Consumo à vista** — contexto e limites de 5h/7d como barras com resolução de 1/8 de bloco, mais quanto falta para o limite resetar. Dá para perceber que vai estourar *antes* de estourar.
- **Nunca quebra a barra** — cada bloco roda isolado. Campo `null`, tipo errado, `NaN`, timestamp estranho: o bloco problemático some, o resto continua aparecendo, e o erro vai para um log.
- **Não atrapalha o Claude** — o git roda em uma única chamada, com timeout de 0,3 s e `GIT_OPTIONAL_LOCKS=0`, então a barra nunca cria `index.lock` nem colide com um commit ou rebase em andamento. Cada execução leva ~75 ms.
- **Seguro com dados externos** — nomes de diretório, branch e modelo passam por sanitização: sequências ANSI e caracteres de controle são removidos, então nada injeta escapes ou quebra o layout.
- **Funciona em qualquer locale** — entrada e saída em UTF-8 por bytes; não falha em terminais com `LANG=C` ou encoding ASCII.
- **Adapta à largura** — quando a largura do terminal é conhecida, descarta blocos por prioridade em vez de quebrar a linha.
- **Zero dependências** — só a biblioteca padrão do Python 3. Um arquivo, fácil de ler e de mudar.
- **Testado** — suíte com `unittest` cobrindo payloads quebrados, locale ASCII, injeção de escapes, git real e encaixe de largura.

## O que aparece

| Bloco | Origem | Quando aparece |
|---|---|---|
| `user@host:dir` | sistema + `workspace.current_dir` | sempre (home vira `~`, caminhos longos são abreviados) |
| `⎇ main*` | `git status` | dentro de um repositório git (`*` = mudanças não commitadas; hash curto se HEAD estiver solto) |
| `◆ Opus 5.5` | `model.display_name` | quando o Claude Code informa o modelo |
| `ctx … /200k` | `context_window` | sempre (`--` enquanto não houver dado; calcula por `current_usage` se o percentual não vier) |
| `5h` / `7d` `↻2h12` | `rate_limits` | planos de assinatura (Pro/Max) |
| `$1.23 ⏱ 12m +156 -23` | `cost` | quando houver custo, duração ou linhas alteradas |

## Requisitos

- Python 3.8+ (`python3` no `PATH`)
- Git (opcional, só para mostrar a branch)
- Linux ou macOS, terminal com suporte a 256 cores e fonte com os caracteres `█ ░ ⎇ ◆ ⏱ ↻` (qualquer fonte moderna; não precisa de Nerd Font)

## Instalação

1. Clone o repositório e torne o script executável:

   ```bash
   git clone git@github.com:heitoroliveiro-dev/claude-cli.custom.git ~/claude-cli.custom
   chmod +x ~/claude-cli.custom/cli.py
   ```

2. Aponte o Claude Code para o script em `~/.claude/settings.json` (mescle com o que já existir no arquivo):

   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "~/claude-cli.custom/cli.py",
       "refreshInterval": 30
     }
   }
   ```

   `refreshInterval` é opcional: re-executa a barra a cada N segundos para que a contagem `↻` continue andando mesmo com o Claude parado.

3. Teste fora do Claude Code:

   ```bash
   echo '{"model":{"display_name":"Opus"},"context_window":{"used_percentage":42}}' | ~/claude-cli.custom/cli.py
   ```

   Pronto — a barra aparece na próxima atualização da interface do Claude Code.

Para atualizar depois: `git -C ~/claude-cli.custom pull`.

## Uso

O Claude Code executa o script a cada atualização, enviando o JSON da sessão pelo stdin; cada linha impressa vira uma linha da barra. Não há nada para rodar manualmente.

### Variáveis de ambiente

Coloque-as no bloco `env` do `settings.json` ou exporte no shell.

| Variável | Efeito |
|---|---|
| `NO_COLOR=1` | saída sem cores ([no-color.org](https://no-color.org)) |
| `COLUMNS=120` | força a largura usada para encaixar os blocos |
| `STATUSLINE_DEBUG=1` | grava o último JSON recebido em `~/.cache/claude-statusline/last.json` |

Exemplo:

```json
{
  "env": { "STATUSLINE_DEBUG": "1" }
}
```

### Logs e diagnóstico

- Erros de blocos individuais: `~/.cache/claude-statusline/error.log` (com traceback; rotaciona para `error.log.1` ao passar de 100 KB).
- Respeita `XDG_CACHE_HOME`, se definido.
- JSON inválido aparece na própria barra em vermelho, ex.: `statusline: JSONDecodeError: ...`.

## Customização

Tudo fica em `cli.py`, organizado em seções (utilitários, formatação, ambiente, blocos, montagem).

### Cores

As cores são índices da paleta de 256 cores, no dicionário `COLORS`:

```python
COLORS = {
    "green": fg(114),
    "blue": fg(75),
    ...
}
```

Troque os números para mudar o tema. Para ver a paleta: `for i in $(seq 0 255); do printf "\e[38;5;${i}m%3d " $i; done; echo`.

### Limiares de alerta

Em `get_color_gradient`:

```python
def get_color_gradient(percentage):
    if percentage < 50: return C["green"]
    if percentage < 80: return C["yellow"]
    return C["red"]
```

### Largura das barras e ícones

- Largura das barras: `metrics_segments` — `(10, 6)` no modo normal (contexto, limites) e `(5, 4)` no compacto.
- Caracteres da barra: `bar()` (`█`, `░` e os blocos parciais `▏▎▍▌▋▊▉`).
- Ícones: `⎇` em `seg_branch`, `◆` em `seg_model`, `⏱` em `seg_extras`, `↻` em `seg_limit`.

### Ordem de corte por largura

Em `metrics_segments`, cada bloco tem um *rank*: maior sai primeiro, `None` nunca sai.

```python
return [
    (None, safe(seg_context, ...)),   # contexto: fica sempre
    (1, safe(seg_limit, ..., "five_hour", ...)),
    (2, safe(seg_limit, ..., "seven_day", ...)),
    (3, safe(seg_extras, data)),      # custo/tempo/linhas: sai primeiro
]
```

Na linha 1, a mesma lógica fica em `build()`: modelo (rank 2) sai antes da branch (rank 1).

### Adicionando um bloco

1. Escreva uma função que recebe os dados e devolve texto (ou `None` para não aparecer). Use `get()` para ler o JSON com segurança e `clean()` para qualquer texto vindo de fora:

   ```python
   def seg_clock(data):
       return f"{C['gray']}{datetime.now():%H:%M}{C['reset']}"
   ```

2. Registre-a em `metrics_segments` (linha 2) ou na lista da linha 1 em `build()`, sempre via `safe()`, para que uma falha nela não derrube a barra:

   ```python
   (4, safe(seg_clock, data)),
   ```

Para descobrir quais campos o Claude Code envia, ative `STATUSLINE_DEBUG=1` e veja `~/.cache/claude-statusline/last.json`.

### Outros ajustes

| Constante | Padrão | Para quê |
|---|---|---|
| `GIT_TIMEOUT` | `0.3` | tempo máximo (s) esperando o git |
| `MAX_TEXT` | `60` | tamanho máximo do caminho exibido |
| `WIDTH_MARGIN` | `2` | folga subtraída da largura detectada do terminal |
| `LOG_MAX_BYTES` | `100_000` | tamanho do log antes de rotacionar |

## Testes

```bash
python3 -m unittest -v test_cli
```

Cobrem payloads nulos e com tipos errados, `NaN`/`Infinity`, timestamps inválidos, injeção de escapes, fallback do contexto, isolamento de blocos, encaixe por largura, `NO_COLOR`, rotação de log, execução real com locale ASCII e entradas inválidas, e um repositório git de verdade (branch, mudanças, HEAD solto, ausência de `index.lock`).

## Solução de problemas

| Sintoma | O que fazer |
|---|---|
| A barra não aparece | Confira se o caminho em `statusLine.command` existe e se o script é executável (`chmod +x`). Rode o teste do passo 3 da instalação. |
| Um bloco sumiu | Veja `~/.cache/claude-statusline/error.log`. |
| A linha 2 quebra em janelas estreitas | A largura não foi detectada; defina `COLUMNS` no bloco `env`. |
| Aparecem quadradinhos no lugar dos ícones | A fonte não tem os caracteres; troque os ícones (veja *Customização*). |
| `5h`/`7d` não aparecem | Normal fora de planos de assinatura: o Claude Code só envia `rate_limits` neles. |
