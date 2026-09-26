from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values, find_dotenv

ENV_PREFIX = "SEARCHIQS_"
BASE_URL = "https://www.searchiqs.com/CTASH/"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    output_dir: Path = Path("output")
    delay_min: float = 1.0
    delay_max: float = 3.0
    connect_timeout: float = 15.0
    request_timeout: float = 60.0
    max_redirects: int = 5
    read_attempts: int = 3
    max_pages: int = 1000
    run_deadline: float = 7200.0
    backoff_base: float = 2.0
    backoff_cap: float = 60.0
    google_credentials: Path | None = None
    google_sheet_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("delay_min", "delay_max", "connect_timeout", "request_timeout", "run_deadline",
                     "backoff_base", "backoff_cap"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigError(f"{name} must be a positive finite number, got {value!r}")
        for name in ("max_redirects", "read_attempts", "max_pages"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be at least 1")
        if self.delay_min > self.delay_max:
            raise ConfigError("delay_min must not exceed delay_max")
        if self.backoff_base > self.backoff_cap:
            raise ConfigError("backoff_base must not exceed backoff_cap")
        if self.connect_timeout > self.request_timeout:
            raise ConfigError("connect_timeout must not exceed request_timeout")

    def require_sheets(self) -> None:
        missing = [n for n, v in (("GOOGLE_SERVICE_ACCOUNT_FILE", self.google_credentials),
                                  ("GOOGLE_SHEET_ID", self.google_sheet_id)) if not v]
        if missing:
            raise ConfigError(f"missing for Google Sheets export: {', '.join(missing)} (or use --local-only)")
        if not self.google_credentials.is_file():
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_FILE does not point to a file")

    def redacted(self) -> dict[str, object]:
        data = asdict(self)
        data["output_dir"] = str(self.output_dir)
        data["google_credentials"] = "set" if self.google_credentials else "unset"
        data["google_sheet_id"] = "set" if self.google_sheet_id else "unset"
        return data


_FLOAT_VARS = {
    "DELAY_MIN": "delay_min",
    "DELAY_MAX": "delay_max",
    "CONNECT_TIMEOUT": "connect_timeout",
    "REQUEST_TIMEOUT": "request_timeout",
    "RUN_DEADLINE": "run_deadline",
    "BACKOFF_BASE": "backoff_base",
    "BACKOFF_CAP": "backoff_cap",
}
_INT_VARS = {
    "MAX_REDIRECTS": "max_redirects",
    "READ_ATTEMPTS": "read_attempts",
    "MAX_PAGES": "max_pages",
}


def load_config(env: Mapping[str, str] | None = None) -> Config:
    if env is None:
        file_values = {k: v for k, v in dotenv_values(find_dotenv(usecwd=True)).items() if v is not None}
        env = {**file_values, **os.environ}
    kwargs: dict[str, object] = {}
    for suffix, attr in _FLOAT_VARS.items():
        if raw := env.get(ENV_PREFIX + suffix, "").strip():
            kwargs[attr] = _parse(raw, float, ENV_PREFIX + suffix)
    for suffix, attr in _INT_VARS.items():
        if raw := env.get(ENV_PREFIX + suffix, "").strip():
            kwargs[attr] = _parse(raw, int, ENV_PREFIX + suffix)
    if raw := env.get(ENV_PREFIX + "OUTPUT_DIR", "").strip():
        kwargs["output_dir"] = Path(raw)
    if raw := env.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip():
        kwargs["google_credentials"] = Path(raw)
    if raw := env.get("GOOGLE_SHEET_ID", "").strip():
        kwargs["google_sheet_id"] = raw
    return Config(**kwargs)


def _parse(raw: str, kind: type, name: str) -> float | int:
    try:
        return kind(raw)
    except ValueError:
        raise ConfigError(f"{name} must be {'an integer' if kind is int else 'a number'}, got {raw!r}") from None
