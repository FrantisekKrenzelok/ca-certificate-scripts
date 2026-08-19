"""
caupdate/tui.py — Rich-based TUI for the ca-certificates update pipeline.

Activated by --human in plan.py, build_combo.py and process.py.
Falls back to plain timestamped stdout when rich is not installed or
--human is not passed.

Usage:
    from caupdate.tui import PipelineOutput

    out = PipelineOutput(human=args.human, title="plan.py")
    out.set_columns(["Release", "Bug", "CRYPTO", "State"])
    with out:
        out.update_row("rhel-10.3", ["rhel-10.3", "RHEL-212568", "–", "planned"])
        out.log("rhel-10.3: creating bug…")
"""

from __future__ import annotations

import sys
import time
import threading
from collections import deque
from typing import Any

# ── graceful fallback if rich is not installed ─────────────────────────────

try:
    from rich.columns import Columns
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
    from rich.spinner import Spinner
    from rich.progress import (
        SpinnerColumn, TextColumn, Progress, BarColumn,
        TaskID, MofNCompleteColumn,
    )
    _RICH = True
except ImportError:
    _RICH = False

# ── colour palette ─────────────────────────────────────────────────────────

_STATE_STYLE = {
    # terminal/good
    'complete':            'green',
    'planned':             'cyan',
    'staged':              'blue',
    'committed':           'blue',
    'pushed':              'blue',
    'builds complete':     'green',
    # waiting
    'waiting centos ci':   'yellow',
    'waiting centos merge':'yellow',
    'builds in progress':  'yellow',
    'builds in gating':    'yellow',
    # error / needs action
    'centos ci failed':    'red bold',
    'builds failed':       'red bold',
    'need bug':            'red',
    'needs errata':        'red',
    'waiting bug clone':   'magenta',
    # default
    '':                    'dim',
}

def _state_style(state: str) -> str:
    for k, v in _STATE_STYLE.items():
        if k and k in state:
            return v
    return 'white'

_LOG_LINES = 18
_LOG_LINES_SMALL = 8


# ── plain-text fallback ────────────────────────────────────────────────────

