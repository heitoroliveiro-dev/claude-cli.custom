#!/usr/bin/env python3
"""Status line do Claude Code: lê o JSON da sessão no stdin e imprime 2 linhas.

Variáveis de ambiente:
  NO_COLOR=1          saída sem cores
  COLUMNS=N           força a largura usada para encaixar os blocos
  STATUSLINE_DEBUG=1  grava o último payload em ~/.cache/claude-statusline/last.json
Erros de blocos individuais vão para ~/.cache/claude-statusline/error.log.
"""
import sys
import json
import math
import os
import re
import getpass
import socket
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path


def fg(code):
    return f"\033[38;5;{code}m"


COLORS = {
    "green": fg(114),
    "blue": fg(75),
    "cyan": fg(80),
    "purple": fg(141),
    "yellow": fg(221),
    "orange": fg(215),
    "red": fg(203),
    "gray": fg(245),
    "dark": fg(238),
    "bold": "\033[1m",
    "reset": "\033[0m",
}
C = COLORS

SEP = f" {C['dark']}│{C['reset']} "

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")) / "claude-statusline"
LOG_MAX_BYTES = 100_000
GIT_TIMEOUT = 0.3
MAX_TEXT = 60
WIDTH_MARGIN = 2  # folga para o padding que o Claude Code coloca em volta da barra

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# --- Utilitários de dados -------------------------------------------------

def get(data, *keys):
    """Acessa chaves aninhadas tolerando None em qualquer nível."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def num(value):
    """Número finito ou None (rejeita bool, NaN, ±inf e inteiros gigantes)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def pct(value):
    n = num(value)
    return None if n is None else max(0.0, min(100.0, float(n)))


def clean(value, limit=MAX_TEXT, tail=False):
    """Remove escapes/controles (evita quebrar o layout) e limita o tamanho."""
    text = CONTROL_RE.sub("", ANSI_RE.sub("", str(value)))
    if len(text) > limit:
        text = "…" + text[-(limit - 1):] if tail else text[:limit - 1] + "…"
    return text


def visible_len(text):
    return len(ANSI_RE.sub("", text))


# --- Log e debug ----------------------------------------------------------

def log_error(context):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        log = CACHE_DIR / "error.log"
        if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
            log.replace(log.with_suffix(".log.1"))
        with log.open("a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now().isoformat(timespec='seconds')} [{context}]\n{traceback.format_exc()}\n")
    except OSError:
        pass


def save_debug(raw):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "last.json").write_text(raw, encoding="utf-8")
    except OSError:
        pass


def safe(fn, *args):
    """Executa um bloco; se falhar, omite só ele e registra no log."""
    try:
        return fn(*args)
    except Exception:
        log_error(getattr(fn, "__name__", "?"))
        return None


# --- Formatação -----------------------------------------------------------

def get_color_gradient(percentage):
    if percentage < 50: return C["green"]
    if percentage < 80: return C["yellow"]
    return C["red"]


def bar(percentage, width=10):
    """Barra de progresso com blocos de 1/8 para resolução fina."""
    pct_ = max(0.0, min(100.0, float(percentage)))
    eighths = round(pct_ / 100 * width * 8)
    full, rest = divmod(eighths, 8)
    partial = " ▏▎▍▌▋▊▉"[rest] if rest else ""
    empty = width - full - (1 if partial else 0)
    color = get_color_gradient(pct_)
    return f"{color}{'█' * full}{partial}{C['dark']}{'░' * empty}{C['reset']}"


def meter(label, percentage, width=10, suffix=""):
    if percentage is None:
        return f"{C['gray']}{label}{C['reset']} {C['dark']}{'░' * width}{C['reset']} {C['gray']}--{C['reset']}"
    color = get_color_gradient(percentage)
    return (f"{C['gray']}{label}{C['reset']} {bar(percentage, width)} "
            f"{color}{percentage:.0f}%{C['reset']}{suffix}")


