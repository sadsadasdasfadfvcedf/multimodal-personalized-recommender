"""
scripts/clean_workspace.py
==========================

清理提交包前必须剔除的产物：__pycache__ / *.pyc / *.pyo / 临时文件。

安全约束：只删除 **项目根目录之内** 的目标；每个待删路径都会先 resolve 并校验
是否位于 PROJECT_ROOT 之下，否则跳过并报告。
"""
from __future__ import annotations

import argparse
import fnmatch
import shutil

from _bootstrap import PROJECT_ROOT

TARGET_DIRS = ["__pycache__", ".pytest_cache", ".mypy_cache"]
TARGET_FILE_PATTERNS = ["*.pyc", "*.pyo", "*.tmp", "*.log"]
# 项目根目录下以 _ 开头的临时脚本（开发过程中的一次性补丁），**只删根目录**，
# 不碰 scripts/_bootstrap.py 这类位于子目录、被正式引用的文件。
ROOT_SCRATCH_PATTERN = "_*.py"
ROOT_SCRATCH_EXCEPT = {"_bootstrap.py"}
# 明确排除：这些目录属于交付证据，不清理
PROTECTED = ["outputs", "docs", "artifacts", "data"]


def _inside_project(path) -> bool:
    try:
        path.resolve().relative_to(PROJECT_ROOT.resolve())
        return True
    except ValueError:
        return False


def clean(dry_run: bool = False) -> dict:
    """执行清理，返回 {dirs, files, skipped}。被 06_e2e_acceptance 复用。"""
    removed_dirs: list[str] = []
    removed_files: list[str] = []
    skipped: list[str] = []

    for path in sorted(PROJECT_ROOT.rglob("*"), key=lambda p: -len(p.parts)):
        rel_parts = path.relative_to(PROJECT_ROOT).parts
        if rel_parts and rel_parts[0] in PROTECTED:
            continue
        if not _inside_project(path):
            skipped.append(str(path))
            continue
        if path.is_dir() and path.name in TARGET_DIRS:
            if dry_run:
                removed_dirs.append(str(path.relative_to(PROJECT_ROOT)))
            else:
                shutil.rmtree(path)
                removed_dirs.append(str(path.relative_to(PROJECT_ROOT)))
        elif (path.is_file() and path.parent == PROJECT_ROOT
              and fnmatch.fnmatch(path.name, ROOT_SCRATCH_PATTERN)
              and path.name not in ROOT_SCRATCH_EXCEPT):
            if dry_run:
                removed_files.append(str(path.relative_to(PROJECT_ROOT)))
            else:
                path.unlink()
                removed_files.append(str(path.relative_to(PROJECT_ROOT)))
        elif path.is_file() and any(fnmatch.fnmatch(path.name, pat) for pat in TARGET_FILE_PATTERNS):
            if dry_run:
                removed_files.append(str(path.relative_to(PROJECT_ROOT)))
            else:
                path.unlink()
                removed_files.append(str(path.relative_to(PROJECT_ROOT)))

    return {"dirs": removed_dirs, "files": removed_files, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description="清理缓存与临时文件")
    parser.add_argument("--dry-run", action="store_true", help="只列出将删除的内容")
    args = parser.parse_args()

    result = clean(dry_run=args.dry_run)
    removed = result["dirs"] + result["files"]
    print(f"[clean_workspace] 目录已清理 {len(result['dirs'])} 个，文件已清理 {len(result['files'])} 个"
          f"{'（dry-run）' if args.dry_run else ''}")
    for item in removed:
        print(f"  - {item}")
    if result["skipped"]:
        print(f"  跳过（不在项目内）：{len(result['skipped'])} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