class _PlainOutput:
    """No-rich fallback: timestamped lines to stdout."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self) -> _PlainOutput:
        return self

    def __exit__(self, *_) -> None:
        pass

    def log(self, msg: str) -> None:
        ts = time.strftime('%H:%M:%S')
        with self._lock:
            print(f'{ts}  {msg}', flush=True)

    def set_columns(self, cols: list[str]) -> None:
        pass

    def update_row(self, key: str, values: list[str]) -> None:
        pass

    def set_subtitle(self, text: str) -> None:
        pass

    def print_final_table(self) -> None:
        pass


# ── rich TUI ───────────────────────────────────────────────────────────────

class _RichOutput:
    """Rich Live TUI: status table on top, scrolling log panel below."""

    def __init__(self, title: str) -> None:
        self._title    = title
        self._subtitle = ''
        self._columns: list[str] = []
        self._rows: dict[str, list[str]] = {}    # key → column values
        self._row_order: list[str] = []
        self._log: deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()

        import os
        in_zellij = os.environ.get('ZELLIJ') is not None
        self._console = Console(force_terminal=True) if in_zellij else Console()
        self._refresh_rate = 1 if in_zellij else 4
        self._live: Live | None = None
        self._stop = threading.Event()

    def __enter__(self) -> _RichOutput:
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=self._refresh_rate,
            screen=False,
        )
        self._live.__enter__()
        t = threading.Thread(target=self._auto_refresh, daemon=True)
        t.start()
        self._refresh_thread = t
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._refresh_thread.join(timeout=2)
        if self._live:
            self._live.__exit__(*exc)
        self.print_final_table()

    def _auto_refresh(self) -> None:
        interval = 1.0 / self._refresh_rate
        while not self._stop.wait(timeout=interval):
            if self._live:
                self._live.update(self._render())

    # ── public API ─────────────────────────────────────────────────────────

    def set_columns(self, cols: list[str]) -> None:
        with self._lock:
            self._columns = cols

    def update_row(self, key: str, values: list[str]) -> None:
        with self._lock:
            if key not in self._row_order:
                self._row_order.append(key)
            self._rows[key] = values

    def set_subtitle(self, text: str) -> None:
        with self._lock:
            self._subtitle = text

    def log(self, msg: str) -> None:
        ts = time.strftime('%H:%M:%S')
        entry = f'{ts}  {msg.rstrip()}'
        with self._lock:
            self._log.append(entry)
        # Also emit below the live display so nothing is lost
        if self._live:
            self._live.console.print(entry, markup=False, highlight=False)

    def print_final_table(self) -> None:
        """Print a clean summary table after the Live context exits."""
        with self._lock:
            if not self._columns or not self._rows:
                return
            table = Table(title=f'{self._title} — final status',
                          show_header=True, header_style='bold')
            for col in self._columns:
                table.add_column(col)
            for key in self._row_order:
                vals = self._rows.get(key, [])
                if not vals:
                    continue
                state_val = vals[-1] if vals else ''
                styled = [Text(v, style=_state_style(state_val) if i == len(vals) - 1
                               else '') for i, v in enumerate(vals)]
                table.add_row(*styled)
        self._console.print(table)

    # ── rendering ──────────────────────────────────────────────────────────

    def _render_table(self) -> Table:
        table = Table(show_header=True, header_style='bold cyan',
                      expand=True, box=None, padding=(0, 1))
        for col in self._columns:
            table.add_column(col)
        for key in self._row_order:
            vals = self._rows.get(key, [])
            if not vals:
                continue
            state_val = vals[-1] if vals else ''
            styled = []
            for i, v in enumerate(vals):
                if i == len(vals) - 1:
                    styled.append(Text(v, style=_state_style(state_val)))
                else:
                    styled.append(v)
            table.add_row(*styled)
        return table

    def _render_log(self, max_lines: int) -> Text:
        visible = list(self._log)[-max_lines:]
        text = Text()
        for line in visible:
            text.append(line + '\n', style='dim')
        return text

    def _render(self) -> Table:
        height = self._console.size.height

        with self._lock:
            subtitle = self._subtitle
            n_rows   = len(self._row_order)

        # fixed heights: title panel (3) + table header+rows + log panel border (2)
        table_height = max(4, n_rows + 2)
        log_height   = max(_LOG_LINES_SMALL, height - table_height - 6)

        with self._lock:
            status_table = self._render_table()
            log_text     = self._render_log(log_height)

        title_text = f'[bold]{self._title}[/bold]'
        if subtitle:
            title_text += f'  [dim]{subtitle}[/dim]'

        header = Panel(status_table, title=title_text, border_style='blue')
        log_panel = Panel(log_text, title='log', border_style='dim',
                          padding=(0, 1))

        grid = Table.grid(expand=True)
        grid.add_row(header)
        grid.add_row(log_panel)
        return grid


# ── process-mode TUI (progress bars per release + log pane) ───────────────

# Ordered pipeline progress values (0-100)
_PROCESS_PROGRESS: dict[str, int] = {
    'planned':               5,
    'staged':               12,
    'committed':            18,
    'pushed':               25,
    'waiting centos ci':    32,
    'waiting centos merge': 38,
    'builds not started':   44,
    'builds need push':     20,
    'builds in progress':   55,
    'builds in gating':     65,
    'builds complete':      70,
    'needs errata':         73,
    'need builds attached': 78,
    'needs bugs attached':  83,
    'errata ready QE':      90,
    'complete':            100,
    # error/terminal
    'centos ci failed':     35,
    'builds failed':        55,
    'need bug':              2,
}


_TERMINAL_STATES = {'complete', 'centos ci failed', 'builds failed'}


def _state_progress(state: str) -> int:
    """Map a state string to a 0-100 progress value."""
    if state in _PROCESS_PROGRESS:
        return _PROCESS_PROGRESS[state]
    for k, v in _PROCESS_PROGRESS.items():
        if k in state:
            return v
    return 0


class _ProcessRichOutput:
    """
    Process.py TUI: two-pane layout.
    Top  — per-release progress bars (one per release, advances through states).
    Bottom — scrolling log panel.
    """

    def __init__(self, title: str) -> None:
        self._title    = title
        self._subtitle = ''
        self._lock     = threading.Lock()
        self._log: deque[str] = deque(maxlen=400)
        self._rows: dict[str, list] = {}   # release → entry dict

        import os
        in_zellij = os.environ.get('ZELLIJ') is not None
        self._console      = Console(force_terminal=True) if in_zellij else Console()
        self._refresh_rate = 1 if in_zellij else 4
        self._live: 'Live | None' = None
        self._stop = threading.Event()

        self._progress = Progress(
            SpinnerColumn(finished_text='[green]✓[/]'),
            TextColumn('{task.description}', style='bold cyan'),
            BarColumn(bar_width=28),
            TextColumn('{task.fields[pct]:>3}%', style='dim'),
            TextColumn('{task.fields[state_str]}'),
            TextColumn('[dim]{task.fields[meta]}[/dim]'),
            console=self._console,
            transient=False,
            expand=False,
        )
        self._task_ids: 'dict[str, TaskID]' = {}

    # ── public API ─────────────────────────────────────────────────────────

    def set_columns(self, cols: list) -> None:
        pass  # columns handled by progress bar layout

    def set_subtitle(self, text: str) -> None:
        with self._lock:
            self._subtitle = text

    def initialize_releases(self, releases: dict) -> None:
        """Pre-populate progress bars before processing starts."""
        for release, entry in releases.items():
            self._upsert_task(release, entry)

    def update_row(self, key: str, values: list) -> None:
        """Called by process.py with [release, branch, state] or similar."""
        # Store raw values; also accept an entry dict (from initialize_releases)
        with self._lock:
            if isinstance(values, dict):
                self._rows[key] = values
                entry = values
            else:
                self._rows[key] = values
                # Reconstruct a minimal entry dict from column values
                state  = values[-1] if values else ''
                branch = values[1]  if len(values) > 1 else ''
                entry  = {'state': state, 'branch': branch}
        self._upsert_task(key, entry)

    def update_release(self, key: str, entry: dict) -> None:
        """Update a release directly from its rhel_packages entry dict."""
        with self._lock:
            self._rows[key] = entry
        self._upsert_task(key, entry)

    def log(self, msg: str) -> None:
        ts = time.strftime('%H:%M:%S')
        with self._lock:
            self._log.append(f'{ts}  {msg.rstrip()}')

    def print_final_table(self) -> None:
        pass  # progress bars remain visible as final state

    # ── internal ───────────────────────────────────────────────────────────

    def _upsert_task(self, release: str, entry: dict) -> None:
        if isinstance(entry, dict):
            state  = entry.get('state', 'planned')
            branch = entry.get('branch', '')
            bug    = entry.get('bugnumber', '')
            errata = entry.get('erratanumber', 0)
            nvr    = entry.get('nvr', '') or ''
        else:
            state = branch = bug = nvr = ''
            errata = 0

        pct        = _state_progress(state)
        style      = _state_style(state)
        state_str  = f'[{style}]{state:<26}[/]'

        parts: list[str] = []
        if bug and bug not in ('0', ''): parts.append(bug)
        if errata:                        parts.append(f'#{errata}')
        if nvr:                           parts.append(nvr[:32])
        meta = '  '.join(parts)

        desc = f'{release:<16} [{branch}]' if branch else f'{release:<16}'

        with self._lock:
            if release not in self._task_ids:
                tid = self._progress.add_task(
                    desc, total=100, completed=pct,
                    pct=pct, state_str=state_str, meta=meta)
                self._task_ids[release] = tid
            else:
                tid = self._task_ids[release]
                self._progress.update(
                    tid,
                    completed=pct, description=desc,
                    pct=pct, state_str=state_str, meta=meta)
            if state in _TERMINAL_STATES:
                self._progress.stop_task(self._task_ids[release])

    def _render_log(self, max_lines: int) -> Text:
        with self._lock:
            visible = list(self._log)[-max_lines:]
        text = Text()
        for line in visible:
            text.append(line + '\n', style='dim')
        return text

    def _render(self) -> Table:
        height = self._console.size.height
        with self._lock:
            subtitle   = self._subtitle
            n_tasks    = len(self._task_ids)

        prog_height = max(4, n_tasks + 4)
        log_height  = max(8, height - prog_height - 8)

        title_text = f'[bold]{self._title}[/bold]'
        if subtitle:
            title_text += f'  [dim]{subtitle}[/dim]'

        log_text = self._render_log(log_height)

        top    = Panel(self._progress, title=title_text, border_style='blue')
        bottom = Panel(log_text, title='log', border_style='dim', padding=(0, 1))

        grid = Table.grid(expand=True)
        grid.add_row(top)
        grid.add_row(bottom)
        return grid

    # ── context manager ────────────────────────────────────────────────────

    def __enter__(self) -> '_ProcessRichOutput':
        self._stop.clear()
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=self._refresh_rate,
            screen=False,
        )
        self._live.__enter__()
        t = threading.Thread(target=self._auto_refresh, daemon=True)
        t.start()
        self._refresh_thread = t
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._refresh_thread.join(timeout=2)
        if self._live:
            self._live.__exit__(*exc)

    def _auto_refresh(self) -> None:
        interval = 1.0 / self._refresh_rate
        while not self._stop.wait(timeout=interval):
            if self._live:
                self._live.update(self._render())


# ── public entry point ─────────────────────────────────────────────────────

class PipelineOutput:
    """
    Unified output interface for the ca-certificates pipeline scripts.

    In plain mode (human=False): timestamped stdout lines.
    In human mode (human=True):  rich Live TUI with status table + log.

    Use as a context manager:
        with PipelineOutput(human=True, title="plan.py") as out:
            out.set_columns(["Release", "Bug", "State"])
            out.update_row("rhel-10.3", ["rhel-10.3", "–", "planned"])
            out.log("creating bug for rhel-10.3…")
    """

    def __init__(self, human: bool = False, title: str = 'pipeline',
                 mode: str = 'table') -> None:
        if human and _RICH:
            if mode == 'process':
                self._impl: '_PlainOutput | _RichOutput | _ProcessRichOutput' = \
                    _ProcessRichOutput(title)
            else:
                self._impl = _RichOutput(title)
        else:
            if human and not _RICH:
                print('WARNING: rich not installed — falling back to plain output.'
                      '  pip install rich', file=sys.stderr)
            self._impl = _PlainOutput()

    def __enter__(self) -> PipelineOutput:
        self._impl.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self._impl.__exit__(*exc)

    def set_columns(self, cols: list[str]) -> None:
        """Set column headers for the status table."""
        self._impl.set_columns(cols)

    def update_row(self, key: str, values: list[str]) -> None:
        """Insert or update a row in the status table (key = first column)."""
        self._impl.update_row(key, values)

    def set_subtitle(self, text: str) -> None:
        """Short subtitle shown next to the title (e.g. version info)."""
        self._impl.set_subtitle(text)

    def log(self, msg: str) -> None:
        """Emit a log line (timestamped in plain mode, appended to panel in TUI)."""
        self._impl.log(msg)

    def print_final_table(self) -> None:
        """Print a clean summary table (called automatically on context exit)."""
        self._impl.print_final_table()

    def initialize_releases(self, releases: dict) -> None:
        """Process mode: pre-populate progress bars for all releases."""
        if hasattr(self._impl, 'initialize_releases'):
            self._impl.initialize_releases(releases)

    def update_release(self, key: str, entry: dict) -> None:
        """Process mode: update a release from its full entry dict."""
        if hasattr(self._impl, 'update_release'):
            self._impl.update_release(key, entry)
        else:
            state = entry.get('state', '')
            branch = entry.get('branch', '')
            self._impl.update_row(key, [key, branch, state])
