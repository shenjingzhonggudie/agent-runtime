"""临时工具：从源码里抽真实的模块依赖边，而不是靠记忆画图。

用法：
    python scripts/_dep_graph.py            # 打印包内依赖边、分层顺序、环
    python scripts/_dep_graph.py --all      # 连 tests/ 与 scripts/ 的边一起打印

做法：AST 解析每个文件的 import，把 ``from .x import``、``from agentrun.x import``
统一还原成包内模块名；只统计包内依赖，标准库与第三方另行统计。
不属于交付代码。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "agentrun"
PACKAGE = "agentrun"


def internal_imports(path: Path, module: str) -> set[str]:
    """返回单个文件依赖的包内模块名。

    Args:
        path: 源文件路径。
        module: 该文件对应的模块名（如 ``agentrun.journal``）。

    Returns:
        set[str]: 依赖的包内模块名集合，不含自身。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module.rsplit(".", 1)[0]
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0]
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            if target == PACKAGE or target.startswith(PACKAGE + "."):
                for alias in node.names:
                    found.add(f"{target}.{alias.name}" if target == PACKAGE else target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PACKAGE + "."):
                    found.add(alias.name)

    return {item for item in found if item != module}


def external_imports(path: Path) -> set[str]:
    """返回单个文件用到的包外模块（标准库或第三方），取顶层包名。

    Args:
        path: 源文件路径。

    Returns:
        set[str]: 形如 ``sqlite3``、``pytest`` 的顶层模块名；``__future__`` 不计。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
    return {name for name in names if name not in {"__future__", "agentrun"}}


def collect() -> dict[str, set[str]]:
    """收集包内所有模块的依赖边。

    Returns:
        dict[str, set[str]]: 模块名 → 它依赖的模块名集合。
    """
    edges: dict[str, set[str]] = {}
    for path in sorted(PKG.glob("*.py")):
        module = f"{PACKAGE}.{path.stem}"
        edges[module] = internal_imports(path, module)
    return edges


def layers(edges: dict[str, set[str]]) -> list[list[str]]:
    """按"只依赖下一层"的方式分层。

    Args:
        edges: 依赖图。

    Returns:
        list[list[str]]: 从最底层（不依赖任何包内模块）往上排。

    Notes:
        出现环时，剩余的模块会被塞进最后一层——所以调用方必须同时看环检测结果。
    """
    remaining = dict(edges)
    ordered: list[list[str]] = []
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not (deps & remaining.keys()))
        if not ready:
            ordered.append(sorted(remaining))
            break
        ordered.append(ready)
        for name in ready:
            del remaining[name]
    return ordered


def cycles(edges: dict[str, set[str]]) -> list[tuple[str, ...]]:
    """找出所有环（同一环只报一次）。

    Args:
        edges: 依赖图。

    Returns:
        list[tuple[str, ...]]: 每个环的模块序列。
    """
    found: list[tuple[str, ...]] = []
    seen: set[frozenset[str]] = set()

    def walk(node: str, path: list[str]) -> None:
        for dep in sorted(edges.get(node, ())):
            if dep in path:
                cycle = tuple(path[path.index(dep) :])
                key = frozenset(cycle)
                if key not in seen:
                    seen.add(key)
                    found.append(cycle)
                continue
            walk(dep, [*path, dep])

    for name in sorted(edges):
        walk(name, [name])
    return found


def main() -> int:
    """打印依赖图、分层与环数量。

    Returns:
        int: 0 表示无环，1 表示有环。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="同时打印 tests/ 与 scripts/ 的边")
    parser.add_argument("--external", action="store_true", help="打印标准库 / 第三方依赖")
    args = parser.parse_args()

    edges = collect()
    print("== 包内依赖边 ==")
    for name in sorted(edges):
        deps = ", ".join(sorted(dep.removeprefix(PACKAGE + ".") for dep in edges[name])) or "—"
        print(f"{name.removeprefix(PACKAGE + '.')}  ->  {deps}")

    print("\n== 分层（自底向上）==")
    for index, layer in enumerate(layers(edges), start=1):
        print(f"L{index}: " + ", ".join(item.removeprefix(PACKAGE + ".") for item in layer))

    found = cycles(edges)
    print(f"\n== 环：{len(found)} 个 ==")
    for cycle in found:
        print(" -> ".join(item.removeprefix(PACKAGE + ".") for item in cycle))

    if args.external:
        stdlib = set(sys.stdlib_module_names)
        all_external: set[str] = set()
        print("\n== 每个模块的包外依赖 ==")
        for path in sorted(PKG.glob("*.py")):
            used = external_imports(path)
            all_external |= used
            if used:
                print(f"{path.stem}  ->  {', '.join(sorted(used))}")
        third_party = sorted(all_external - stdlib)
        print("\n== 汇总 ==")
        print(f"标准库（{len(all_external & stdlib)}）：{', '.join(sorted(all_external & stdlib))}")
        print(f"第三方（{len(third_party)}）：{', '.join(third_party) or '无'}")

    if args.all:
        print("\n== 测试与脚本引用到的包内模块 ==")
        for base in ("tests", "scripts"):
            for path in sorted((ROOT / base).glob("*.py")):
                used = internal_imports(path, f"{PACKAGE}.{path.stem}")
                if used:
                    names = ", ".join(sorted(item.removeprefix(PACKAGE + ".") for item in used))
                    print(f"{base}/{path.name}  ->  {names}")

    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
