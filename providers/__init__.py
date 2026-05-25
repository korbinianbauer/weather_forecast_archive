from .base import ForecastEntry, LocationResult, WeatherProvider
from .meteoblue import MeteoblueProvider
from .wetter_com import WetterComProvider

_all: list[WeatherProvider] = [
    WetterComProvider(),
    MeteoblueProvider(),
]

REGISTRY: dict[str, WeatherProvider] = {p.name: p for p in _all}


def get(name: str) -> WeatherProvider:
    return REGISTRY[name]


def all_providers() -> list[WeatherProvider]:
    return list(REGISTRY.values())
