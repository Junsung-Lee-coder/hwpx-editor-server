from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.api_auth import is_loopback_host


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='HWP_',
        extra='ignore',
        populate_by_name=True,
    )

    api_host: str = '127.0.0.1'
    api_port: int = 8765
    # Optional bearer token. Required whenever api_host is not a loopback address.
    api_token: str = Field(default='', repr=False)
    spool_root: Path = Path('./spool')
    pdftoppm_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices('HWP_PDFTOPPM', 'HWP_PDFTOPPM_PATH'),
    )
    source_manifest: Path | None = None
    api_task_name: str = 'hwpx-editor-api'
    worker_task_name: str = 'hwpx-editor-worker'
    allowed_extensions: str = '.hwpx,.hwp'
    max_upload_mb: int = 50
    poll_interval_seconds: int = 3
    job_timeout_seconds: int = 300
    job_stale_seconds: int = 900
    max_attempts: int = 2
    log_level: str = 'INFO'
    retention_days: int = 7
    worker_name: str = 'hwpx-worker'
    # Hancom security-module registration defaults.
    # These map to the registry-backed module alias that suppresses the file-path/security confirmation.
    security_module_dll: str = 'FilePathCheckDLL'
    security_module_name: str = 'FilePathCheckerModule'

    @field_validator('api_port')
    @classmethod
    def _validate_api_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError('api_port must be between 1 and 65535')
        return value

    @field_validator('api_token')
    @classmethod
    def _validate_api_token(cls, value: str) -> str:
        if value and (len(value) < 32 or not value.isascii() or any(c.isspace() for c in value)):
            raise ValueError('api_token must be at least 32 ASCII characters with no whitespace')
        return value

    @model_validator(mode='after')
    def _require_token_off_loopback(self) -> 'Settings':
        if not is_loopback_host(self.api_host) and not self.api_token:
            raise ValueError('HWP_API_TOKEN is required when HWP_API_HOST is not a loopback address')
        return self

    @field_validator('spool_root', mode='before')
    @classmethod
    def _coerce_spool_root(cls, value: object) -> Path:
        return Path(value)

    @field_validator('allowed_extensions', mode='before')
    @classmethod
    def _parse_allowed_extensions(cls, value: object) -> str:
        if isinstance(value, str):
            items = [item.strip().lower() for item in value.split(',') if item.strip()]
            return ','.join(items) if items else '.hwpx,.hwp'
        if isinstance(value, (list, tuple, set)):
            items = [str(item).strip().lower() for item in value if str(item).strip()]
            return ','.join(items) if items else '.hwpx,.hwp'
        return '.hwpx,.hwp'

    @property
    def allowed_extensions_list(self) -> tuple[str, ...]:
        items = [item.strip().lower() for item in self.allowed_extensions.split(',') if item.strip()]
        return tuple(items) if items else ('.hwpx', '.hwp')

    @property
    def jobs_root(self) -> Path:
        return self.spool_root / 'jobs'

    @property
    def logs_root(self) -> Path:
        return self.spool_root / 'logs'

    @property
    def db_path(self) -> Path:
        return self.spool_root / 'queue.db'

    def ensure_directories(self) -> None:
        self.spool_root.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
