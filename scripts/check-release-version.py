#!/usr/bin/env python3
"""检查源码、发行元数据、镜像和 Git 标签使用同一版本。"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def match_version(path: Path, pattern: str) -> str:
    matches = re.findall(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"{path.relative_to(ROOT)} 应恰好声明一次版本")
    return matches[0]


def checked_version(tag: str | None) -> str:
    versions = {}
    for package in ("core", "hub"):
        proj = ROOT / package / "pyproject.toml"
        versions[proj.relative_to(ROOT).as_posix()] = tomllib.loads(
            proj.read_text(encoding="utf-8")
        )["project"]["version"]
        source = ROOT / package / "src" / f"zw_{package}" / "__init__.py"
        versions[str(source.relative_to(ROOT))] = match_version(
            source, r'^__version__\s*=\s*"([^"\n]+)"$'
        )
    versions["hub/docker-compose.yml"] = match_version(
        ROOT / "hub/docker-compose.yml", r"^[ \t]+image:[ \t]+zw-hub:([^\s]+)[ \t]*$"
    )
    version = versions["core/pyproject.toml"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"发行版本必须使用三段数字，当前是 {version}")
    if len(set(versions.values())) != 1:
        values = "，".join(f"{path}={value}" for path, value in versions.items())
        raise ValueError(f"发行版本不一致：{values}")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"标签 {tag} 与当前源码版本 v{version} 不一致")
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="同时核对发行标签，如 v0.8.37")
    args = parser.parse_args()
    try:
        print(checked_version(args.tag))
    except (ValueError, OSError, KeyError) as exc:
        print(f"发行版本检查失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
