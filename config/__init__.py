"""config 包：统一配置出口。"""

from . import settings
from .settings import (
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
    "PATHS",
    "PROJECT_ROOT",
    "RUNTIME",
    "SEED",
    "describe",
    "ensure_dirs",
    "output_path",
    "set_seed",
    "settings",
]
