# Agent guide

## Development and verification
- Run commands from the repository root in an activated Python virtualenv. Python >=3.10 is required; Windows CI uses 3.12. Install with `python -m pip install -e '.[dev]'`.
- Full suite: `python -m pytest -q`. Focused report pipeline: `python -m pytest -q tests/test_e2e.py::test_report_end_to_end`.
- After editing either HTML UI, run `python -m pytest -q tests/test_ui_syntax.py`. Node.js must be on PATH; otherwise these JavaScript syntax checks are skipped.
- Tests synthesize FITS data in temporary directories. `tests/fake_pixinsight.py` (`ProcRunner`) runs a fake PixInsight as a real subprocess with the real command line and mimics WBPP quirks (comma splitting, command-line limit, header-only frame types, `ASFLAT` groups, aborted integration with <3 raw calibration frames). Add regressions from user reports to `tests/test_stack_scenarios.py`. Passing tests still do not verify real WBPP; the in-app self-test does (`<workdir>/selbsttest/bericht-*.json`). `tests/conftest.py` disables update checks via `B2_NO_UPDATE_CHECK=1`.
- `.github/workflows/tests-windows.yml` runs the suite on Windows for every non-main push (including a real UNC share). Push a feature branch and check it with `gh run list --branch <branch>` before merging.
- No configured lint, formatter, typecheck, or separate npm build/test pipeline exists; pytest also owns the JavaScript syntax checks.
- Headless UI: `python -m astrostack gui --no-browser --port 18765`; inspect `http://127.0.0.1:18765/api/state`. Without arguments, the entrypoint opens a desktop window, not CLI help.
- For isolated manual runs, set `ASTROSTACK_SETTINGS` to a temporary settings JSON with a temporary `workdir`; the settings override alone does not redirect project output. Set `B2_NO_UPDATE_CHECK=1` to avoid GitHub update checks.
- Debug with `python -m astrostack logs --kind error --trace`; logs live under `<workdir>/logs/`, with per-run files under `<workdir>/projects/<target>/runs/<timestamp>/`.

## Wiring and data safety
- Both console scripts (`astrostack`, `b2stacker`) and module execution enter `astrostack/__main__.py`; commands dispatch through `cli.py`. Global `--json` goes **before** the subcommand.
- `project.py` orchestrates scan → analysis/calibration → report/plan; `app/server.py` exposes this through a standard-library HTTP server. `jobs.py` serializes background jobs. Keep CLI and UI on the shared project/engine logic.
- `astrostack/app/index.html` is the application; `astrostack/report/template.html` is the generated report template. Both contain inline JavaScript. Chart.js is vendored for offline reports; there is no frontend bundler.
- Runtime projects live in `Settings.workdir` (outside the repo by default). `plan_edits.json` and `overrides.json` preserve user choices across reanalysis; do not replace them with regenerated `plan.json` or scan results.
- All file staging for PixInsight goes through `staging.prepare()` (comma/quote paths, overlong command lines, `ASFLAT` flat groups): symlink → hardlink → cached copy in `cache/staged/`; directory junctions only on local volumes. Never rename, move or modify original data. Preserve `remove_link_tree()` semantics: cleanup must unlink staged entries, never recurse into original data.
- WBPP automation uses combined debayer output (`debayerOutputMethod=0`); separated output is broken in this mode. Dualband splitting happens afterward through `pjsr/split.js`.
- User-facing documentation lives in `docs/ANLEITUNG.md` (linked from README); update it when UI behavior changes.
- Before changing WBPP behavior, read `docs/ARCHITEKTUR.md` and the relevant dated findings in `docs/ERKENNTNISSE.md`, then confirm against current engine code; the notes include historical/superseded approaches.

## Windows packaging and releases
- `.github/workflows/build-exe.yml` is the executable build/release reference. Windows packaging is not validated by Linux pytest.
- CI runs tests, **uninstalls `pytest` and `pytest-timeout`** to avoid the astropy PyInstaller hook failure, then runs `pyinstaller packaging/astrostack.spec --noconfirm` to produce `dist/b2stacker.exe`.
- New bundled HTML/JS resources must be covered by both `pyproject.toml` package-data and the explicit `datas` list in `packaging/astrostack.spec`.
- Keep versions in `astrostack/__init__.py` and `pyproject.toml` synchronized. Release tags are derived from `astrostack.__version__`, including tag-triggered builds.
- A push to `main` or a `v*` tag builds and publishes a GitHub release; reusing a version replaces its exe asset. Do not treat pushing `main` as a deploy-free operation.