def short_path(path):
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        path = "~" + path[len(home):]
    parts = path.split(os.sep)
    # Abrevia diretórios intermediários quando o caminho é longo
    if len(parts) > 4:
        parts = parts[:1] + [p[:1] for p in parts[1:-2]] + parts[-2:]
    return os.sep.join(parts)


def format_duration(ms):
    seconds = int(ms // 1000)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        return f"{hours // 24}d{hours % 24}h"
    if hours:
        return f"{hours}h{minutes:02d}"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def reset_in(resets_at):
    """Aceita epoch (s ou ms) ou ISO 8601; retorna tempo restante tipo '2h13'."""
    if resets_at is None or isinstance(resets_at, bool):
        return None
    try:
        if isinstance(resets_at, (int, float)):
            ts = resets_at / 1000 if resets_at > 1e12 else resets_at
        else:
            ts = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00")).timestamp()
        remaining = ts - time.time()
        if not math.isfinite(remaining) or remaining <= 0:
            return None
        return format_duration(remaining * 1000)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def format_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(int(n))


# --- Ambiente -------------------------------------------------------------

def user_host():
    try:
        user = getpass.getuser()
    except Exception:  # OSError/KeyError conforme a versão do Python
        user = os.environ.get("USER") or "?"
    try:
        hostname = socket.gethostname().split('.')[0] or "?"
    except OSError:
        hostname = "?"
    return user, hostname


def git_branch(cwd):
    """Branch + '*' se houver mudanças, numa única chamada e sem criar index.lock."""
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain=v2", "--branch", "-uno"],
            capture_output=True, timeout=GIT_TIMEOUT,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    head = oid = None
    dirty = False
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):]
        elif line.startswith("# branch.oid "):
            oid = line[len("# branch.oid "):]
        elif line and not line.startswith("#"):
            dirty = True
    if head in (None, "(detached)"):
        head = oid[:7] if oid and oid != "(initial)" else "HEAD"
    return f"{head}{'*' if dirty else ''}"


def terminal_width():
    cols = os.environ.get("COLUMNS", "")
    if cols.isdigit() and int(cols) > 0:
        return int(cols)
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
        try:
            cols = os.get_terminal_size(fd).columns
        finally:
            os.close(fd)
    except OSError:
        return None
    return cols - WIDTH_MARGIN if cols > WIDTH_MARGIN else None


# --- Blocos ---------------------------------------------------------------

def seg_location(cwd):
    user, hostname = user_host()
    return (f"{C['green']}{clean(user, 20)}@{clean(hostname, 20)}{C['reset']}:"
            f"{C['blue']}{C['bold']}{clean(short_path(cwd), tail=True)}{C['reset']}")


def seg_branch(cwd):
    branch = git_branch(cwd)
    return f"{C['purple']}⎇ {clean(branch, 40)}{C['reset']}" if branch else None


def seg_model(data):
    model = get(data, "model", "display_name")
    model = clean(model, 30) if isinstance(model, str) else ""
    return f"{C['cyan']}◆ {model}{C['reset']}" if model.strip() else None


def context_percentage(data):
    used = pct(get(data, "context_window", "used_percentage"))
    if used is not None:
        return used
    # Fallback: calcula pelo uso atual quando o percentual ainda não veio
    size = num(get(data, "context_window", "context_window_size"))
    usage = get(data, "context_window", "current_usage")
    if not size or size <= 0 or not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    values = [num(usage.get(k)) for k in keys]
    if all(v is None for v in values):
        return None
    return pct(sum(v or 0 for v in values) / size * 100)


def seg_context(data, width, show_size):
    size = num(get(data, "context_window", "context_window_size"))
    suffix = f"{C['gray']}/{format_tokens(size)}{C['reset']}" if show_size and size and size > 0 else ""
    return meter("ctx", context_percentage(data), width, suffix)


