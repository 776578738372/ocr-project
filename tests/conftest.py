import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES_DIR = os.path.join(REPO_ROOT, "data", "sample_w9s")


@pytest.fixture(scope="session", autouse=True)
def ensure_sample_data():
    """Generates data/sample_w9s (and the supplier master) on first run so the
    test suite works from a fresh clone without a manual setup step."""
    if not os.path.isdir(SAMPLES_DIR) or not os.listdir(SAMPLES_DIR):
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        import generate_sample_data

        generate_sample_data.main()


@pytest.fixture
def samples_dir():
    return SAMPLES_DIR


@pytest.fixture
def supplier_master_path():
    return os.path.join(REPO_ROOT, "data", "sample_supplier_master.csv")
