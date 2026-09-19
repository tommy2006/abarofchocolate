import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A developer's .env holds real credentials (Anthropic key, mail server with an API key as password). Tests must never
# read them: the e-mail tests would really send, a hybrid-profile test would really call the external model.
# Set before tpm.config is imported; inherited by the CLI subprocesses the tests start.
os.environ["TPM_NO_DOTENV"] = "1"
_REAL_WORLD_ENV = ("ANTHROPIC_API_KEY", "TPM_PROFILE", "TPM_EXTERNAL_BASE_URL", "TPM_EXTERNAL_MODEL",
                   "TPM_SMTP_HOST", "TPM_SMTP_PORT", "TPM_SMTP_USER", "TPM_SMTP_PASSWORD", "TPM_SMTP_FROM")


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Same protection for variables exported in the shell rather than in .env; tests set what they need."""
    for var in _REAL_WORLD_ENV:
        monkeypatch.delenv(var, raising=False)


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
