"""
Unit tests for caupdate/tui.py

Tests cover both plain-mode (_PlainOutput) and the public PipelineOutput
interface. Rich TUI (_RichOutput) rendering is tested without a live terminal
by checking the data structures, not the visual output.
"""

import sys
import time
import threading
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import caupdate.tui as tui


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_plain():
    return tui.PipelineOutput(human=False, title='test')

def _make_rich():
    return tui.PipelineOutput(human=True, title='test')


# ══════════════════════════════════════════════════════════════════════════════
# PipelineOutput — plain mode
# ══════════════════════════════════════════════════════════════════════════════

class TestPipelineOutputPlain:
    def test_context_manager(self):
        out = _make_plain()
        with out:
            pass   # must not raise

    def test_log_prints_to_stdout(self, capsys):
        out = _make_plain()
        with out:
            out.log('hello world')
        captured = capsys.readouterr().out
        assert 'hello world' in captured

    def test_log_includes_timestamp(self, capsys):
        out = _make_plain()
        with out:
            out.log('message')
        captured = capsys.readouterr().out
        # HH:MM:SS format
        import re
        assert re.search(r'\d{2}:\d{2}:\d{2}', captured)

    def test_set_columns_does_not_crash(self):
        out = _make_plain()
        out.set_columns(['A', 'B', 'C'])   # no-op in plain mode

    def test_update_row_does_not_crash(self):
        out = _make_plain()
        out.update_row('key', ['a', 'b', 'c'])   # no-op in plain mode

    def test_set_subtitle_does_not_crash(self):
        out = _make_plain()
        out.set_subtitle('NSS 3.101')

    def test_print_final_table_does_not_crash(self):
        out = _make_plain()
        out.print_final_table()   # no-op in plain mode

    def test_thread_safe_log(self, capsys):
        out = _make_plain()
        errors = []
        def _log(i):
            try:
                out.log(f'thread {i}')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_log, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ══════════════════════════════════════════════════════════════════════════════
# PipelineOutput — falls back when rich not installed
# ══════════════════════════════════════════════════════════════════════════════

class TestPipelineOutputFallback:
    def test_falls_back_when_rich_missing(self, capsys):
        with patch.object(tui, '_RICH', False):
            out = tui.PipelineOutput(human=True, title='test')
            # Should be a _PlainOutput despite human=True
            assert isinstance(out._impl, tui._PlainOutput)

    def test_fallback_prints_warning(self, capsys):
        with patch.object(tui, '_RICH', False):
            tui.PipelineOutput(human=True, title='test')
        assert 'WARNING' in capsys.readouterr().err


# ══════════════════════════════════════════════════════════════════════════════
# _state_style
# ══════════════════════════════════════════════════════════════════════════════

class TestStateStyle:
    def test_complete_is_green(self):
        assert 'green' in tui._state_style('complete')

    def test_failed_is_red(self):
        style = tui._state_style('centos ci failed')
        assert 'red' in style

    def test_waiting_is_yellow(self):
        style = tui._state_style('waiting centos ci')
        assert 'yellow' in style

    def test_planned_is_cyan(self):
        assert 'cyan' in tui._state_style('planned')

    def test_unknown_state_is_white(self):
        assert tui._state_style('unknown-random-state') == 'white'

    def test_empty_falls_through_to_white(self):
        # empty string matches no key → falls through to white
        assert tui._state_style('') == 'white'


# ══════════════════════════════════════════════════════════════════════════════
# _RichOutput data layer (without live terminal rendering)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not tui._RICH, reason='rich not installed')
class TestRichOutputData:
    def _make(self):
        return tui._RichOutput(title='test-title')

    def test_update_row_stores_values(self):
        r = self._make()
        r.update_row('rhel-10.3', ['rhel-10.3', 'GA', 'RHEL-200', 'planned'])
        assert r._rows['rhel-10.3'] == ['rhel-10.3', 'GA', 'RHEL-200', 'planned']

    def test_update_row_preserves_order(self):
        r = self._make()
        r.update_row('rhel-10.3', ['a'])
        r.update_row('rhel-9.9.0', ['b'])
        r.update_row('rhel-8.10.0', ['c'])
        assert r._row_order == ['rhel-10.3', 'rhel-9.9.0', 'rhel-8.10.0']

    def test_update_row_overwrites_existing(self):
        r = self._make()
        r.update_row('rhel-10.3', ['old'])
        r.update_row('rhel-10.3', ['new'])
        assert r._rows['rhel-10.3'] == ['new']
        assert r._row_order.count('rhel-10.3') == 1   # not duplicated

    def test_set_columns(self):
        r = self._make()
        r.set_columns(['Release', 'Bug', 'State'])
        assert r._columns == ['Release', 'Bug', 'State']

    def test_set_subtitle(self):
        r = self._make()
        r.set_subtitle('NSS 3.101 · Firefox 153')
        assert r._subtitle == 'NSS 3.101 · Firefox 153'

    def test_log_appends_with_timestamp(self):
        r = self._make()
        r.log('test message')
        last = list(r._log)[-1]
        import re
        assert re.search(r'\d{2}:\d{2}:\d{2}', last)
        assert 'test message' in last

    def test_log_buffer_max(self):
        r = self._make()
        for i in range(300):
            r.log(f'line {i}')
        assert len(r._log) <= 200   # maxlen=200

    def test_thread_safe_update(self):
        r = self._make()
        errors = []
        def _write(i):
            try:
                r.update_row(f'rel-{i}', [f'val-{i}'])
                r.log(f'message {i}')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []

    def test_context_manager_starts_and_stops(self):
        r = self._make()
        # Mock the Live class to avoid terminal output
        with patch('caupdate.tui.Live') as mock_live_cls:
            mock_live = mock_live_cls.return_value.__enter__.return_value
            with r:
                pass   # enter and exit cleanly
            mock_live_cls.return_value.__exit__.assert_called_once()

    def test_print_final_table_no_crash_empty(self):
        r = self._make()
        r.print_final_table()   # empty rows — should not crash

    def test_print_final_table_with_rows(self, capsys):
        r = self._make()
        r.set_columns(['Release', 'State'])
        r.update_row('rhel-10.3', ['rhel-10.3', 'complete'])
        r.print_final_table()
        # Rich prints a table — just check it doesn't crash

    def test_render_table_has_rows(self):
        r = self._make()
        r.set_columns(['R', 'S'])
        r.update_row('rhel-10.3', ['rhel-10.3', 'planned'])
        table = r._render_table()
        assert table.row_count == 1

    def test_render_table_multiple_rows(self):
        r = self._make()
        r.set_columns(['R', 'S'])
        for i in range(5):
            r.update_row(f'rel-{i}', [f'rel-{i}', 'staged'])
        table = r._render_table()
        assert table.row_count == 5
