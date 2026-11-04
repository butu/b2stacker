"""Progress (substeps from the script, throttled log) and preflight checks (processes, Windows paths)."""
import os
import subprocess
import sys

import pytest

from astrostack import engines, staging
from astrostack.engines import siril
from astrostack.engines.pixinsight import StackError

from .test_siril_run import cli, make_plan, settings  # noqa: F401 – Fixture

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Prozesse/Shell nur POSIX")

SCRIPT = """requires 1.4.0
setext fit
cd "/o/raw/flat"
convert flat "-out=/o/seq_flat"
cd "/o/seq_flat"
stack flat_ rej w 3 3 -nonorm -32b -out=master_flat
cd "/o/raw/light"
convert light "-out=/o/seq_light"
cd "/o/seq_light"
calibrate light_ "-flat=/o/seq_flat/master_flat.fit" -prefix=pp_
register pp_light_ -2pass -minpairs=10 -transf=homography -maxstars=2000
seqapplyreg pp_light_ -interp=lanczos4 -prefix=r_
stack r_pp_light_ rej w 3 3 -norm=addscale -weight=wfwhm -output_norm -32b -out=result
"""


def test_clean_line_strips_terminal_codes_and_keeps_last_progress_state():
    assert siril.clean_line("\x1b[A\x1b[2KTprogress: 40.00%\r\n") == "progress: 40.00%"
    assert siril.clean_line("10%\r20%\r30%\r") == "30%"
    assert siril.clean_line("[A[2Kprogress: 100.00%") == "progress: 100.00%"
    assert siril.clean_line("log: Running command: stack\n") == "log: Running command: stack"


def test_phases_follow_script_order_with_totals():
    subs = siril.phases_from_script(SCRIPT, n=5, n_flats=3)
    assert [s["key"] for s in subs] == ["master_flat", "cal_light", "register", "integrate"]
    assert subs[0]["total"] == 0 and subs[1]["total"] == 5 and subs[3]["total"] == 5


def test_substep_states_follow_command_index():
    subs = siril.phases_from_script(SCRIPT, n=5, n_flats=3)
    early = siril.substep_view(subs, cmd_idx=5, pct=None)          # master flat is stacked (line 5)
    assert early[0]["state"] == "now" and early[0]["key"] == "master_flat"
    mid = siril.substep_view(subs, cmd_idx=9, pct=40.0)           # lights calibrated, 40 %
    assert [s["state"] for s in mid] == ["done", "now"] and mid[1]["done"] == 2
    late = siril.substep_view(subs, cmd_idx=12, pct=None)
    assert [s["state"] for s in late] == ["done", "done", "done", "now"]


def test_script_builds_expected_phases(tmp_path):
    spec = dict(outdir=str(tmp_path), lights=str(tmp_path), flat=dict(raw=str(tmp_path), prefix="flat"),
                bias=None, dark=None, osc=False, cosmetic=False)
    subs = siril.phases_from_script(siril.build_script(spec), n=4, n_flats=3)
    keys = [s["key"] for s in subs]
    assert keys[0] == "cal_flat" and "master_flat" in keys and keys[-1] == "integrate" and "cal_light" in keys


@posix_only
@pytest.mark.real_siril_processes
def test_running_siril_process_is_seen_and_blocks_start(tmp_path, cli):
    dummy = subprocess.Popen(["bash", "-c", "exec -a siril-cli sleep 60"])
    try:
        assert any(p["pid"] == dummy.pid for p in siril.siril_processes())
        plan, _, _ = make_plan(tmp_path)
        st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
        assert st.state == "failed" and "Siril läuft bereits" in st.error
    finally:
        dummy.kill()
        dummy.wait()


@posix_only
def test_own_process_is_not_reported(tmp_path):
    assert all(p["pid"] != os.getpid() for p in siril.siril_processes())


def test_windows_long_path_estimate_and_block(tmp_path, cli, monkeypatch):
    long_base = "C:\\" + "x" * 230
    assert siril.siril_max_path(long_base) > 256
    assert siril.siril_max_path("D:\\b2") < 236
    monkeypatch.setattr(staging, "long_paths_enabled", lambda: False)
    monkeypatch.setattr(siril, "siril_max_path", lambda outdir: 300)
    plan, _, _ = make_plan(tmp_path)
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "failed" and "256" in st.error and "lange Pfade" in st.error


@posix_only
def test_chatty_output_is_throttled_but_complete_in_siril_log(tmp_path, cli, monkeypatch):
    monkeypatch.setenv("FAKESIRIL_CHATTY", "60")
    plan, _, _ = make_plan(tmp_path)
    lines = []
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda m: lines.append(m), open_results=False)
    assert st.state == "done", st.error
    shown = [m for m in lines if "chatty output" in m]
    log_file = os.path.join(os.path.dirname(st.runs["L"]["master"]), "logs", "siril.log")
    text = open(log_file, encoding="utf-8").read().splitlines()
    commands = sum(ln.startswith("log: Running command") for ln in text)
    assert 0 < len(shown) <= siril.LOG_LINES_PER_COMMAND * commands    # at most 20 lines per command
    assert len(shown) < 60 * commands                                   # throttled, not everything
    assert any("unterdrückt" in m for m in lines)
    assert sum("chatty output" in ln for ln in text) >= 60 * commands - 60   # siril.log complete