def seg_limit(data, key, label, width):
    # Rate limits só existem em planos de assinatura
    used = pct(get(data, "rate_limits", key, "used_percentage"))
    if used is None:
        return None
    reset = reset_in(get(data, "rate_limits", key, "resets_at"))
    suffix = f" {C['gray']}↻{reset}{C['reset']}" if reset else ""
    return meter(label, used, width, suffix)


def seg_extras(data):
    extras = []
    cost = num(get(data, "cost", "total_cost_usd"))
    if cost and cost > 0:
        extras.append(f"{C['orange']}${cost:.2f}{C['reset']}")
    duration = num(get(data, "cost", "total_duration_ms"))
    if duration and duration > 0:
        extras.append(f"{C['gray']}⏱ {format_duration(duration)}{C['reset']}")
    added = int(num(get(data, "cost", "total_lines_added")) or 0)
    removed = int(num(get(data, "cost", "total_lines_removed")) or 0)
    if added or removed:
        extras.append(f"{C['green']}+{added}{C['reset']} {C['red']}-{removed}{C['reset']}")
    return " ".join(extras) or None


# --- Montagem -------------------------------------------------------------

def fit_line(segments, width, min_rank=1):
    """segments = [(rank, texto)]; rank None nunca sai, rank maior sai primeiro."""
    segs = [s for s in segments if s[1]]
    while width and visible_len(SEP.join(t for _, t in segs)) > width:
        droppable = [s for s in segs if s[0] is not None and s[0] >= min_rank]
        if not droppable:
            break
        segs.remove(max(droppable, key=lambda s: s[0]))
    return SEP.join(t for _, t in segs)


def metrics_segments(data, compact):
    ctx_width, limit_width = (5, 4) if compact else (10, 6)
    return [
        (None, safe(seg_context, data, ctx_width, not compact)),
        (1, safe(seg_limit, data, "five_hour", "5h", limit_width)),
        (2, safe(seg_limit, data, "seven_day", "7d", limit_width)),
        (3, safe(seg_extras, data)),
    ]


def resolve_cwd(data):
    cwd = get(data, "workspace", "current_dir") or data.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    try:
        return os.getcwd()
    except OSError:
        return "?"


def build(data, width=None):
    cwd = resolve_cwd(data)

    # Linha 1: identidade (user@host, diretório, branch, modelo)
    line1 = fit_line([
        (None, safe(seg_location, cwd)),
        (1, safe(seg_branch, cwd)),
        (2, safe(seg_model, data)),
    ], width)

    # Linha 2: métricas. Ordem de corte: extras → 7d → modo compacto → 5h
    line2 = fit_line(metrics_segments(data, compact=False), width, min_rank=2)
    if width and visible_len(line2) > width:
        line2 = fit_line(metrics_segments(data, compact=True), width)

    output = line1 + "\n" + line2
    if os.environ.get("NO_COLOR"):
        output = ANSI_RE.sub("", output)
    return output


def write(output):
    try:
        sys.stdout.buffer.write(output.encode("utf-8", "replace"))
        sys.stdout.flush()
    except BrokenPipeError:
        # Claude Code já fechou o pipe; evita traceback no encerramento
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass


def main():
    if sys.stdin.isatty():
        sys.stderr.write("uso: <json da status line> | cli.py  (o Claude Code envia o JSON no stdin)\n")
        return
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    if not raw.strip():
        return
    if os.environ.get("STATUSLINE_DEBUG"):
        save_debug(raw)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("payload não é um objeto JSON")
    except (ValueError, RecursionError) as e:
        output = f"{C['red']}statusline: {type(e).__name__}: {clean(e, 80)}{C['reset']}"
    else:
        try:
            output = build(data, terminal_width())
        except Exception as e:
            log_error("build")
            output = f"{C['red']}statusline: {type(e).__name__} (veja {CACHE_DIR}/error.log){C['reset']}"
    write(output)


if __name__ == "__main__":
    main()
