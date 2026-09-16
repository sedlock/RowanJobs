"""Configuration loading.

Configuration is a TOML file, defaulting to ``~/.config/rowanjobs/config.toml``.
Every key has a documented default, so the collector runs with no config file at
all. The *effective* configuration is hashed and stored with every run so that a
past run can be reproduced.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from .paths import Layout, default_config_file, default_data_root

SOURCE_BASE = "https://jobs.rowan.edu"
LISTING_PATH = "/en-us/listing/"


@dataclass(slots=True)
class NetworkConfig:
    """Source-traffic policy. One budget governs every live request."""

    concurrency: int = 1
    # 2.5 s comes from the 2026-09-16 source audit: roughly seven application
    # requests inside 90 seconds tripped the site's WAF challenge. A ~150-request
    # harvest at this pace takes about six minutes and stayed under the threshold.
    min_interval_seconds: float = 2.5
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 30.0
    write_timeout_seconds: float = 10.0
    pool_timeout_seconds: float = 10.0
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 120.0
    jitter_seconds: float = 0.75
    # After this many consecutive challenge/blocked responses the run stops
    # requesting rather than hammering the source.
    challenge_backoff_seconds: float = 60.0
    max_consecutive_challenges: int = 4
    # Hard ceiling on live requests per run. Protects the source and us.
    max_requests_per_run: int = 1200
    user_agent: str = (
        "RowanJobsArchiver/1.0 (+https://github.com/sedlock/RowanJobs; contact sedlock@rowan.edu)"
    )
    accept_language: str = "en-US,en;q=0.9"
    http2: bool = True
    max_response_bytes: int = 25 * 1024 * 1024
    max_resource_bytes: int = 50 * 1024 * 1024
    max_redirects: int = 5
    allowed_hosts: tuple[str, ...] = (
        "jobs.rowan.edu",
        "careers-static.pageuppeople.com",
    )
    # Hosts whose documents count as job-specific resources worth archiving.
    resource_hosts: tuple[str, ...] = ("jobs.rowan.edu",)


@dataclass(slots=True)
class BrowserConfig:
    """Browser fallback.

    Used only to satisfy an AWS WAF *challenge* the way an ordinary public
    visitor's browser does, and only when plain HTTP has been challenged. No
    stealth plugins, no proxy rotation, no CAPTCHA solving. See
    docs/SECURITY.md.
    """

    enabled: bool = True
    channel: str | None = None
    headless: bool = True
    nav_timeout_seconds: float = 45.0
    # Reuse the issued token for this long before re-solving.
    token_ttl_seconds: float = 1800.0
    max_solves_per_run: int = 6


@dataclass(slots=True)
class CollectionConfig:
    base_url: str = SOURCE_BASE
    listing_path: str = LISTING_PATH
    locale: str = "en-us"
    page_items: int = 20
    # Safety bound on pagination. Hitting it marks the scan unqualified.
    max_listing_pages: int = 200
    scope_label: str = "all-unfiltered"
    # Bump when the collected scope changes; absence history is only comparable
    # within one comparability group.
    comparability_group: str = "v1-unfiltered-en-us"
    verification_scan: bool = True
    reconciliation_scan: bool = True
    collect_resources: bool = True
    # Historical URLs that have been terminal this many times move to weekly.
    terminal_observations_before_weekly: int = 3
    weekly_recheck_interval_days: int = 7
    max_historical_rechecks_per_run: int = 120


@dataclass(slots=True)
class ScheduleConfig:
    hour: int = 6
    minute: int = 15
    timezone: str = "America/New_York"
    max_retries_per_slot: int = 2


@dataclass(slots=True)
class BackupConfig:
    enabled: bool = True
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 12
    verify_after_backup: bool = True
    # Full restore-and-query verification cadence.
    restore_check_interval_days: int = 7
    # Off-host destination. Empty means "no off-host protection configured".
    offhost_kind: str = ""  # '', 'rclone', 'rsync-ssh', 'command'
    offhost_target: str = ""
    offhost_namespace: str = "rowanjobs"
    offhost_command: tuple[str, ...] = ()


@dataclass(slots=True)
class NotifyConfig:
    """Operational alerting.

    Left unconfigured on purpose: RowanJobs will not borrow another
    application's credentials or invent a recipient. When ``kind`` is empty the
    health output reports notifications as UNCONFIGURED and nothing is sent.
    """

    kind: str = ""  # '', 'command'
    command: tuple[str, ...] = ()
    notify_on: tuple[str, ...] = ("failed", "partial")


@dataclass(slots=True)
class Config:
    data_root: Path = field(default_factory=default_data_root)
    db_path: Path | None = None
    network: NetworkConfig = field(default_factory=NetworkConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    config_path: Path | None = None

    @property
    def layout(self) -> Layout:
        return Layout(self.data_root, self.db_path)

    @property
    def listing_url(self) -> str:
        return self.collection.base_url.rstrip("/") + self.collection.listing_path

    def start_urls(self) -> list[str]:
        return [self.listing_url]

    def effective_dict(self) -> dict[str, Any]:
        """Only the parts that change what a run *attempted*.

        Local paths are excluded so the same collection policy hashes the same
        on a restore host.
        """
        return {
            "network": _clean(asdict(self.network)),
            "browser": _clean(asdict(self.browser)),
            "collection": _clean(asdict(self.collection)),
            "schedule": _clean(asdict(self.schedule)),
        }

    def config_hash(self) -> str:
        blob = json.dumps(self.effective_dict(), sort_keys=True, separators=(",", ":"))
        return sha256(blob.encode("utf-8")).hexdigest()

    def retrieval_policy(self) -> dict[str, Any]:
        return _clean(asdict(self.network))


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, tuple):
            out[k] = list(v)
        elif isinstance(v, Path):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _apply(section: Any, values: dict[str, Any], where: str) -> None:
    known = set(section.__slots__)
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"unknown configuration key [{where}] {key!r}")
        current = getattr(section, key)
        if isinstance(current, tuple):
            value = tuple(value)
        setattr(section, key, value)


def load_config(path: Path | None = None) -> Config:
    """Load configuration. A missing file is not an error."""
    cfg_path = Path(path).expanduser() if path else default_config_file()
    cfg = Config(config_path=cfg_path if cfg_path.exists() else None)
    if not cfg_path.exists():
        return cfg

    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)

    if "data_root" in raw:
        cfg.data_root = Path(raw["data_root"]).expanduser()
    if "db_path" in raw:
        cfg.db_path = Path(raw["db_path"]).expanduser()

    for name, section in (
        ("network", cfg.network),
        ("browser", cfg.browser),
        ("collection", cfg.collection),
        ("schedule", cfg.schedule),
        ("backup", cfg.backup),
        ("notify", cfg.notify),
    ):
        if name in raw and not isinstance(raw[name], dict):
            raise ValueError(f"[{name}] must be a table")
        if name in raw:
            _apply(section, raw[name], name)

    unknown = set(raw) - {
        "data_root",
        "db_path",
        "network",
        "browser",
        "collection",
        "schedule",
        "backup",
        "notify",
    }
    if unknown:
        raise ValueError(f"unknown configuration table(s): {sorted(unknown)}")
    return cfg
