import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Application Files"))
os.environ["INAB_TODAY"] = "2026-10-12"


@pytest.fixture
def app(tmp_path):
    """A BudgetService on a throwaway folder, with its own Downloads folder."""
    from db import Database
    from service import BudgetService
    from settings import Settings

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    settings = Settings(tmp_path / "settings.json")
    settings.update({"downloads_dir": str(downloads)})
    service = BudgetService(Database(tmp_path / "budget.db"), settings, tmp_path / "data")
    service.test_downloads = downloads
    return service
