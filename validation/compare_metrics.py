"""Check unchanged metric calculations against the exact upstream revision."""
import ast
import importlib.util
from pathlib import Path
import subprocess
import sys
import numpy as np
import pandas as pd
import xarray as xr
from googlehydrology.evaluation import metrics

source, base, evidence = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
file = "googlehydrology/evaluation/metrics.py"
before = subprocess.check_output(["git", "show", base + ":" + file], cwd=source).decode()
after = (source / file).read_text()
skip = {"get_available_metrics", "calculate_metrics", "calculate_all_metrics"}
def formulas(text):
    return {node.name: ast.dump(node, include_attributes=False)
            for node in ast.parse(text).body
            if isinstance(node, ast.FunctionDef) and node.name not in skip}
assert formulas(before) == formulas(after)
print("Unchanged calculation/helper functions:", len(formulas(before)))
original = evidence / "upstream_metrics.py"
original.write_text(before)
spec = importlib.util.spec_from_file_location("upstream_metrics", original)
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)
comparisons = 0
for resolution in ("1D", "1h", "6h"):
    for dtype in (np.float32, np.float64):
        for gaps in (False, True):
            dates = pd.date_range("2020-01-01", periods=500, freq=resolution)
            rng = np.random.default_rng(23)
            t = np.arange(500)
            values = (10 + 3 * np.sin(t / 19) + rng.uniform(0, 0.2, 500)).astype(dtype)
            values[[100, 250, 400]] += np.array([50, 60, 70], dtype=dtype)
            obs = xr.DataArray(values, dims=["date"], coords={"date": dates})
            sim = xr.DataArray((0.9 * np.roll(values, 2) + 0.1).astype(dtype), dims=["date"], coords={"date": dates})
            if gaps:
                obs.data[20:24] = np.nan
                sim.data[22:26] = np.nan
            old = upstream.calculate_all_metrics(obs, sim, resolution=resolution)
            new = metrics.calculate_all_metrics(obs, sim, resolution=resolution)
            for key, value in old.items():
                np.testing.assert_equal(new[key], value)
                comparisons += 1
            # The previously supported explicitly-selected metric agrees too.
            expected = upstream.calculate_metrics(obs, sim, ["Missed-Peaks"], resolution=resolution)
            np.testing.assert_equal(new["Missed-Peaks"], expected["Missed-Peaks"])
print("Exact pre-existing all-metric comparisons:", comparisons)
