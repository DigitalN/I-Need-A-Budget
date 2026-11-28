# SPDX-License-Identifier: GPL-3.0-only
import functools
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Application Files"))
os.environ["INAB_TODAY"] = "2026-10-12"


@pytest.fixture(autouse=True, scope="session")
def real_copy():
    """No test takes a real copy's lock or mutex. The real data folder's lock would keep the app
    from opening on that computer (the maintainer's real copy, on their Mac); while anything holds
    RUNNING_MUTEX, Windows' installer and uninstaller won't run. Windows copies made in a test hold
    a mutex of their own, and taking either real one fails the test. Its value is what a real copy
    uses, for the tests that check it."""
    import computer
    import computer_mac
    import computer_windows

    real = SimpleNamespace(lock=(computer.this().data_dir() / ".lock").resolve(),
                           mutex=computer_windows.Windows._running_mutex)

    def guard_lock(lock):
        @functools.wraps(lock)
        def guarded(self, path):
            assert Path(path).resolve() != real.lock, f"a test took the real copy's lock, {real.lock}"
            return lock(self, path)
        return guarded

    create_mutex = computer_windows._create_mutex

    @functools.wraps(create_mutex)
    def guarded_mutex(name):
        assert name != real.mutex, f"a test took the real copy's mutex, {name!r}"
        return create_mutex(name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(computer_windows.Windows, "_running_mutex", f"I Need A Budget tests {os.getpid()}")
        patch.setattr(computer_windows, "_create_mutex", guarded_mutex)
        for system in (computer_mac.Mac, computer_windows.Windows):
            patch.setattr(system, "lock", guard_lock(system.lock))
        yield real


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
