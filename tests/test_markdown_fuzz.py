import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.quality
def test_streamed_markdown_matches_a_full_parse():
    subprocess.run(["node", "tests/markdown_stream_fuzz.mjs"], cwd=ROOT, check=True)
