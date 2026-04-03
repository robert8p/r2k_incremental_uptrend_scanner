from __future__ import annotations

from typing import Callable, Optional

from app.config import Settings, load_settings
from app.db import Database
from app.repositories import RepositoryBundle
from app.services.alpaca_client import AlpacaClient


class AppRuntime:
    def __init__(
        self,
        *,
        settings_loader: Callable[[], Settings] = load_settings,
        db_factory: Callable[[str], Database] = Database,
        alpaca_factory: Callable[[Settings], AlpacaClient] = AlpacaClient,
        initial_settings: Optional[Settings] = None,
    ):
        self._settings_loader = settings_loader
        self._db_factory = db_factory
        self._alpaca_factory = alpaca_factory
        self.settings = initial_settings or self._settings_loader()
        self.db = self._db_factory(self.settings.database_path)
        self.repositories = RepositoryBundle(self.db)
        self.alpaca = self._alpaca_factory(self.settings)
        self.scheduler_status = {
            'leader': False,
            'scheduler_running': False,
            'mode': 'not_initialized',
        }

    def refresh(self) -> Settings:
        new_settings = self._settings_loader()
        if getattr(self.db, 'path', None) != new_settings.database_path:
            self.db = self._db_factory(new_settings.database_path)
            self.repositories = RepositoryBundle(self.db)
        self.settings = new_settings
        self.alpaca = self._alpaca_factory(self.settings)
        return self.settings
