from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class ForecastEntry:
    """
    Common archive format for all providers and both granularities.

    Log only what the provider explicitly supplies — leave everything else as
    None. Use `temperature` for a single point-in-time value (e.g. hourly),
    `temp_max` / `temp_min` for daily extremes.
    """
    forecast_date: str                          # YYYY-MM-DD
    granularity: str                            # 'daily' | 'hourly'
    forecast_hour: Optional[int] = None         # 0-23; None for daily entries
    condition_text: Optional[str] = None
    icon_url: Optional[str] = None
    temperature: Optional[float] = None         # °C — single representative value (e.g. hourly)
    temp_max: Optional[float] = None            # °C — daily maximum
    temp_min: Optional[float] = None            # °C — daily minimum
    precip_probability: Optional[int] = None    # %
    precip_amount: Optional[float] = None       # mm
    wind_direction: Optional[str] = None
    wind_speed: Optional[int] = None            # km/h
    cloud_cover: Optional[int] = None           # %
    pressure: Optional[float] = None            # hPa
    humidity: Optional[int] = None              # %

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LocationResult:
    """A candidate location returned by a provider search."""
    name: str
    provider_location_id: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    extra: dict = field(default_factory=dict)   # provider-specific metadata


class WeatherProvider(ABC):
    """
    Abstract base for weather forecast providers.

    Subclass this, set `name` / `display_name` / `supports_*`, implement
    `search` and whichever fetch methods the source provides, then register
    the instance in providers/__init__.py.
    """
    name: str           # stable slug used as DB key, e.g. 'wetter_com'
    display_name: str   # shown in UI, e.g. 'Wetter.com'

    supports_daily: bool = True
    supports_hourly: bool = False

    @abstractmethod
    def search(self, query: str) -> list[LocationResult]:
        """Return up to ~10 candidates matching *query*."""
        ...

    def fetch_daily(
        self,
        provider_location_id: str,
        extra: dict,
    ) -> list[ForecastEntry]:
        """Return up to 14 ForecastEntry objects with granularity='daily'."""
        raise NotImplementedError

    def fetch_hourly(
        self,
        provider_location_id: str,
        extra: dict,
    ) -> list[ForecastEntry]:
        """Return ForecastEntry objects with granularity='hourly' for ≥1 day."""
        raise NotImplementedError
