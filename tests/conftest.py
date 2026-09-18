import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_settings(tmp_path):
    from tpm.config import load_settings

    s = load_settings()
    s.workspace_dir = str(tmp_path / "workspace")
    s.detect.max_fit_rows = 20000
    s.detect.time_budget_s = 60
    s.detect.n_folds = 3
    return s


@pytest.fixture
def synth():
    from tests.fixtures.synth import make_synthetic

    return make_synthetic(n_groups=12, n_samples=200, seed=1)
