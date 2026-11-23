import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="fph_test_"))
os.environ["FPH_DATA_DIR"] = str(_TMP / "data")
os.environ["FPH_EXPORT_DIR"] = str(_TMP / "exports")
os.environ["FPH_WAREHOUSE"] = str(_TMP / "warehouse.duckdb")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(scope="session")
def manifest():
    from fph import pipeline
    return pipeline.run(verbose=False)


@pytest.fixture(scope="session")
def con(manifest):
    from fph import warehouse
    c = warehouse.connect(read_only=True)
    yield c
    c.close()
