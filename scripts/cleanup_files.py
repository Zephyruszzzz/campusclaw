#!/usr/bin/env python
"""重试待处理的文件清理任务。

用法：
    python scripts/cleanup_files.py --once

退出码 0 表示全部 pending 任务已完成；非零表示仍有任务清理失败。
只处理任务指向的已删除材料，不会误删活跃材料的文件。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from campusclaw import db as dbmod, storage  # noqa: E402
from campusclaw.config import ConfigError, load_settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重试 CampusClaw 文件清理任务")
    parser.add_argument(
        "--once", action="store_true", required=True, help="执行一次清理推进后退出"
    )
    parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[cleanup] 配置错误：{exc}", file=sys.stderr)
        return 2

    conn = dbmod.connect(settings.db_path)
    try:
        succeeded, failed = storage.cleanup_once(conn, settings.upload_dir)
    finally:
        conn.close()

    if succeeded:
        print(f"[cleanup] 已完成清理的材料：{succeeded}")
    if failed:
        print(
            f"[cleanup] 仍有 {len(failed)} 项清理失败，保留 pending 任务待下一次重试：{failed}",
            file=sys.stderr,
        )
        return 1
    print("[cleanup] 无失败任务，清理状态：done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
