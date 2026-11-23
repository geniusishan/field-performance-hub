from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app" / "dashboard.py")


def test_dashboard_renders_without_errors(manifest):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, at.exception
    assert len(at.tabs) == 5
    assert any("YTD signings" in m.label for m in at.metric)
