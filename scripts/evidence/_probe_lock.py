"""临时探针：验证操作系统文件锁在跨进程、进程被杀时的行为。

- ``hold``：拿到锁后打印 ``HELD``，睡若干秒（或等被杀），退出时自然释放。
- ``try``：非阻塞尝试一次，打印 ``ACQUIRED`` 或 ``BUSY``。

只在本机（Windows）验证；POSIX 分支没有在本机跑过。不属于交付代码。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    fcntl = None
else:  # pragma: no cover - 本机不跑
    import fcntl

    msvcrt = None


def lock(handle: object) -> bool:
    """尝试对文件加独占锁。

    Args:
        handle: 以二进制追加模式打开的文件对象。

    Returns:
        bool: 拿到锁返回 True；被别人占用返回 False。
    """
    if msvcrt is not None:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def main() -> int:
    """按模式执行一次探针。

    Returns:
        int: 0 表示按预期结束。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("hold", "try"))
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=3.0)
    args = parser.parse_args()

    path: Path = args.path
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)

    got = lock(handle)
    if args.mode == "try":
        print("ACQUIRED" if got else "BUSY")
        handle.close()
        return 0

    if not got:
        print("BUSY")
        handle.close()
        return 1
    print("HELD", flush=True)
    time.sleep(args.seconds)
    handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
