import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES_DIR = os.path.join(REPO_ROOT, "samples")

# src/ holds flat top-level packages (extraction/, validation/, matching/,
# decision/, providers/, schemas/, utils/) rather than one wrapped package,
# so it must be on sys.path for `pip install -e .` to not be a hard
# requirement when just running pytest directly.
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))


# NOTE: auto-regeneration of samples/ via scripts/generate_sample_data.py is
# deliberately disabled right now -- samples/ and data/supplier_master.csv
# are being rebuilt around a new, real-document test set (see git history /
# conversation) instead of the old synthetic fixtures that script produces.
# Re-enable by restoring the old fixture body once the new test set +
# matching eval cases are in place, or if you want the old synthetic
# fixtures back as a fallback.
@pytest.fixture(scope="session", autouse=True)
def ensure_sample_data():
    pass


@pytest.fixture
def samples_dir():
    return SAMPLES_DIR


@pytest.fixture
def supplier_master_path():
    return os.path.join(REPO_ROOT, "data", "supplier_master.csv")
