"""Validate the exact source revisions, regressions, and installed packages."""
import collections
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

CASE = sys.argv[1]
HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "fixes.json").read_text())[CASE]
SOURCE = Path(sys.argv[2]).resolve()
EVIDENCE = Path(sys.argv[3]).resolve()
EVIDENCE.mkdir(parents=True, exist_ok=True)
ENV = os.environ.copy()
ENV.update(MPLBACKEND="Agg", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONHASHSEED="7", MKL_NUM_THREADS="1")
ENV.pop("PYTHONPATH", None)
PYTHON = sys.executable


def run(label, command, expected=0, cwd=SOURCE):
    try:
        result = subprocess.run(command, cwd=cwd, env=ENV, capture_output=True, text=True, timeout=720)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        output = (stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout) + (stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr)
        (EVIDENCE / (label + ".log")).write_text(output, encoding="utf-8")
        print(label, "TIMEOUT", output[-18000:], flush=True)
        raise
    output = result.stdout + result.stderr
    (EVIDENCE / (label + ".log")).write_text(output, encoding="utf-8")
    print(label, "exit", result.returncode, flush=True)
    if expected is not None and result.returncode != expected:
        print(output[-12000:], flush=True)
        raise AssertionError((label, result.returncode, expected))
    return result


def pytest(label, paths, total, failures=0, coverage=False):
    xml = EVIDENCE / (label + ".xml")
    cmd = [PYTHON, "-m", "pytest", "-vv", "-o", "faulthandler_timeout=180", "-m", "not gpu", *paths, "--junitxml", str(xml)]
    if coverage:
        cmd += ["--cov=googlehydrology", "--cov-branch", "--cov-report=json:" + str(EVIDENCE / "coverage.json")]
    result = run(label, cmd, expected=0 if failures == 0 else 1)
    tree = ET.parse(xml)
    counts = [sum(int(s.get(key, 0)) for s in tree.iter("testsuite")) for key in ("tests", "failures", "errors", "skipped")]
    failed = [tc for tc in tree.iter("testcase") if tc.find("failure") is not None]
    assert counts[0] == total and counts[2:] == [0, 0], counts
    if failures is not None:
        assert counts[1] == failures, counts
    print(label, "counts", counts, flush=True)
    return failed, result


actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
assert actual == CONFIG["source"], (actual, CONFIG["source"])
print("TESTED SOURCE", actual, flush=True)
run("import-location", [PYTHON, "-c", "import pathlib, googlehydrology; p=pathlib.Path(googlehydrology.__file__).resolve(); print(p); assert p.is_relative_to(pathlib.Path(" + repr(str(SOURCE)) + "))"])
if CASE == "metrics":
    run("metric-formula-comparison", [PYTHON, str(HERE / "compare_metrics.py"), str(SOURCE), CONFIG["base"], str(EVIDENCE)])
pytest("submitted-suite", ["test"], CONFIG["total"])
pytest("focused-coverage", [CONFIG["test"]], CONFIG["new_total"], coverage=True)
run("new-test-ruff", [PYTHON, "-m", "ruff", "check", CONFIG["test"]])
run("new-test-format", [PYTHON, "-m", "ruff", "format", "--check", CONFIG["test"]])
run("patch-check", ["git", "diff", "--check", CONFIG["base"], "HEAD"])
run("dependency-check", [PYTHON, "-m", "pip", "check"])
run("dependency-versions", [PYTHON, "-m", "pip", "freeze"])
files = CONFIG["production"]
original = {f: (SOURCE / f).read_bytes() for f in files}
try:
    for f in files:
        (SOURCE / f).write_bytes(subprocess.check_output(["git", "show", CONFIG["base"] + ":" + f], cwd=SOURCE))
    pytest("original-regressions", [CONFIG["test"]], CONFIG["new_total"],
           failures=CONFIG["original_failures"])

finally:
    for f, content in original.items():
        (SOURCE / f).write_bytes(content)

fixture = SOURCE / "test/test_multimet.py"
fixed_fixture = fixture.read_bytes()
try:
    fixture.write_bytes(subprocess.check_output(["git", "show", CONFIG["base"] + ":test/test_multimet.py"], cwd=SOURCE))
    _, result = pytest("original-fixture", ["test/test_multimet.py::test_multimet_dict_inputs_and_missing_band_validation"], 1, failures=1)
    assert "FileNotFoundError" in result.stdout and "basin_01.nc" in result.stdout, result.stdout
finally:
    fixture.write_bytes(fixed_fixture)
for f, content in original.items():
    assert (SOURCE / f).read_bytes() == content
assert fixture.read_bytes() == fixed_fixture
run("restoration-check", ["git", "diff", "--exit-code"])
pytest("restored-suite", ["test"], CONFIG["total"])

build_dir = EVIDENCE / "dist"
run("package-build", [PYTHON, "-m", "build", "--outdir", str(build_dir)])
wheel, = build_dir.glob("*.whl")
run("install-wheel", [PYTHON, "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheel)])
with tempfile.TemporaryDirectory(prefix="flood-wheel-tests-") as td:
    outside = Path(td)
    shutil.copy2(SOURCE / CONFIG["test"], outside / Path(CONFIG["test"]).name)
    shutil.copy2(SOURCE / "pyproject.toml", outside / "pyproject.toml")
    check = "import pathlib, googlehydrology; p=pathlib.Path(googlehydrology.__file__).resolve(); print(p); assert 'site-packages' in p.parts"
    run("installed-import", [PYTHON, "-c", check], cwd=outside)
    run("installed-regressions", [PYTHON, "-m", "pytest", "-q", str(outside / Path(CONFIG["test"]).name), "--junitxml", str(EVIDENCE / "installed.xml")], cwd=outside)
    counts = [sum(int(s.get(key, 0)) for s in ET.parse(EVIDENCE / "installed.xml").iter("testsuite")) for key in ("tests", "failures", "errors", "skipped")]
    assert counts == [CONFIG["new_total"], 0, 0, 0], counts
print("VALIDATION COMPLETE", CASE, actual, flush=True)
