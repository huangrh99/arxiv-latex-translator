import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_token_summary_uses_compact_units_and_two_decimal_places() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    formatter = Path(__file__).parents[1] / "arxiv_latex_translator" / "static" / "formatters.js"
    job = {
        "total_tokens": 2451950,
        "input_tokens": 2393512,
        "cached_input_tokens": 1903232,
        "output_tokens": 58438,
        "reasoning_output_tokens": 29511,
        "cost_usd": 1.755263,
        "pricing_model": "gpt-6.2-sol",
    }
    script = f"""
const fs = require('fs');
const vm = require('vm');
const context = {{window: {{}}}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(formatter))}, 'utf8'), context);
process.stdout.write(context.window.arxivTokenSummary({json.dumps(job)}));
"""
    result = subprocess.run([node], input=script, text=True, capture_output=True, check=True)
    summary = result.stdout
    assert "2.45M" in summary
    assert "490.28K" in summary
    assert "$1.76 USD" in summary
    assert "2451950" not in summary
    assert "2393512" not in summary
