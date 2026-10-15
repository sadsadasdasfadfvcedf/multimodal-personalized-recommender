"""config 包：统一配置出口。"""
from . import settings  # noqa: F401
from .settings import (  # noqa: F401
    PATHS,
    PROJECT_ROOT,
    RUNTIME,
    SEED,
    describe,
    ensure_dirs,
    output_path,
    set_seed,
)

__all__ = [
    "settings",
    "PATHS",
    "PROJECT_ROOT",
    "RUNTIME",
    "SEED",
    "set_seed",
    "ensure_dirs",
    "output_path",
    "describe",
]
