"""수집 플러그인 등록·탐색.

핵심 코드에 플러그인 import 를 추가하지 않도록, collectors.plugins 패키지를 자동 탐색한다.
"""
from __future__ import annotations

import importlib
import pkgutil

from collectors.core.interface import CollectorPlugin

_REGISTRY: dict[str, type[CollectorPlugin]] = {}
_discovered = False


class PluginNotFound(LookupError):
    pass


def register_collector(cls: type[CollectorPlugin]) -> type[CollectorPlugin]:
    """클래스 데코레이터. league.collector_key 와 같은 key 로 등록한다."""
    for attr in ("key", "sport_code", "source_code"):
        if not getattr(cls, attr, None):
            raise TypeError(f"{cls.__name__}.{attr} 가 필요합니다")
    existing = _REGISTRY.get(cls.key)
    if existing is not None and existing is not cls:
        raise ValueError(f"수집 플러그인 key 중복: {cls.key} ({existing.__name__}, {cls.__name__})")
    _REGISTRY[cls.key] = cls
    return cls


def discover_plugins(package: str = "collectors.plugins") -> None:
    """패키지 하위 모듈을 모두 import 해서 데코레이터 등록을 실행한다 (1회)."""
    global _discovered
    if _discovered:
        return
    pkg = importlib.import_module(package)
    for info in pkgutil.walk_packages(pkg.__path__, prefix=package + "."):
        importlib.import_module(info.name)
    _discovered = True


def get_plugin(key: str) -> CollectorPlugin:
    discover_plugins()
    try:
        return _REGISTRY[key]()
    except KeyError:
        raise PluginNotFound(f"수집 플러그인 '{key}' 가 등록되지 않았습니다") from None


def registered_plugins() -> dict[str, type[CollectorPlugin]]:
    discover_plugins()
    return dict(_REGISTRY)
