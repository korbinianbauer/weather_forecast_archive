from .awekas import AwekasProvider
from .base import ForecastEntry, LocationResult, WeatherProvider
from .dwd import DwdProvider
from .lwd_bayern import LwdBayernProvider
from .meteoblue import MeteoblueProvider
from .tirol_smet import HdTirolProvider, LwdTirolProvider
from .wetter_com import WetterComProvider
from .wetteronline import WetterOnlineProvider

_all: list[WeatherProvider] = [
    WetterComProvider(),
    MeteoblueProvider(),
    WetterOnlineProvider(),
    DwdProvider(),
    LwdBayernProvider(),
    LwdTirolProvider(),
    HdTirolProvider(),
    AwekasProvider(),
]

REGISTRY: dict[str, WeatherProvider] = {p.name: p for p in _all}


def get(name: str) -> WeatherProvider:
    return REGISTRY[name]


def all_providers() -> list[WeatherProvider]:
    return list(REGISTRY.values())
