"""Deterministic JSON Schema counterexample minimizer (delta debugging).

流程:
1. 校验 schema 本身, 并用它校验原始实例。
2. 若原始实例有效 -> 抛出 :class:`InstanceValidError` (HTTP 422)。
3. 锁定首个校验错误的 ``validator`` 关键字 (按 JSON path/关键字/信息排序保证确定性)。
4. 以确定性 delta debugging 反复删除对象字段、删除数组块、简化标量,
   只有"仍触发同一关键字"的候选才被接受。
5. 达到固定点或评估预算耗尽时返回最小实例与完整评估轨迹。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError, ValidationError


class InstanceValidError(Exception):
    """原始实例通过了 schema 校验, 无反例可最小化。"""


class InvalidSchemaError(Exception):
    """请求中的 JSON Schema 自身不合法。"""


class _BudgetExhausted(Exception):
    """内部控制信号: 评估次数达到预算上限。"""


@dataclass
class _Budget:
    """评估次数预算。"""

    max_evaluations: int
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.max_evaluations:
            raise _BudgetExhausted()
        self.used += 1


@dataclass
class MinimizeResult:
    schema: Any
    original_instance: Any
    minimal_instance: Any
    keyword: str | None
    evaluations: int
    max_evaluations: int
    stopped_early: bool
    trace: list[dict[str, Any]] = field(default_factory=list)


def _render_path(path: Any) -> str:
    """把 jsonschema 的 deque path 或内部 tuple path 渲染成 JSONPath-ish 字符串。"""
    parts = ["$"]
    for token in path:
        if isinstance(token, int):
            parts.append(f"[{token}]")
        else:
            parts.append(f".{token}")
    return "".join(parts)


def _error_sort_key(err: ValidationError) -> tuple[str, str, str]:
    return (_render_path(err.absolute_path), err.validator or "", err.message)


class Minimizer:
    """针对单个 (schema, 实例, 预算) 执行一次最小化任务。"""

    def __init__(
        self,
        schema: Any,
        instance: Any,
        max_evaluations: int,
    ) -> None:
        self.schema = schema
        self.validator = Draft7Validator(schema)
        self.budget = _Budget(max_evaluations=max_evaluations)
        self.original_instance = copy.deepcopy(instance)
        self.current = copy.deepcopy(instance)
        self.trace: list[dict[str, Any]] = []
        self.locked_keyword: str | None = None
        self._initial_error: ValidationError | None = None

    # ------------------------------------------------------------------ #
    # 校验与候选尝试
    # ------------------------------------------------------------------ #
    def _evaluate(self, candidate: Any, label: str) -> tuple[bool, str | None, str | None]:
        """校验候选, 记录轨迹; 返回 (是否仍触发锁定关键字, 首个关键字, 信息)。

        校验抛异常的候选视为"不触发"。每次调用消耗一次评估预算。
        """
        self.budget.consume()
        first_keyword: str | None = None
        message: str | None = None
        triggered = False
        try:
            errors = sorted(self.validator.iter_errors(candidate), key=_error_sort_key)
        except Exception as exc:  # noqa: BLE001 - 任何递归/求值异常都视为不触发
            message = f"validator raised: {exc}"
            self.trace.append(
                {
                    "evaluation": self.budget.used,
                    "action": label,
                    "accepted": False,
                    "keyword": None,
                    "message": message,
                }
            )
            return False, None, message

        if errors:
            first = errors[0]
            first_keyword = first.validator
            message = first.message
            triggered = any(
                (err.validator or None) == self.locked_keyword for err in errors
            )

        self.trace.append(
            {
                "evaluation": self.budget.used,
                "action": label,
                "accepted": triggered,
                "keyword": first_keyword,
                "message": message,
            }
        )
        return triggered, first_keyword, message

    def _attempt_delete_key(self, path: tuple[Any, ...], key: str) -> bool:
        parent = self._navigate(path)
        if not isinstance(parent, dict) or key not in parent:
            return False
        candidate = copy.deepcopy(self.current)
        cand_parent = self._navigate_on(candidate, path)
        del cand_parent[key]
        ok, _, _ = self._evaluate(
            candidate, f"delete field {_render_path(path + (key,))}"
        )
        if ok:
            self.current = candidate
            return True
        return False

    def _attempt_replace(
        self, path: tuple[Any, ...], value: Any, label: str
    ) -> bool:
        candidate = copy.deepcopy(self.current)
        if not path:
            candidate = value
        else:
            parent = self._navigate_on(candidate, path[:-1])
            parent[path[-1]] = value
        # 与现状相同的候选不是真正的缩减: 不评估、不接受, 避免固定点空转。
        if candidate == self.current:
            return False
        ok, _, _ = self._evaluate(candidate, label)
        if ok:
            self.current = candidate
            return True
        return False

    # ------------------------------------------------------------------ #
    # 树工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _navigate_on(root: Any, path: tuple[Any, ...]) -> Any:
        node = root
        for token in path:
            node = node[token]
        return node

    def _navigate(self, path: tuple[Any, ...]) -> Any:
        return self._navigate_on(self.current, path)

    # ------------------------------------------------------------------ #
    # 阶段 1: 删除对象字段 (前序 DFS, 字段名排序 -> 确定性)
    # ------------------------------------------------------------------ #
    def _object_pass(self) -> bool:
        changed = False

        def walk(path: tuple[Any, ...]) -> None:
            nonlocal changed
            node = self._navigate(path)
            if isinstance(node, dict):
                # 在进入子树之前先尝试删除本对象的每个字段 (前序)。
                for key in sorted(node.keys()):
                    if self._attempt_delete_key(path, key):
                        changed = True
                        node = self._navigate(path)
                        if not isinstance(node, dict):
                            return
                for key in sorted(node.keys()):
                    walk(path + (key,))
            elif isinstance(node, list):
                for idx in range(len(node)):
                    walk(path + (idx,))

        walk(())
        return changed

    # ------------------------------------------------------------------ #
    # 通用 ddmin (序列) —— 同时用于数组块删除与字符串字符删除
    # ------------------------------------------------------------------ #
    @staticmethod
    def _chunk_ranges(length: int, n: int) -> list[range]:
        n = max(1, min(n, length))
        step = length // n
        remainder = length % n
        ranges: list[range] = []
        start = 0
        for i in range(n):
            size = step + (1 if i < remainder else 0)
            ranges.append(range(start, start + size))
            start += size
        return ranges

    def _ddmin(
        self,
        length: int,
        test: Callable[[str, int, int], bool],
    ) -> bool:
        """经典 ddmin, 作用于长度为 ``length`` 的序列。

        ``test(mode, a, b)`` 用切片语义探测候选:
        - ``"delete"``: 删除区间 [a, b);
        - ``"keep"``  : 只保留区间 [a, b)。
        返回候选是否被接受。
        """
        changed = False
        n = min(2, length)
        while length >= 1 and n <= length:
            ranges = self._chunk_ranges(length, n)
            progress = False

            # 1) 尝试删除每个块
            for rng in ranges:
                if test("delete", rng.start, rng.stop):
                    length -= rng.stop - rng.start
                    n = min(2, length) if length > 0 else 1
                    progress = True
                    changed = True
                    break
            if progress:
                continue

            # 2) 尝试只保留每个块 (n == length 时与删除其余元素等价)
            if n < length:
                for rng in ranges:
                    if test("keep", rng.start, rng.stop):
                        length = rng.stop - rng.start
                        n = min(2, length) if length > 0 else 1
                        progress = True
                        changed = True
                        break
            if progress:
                continue

            # 3) 增加粒度
            if n >= length:
                break
            n = min(n * 2, length)
        return changed

    def _ddmin_array_at(self, path: tuple[Any, ...]) -> bool:
        arr = self._navigate(path)
        # 长度 1 也尝试一次: 空数组可能仍然触发同一关键字 (如 type)。
        if not isinstance(arr, list) or len(arr) < 1:
            return False
        length = len(arr)

        def test(mode: str, a: int, b: int) -> bool:
            current_arr = self._navigate(path)
            if not isinstance(current_arr, list):
                return False
            if mode == "delete":
                candidate_arr = current_arr[:a] + current_arr[b:]
                label = f"delete array chunk {_render_path(path)}[{a}:{b}]"
            else:
                candidate_arr = current_arr[a:b]
                label = f"keep array chunk {_render_path(path)}[{a}:{b}]"
            return self._attempt_replace(path, candidate_arr, label)

        return self._ddmin(length, test)

    # ------------------------------------------------------------------ #
    # 阶段 2: 数组块删除
    # ------------------------------------------------------------------ #
    def _array_pass(self) -> bool:
        changed = False

        def walk(path: tuple[Any, ...]) -> None:
            nonlocal changed
            node = self._navigate(path)
            if isinstance(node, list):
                # 先缩小当前数组, 再递归处理 (索引可能已变化)。
                if self._ddmin_array_at(path):
                    changed = True
                node = self._navigate(path)
                if isinstance(node, list):
                    for idx in range(len(node)):
                        walk(path + (idx,))
            elif isinstance(node, dict):
                for key in sorted(node.keys()):
                    walk(path + (key,))

        walk(())
        return changed

    # ------------------------------------------------------------------ #
    # 阶段 3: 标量简化
    # ------------------------------------------------------------------ #
    def _reduce_number_at(self, path: tuple[Any, ...], value: Any) -> bool:
        """先尝试归零; 若被拒则基于当前值反复减半, 直到不再被接受。"""
        is_int = isinstance(value, int)
        zero: int | float = 0 if is_int else 0.0

        def halve(v: int | float) -> int | float:
            if is_int:
                # 向零截断, 不用地板除法, 保证负值也能收敛到 0;
                # 基于 abs 计算避免超大整数走浮点丢精度。
                half = abs(v) // 2
                return -half if v < 0 else half
            return round(v / 2.0, 12)

        changed = False
        label = _render_path(path)
        if self._attempt_replace(path, zero, f"simplify number {label} -> 0"):
            return True
        current = value
        # 32 步硬上限; 每次尝试都计入评估预算, 预算会提前终止。
        for _ in range(32):
            nxt = halve(current)
            if nxt == current:
                break
            if self._attempt_replace(
                path, nxt, f"simplify number {label} -> {nxt}"
            ):
                changed = True
                current = nxt
            else:
                break
        return changed

    def _reduce_string_at(self, path: tuple[Any, ...], value: str) -> bool:
        if len(value) < 1:
            return False
        length = len(value)

        def test(mode: str, a: int, b: int) -> bool:
            current_value = self._navigate(path)
            if not isinstance(current_value, str):
                return False
            if mode == "delete":
                candidate_str = current_value[:a] + current_value[b:]
                label = f"delete string chunk {_render_path(path)}[{a}:{b}]"
            else:
                candidate_str = current_value[a:b]
                label = f"keep string chunk {_render_path(path)}[{a}:{b}]"
            return self._attempt_replace(path, candidate_str, label)

        return self._ddmin(length, test)

    def _scalar_pass(self) -> bool:
        changed = False

        def walk(path: tuple[Any, ...]) -> None:
            nonlocal changed
            node = self._navigate(path)
            if isinstance(node, dict):
                for key in sorted(node.keys()):
                    walk(path + (key,))
            elif isinstance(node, list):
                for idx in range(len(node)):
                    walk(path + (idx,))
            else:
                label = _render_path(path)
                if isinstance(node, bool):
                    # 单向、确定性的布尔简化: 只尝试 True -> False, 不回翻,
                    # 避免在两个标量之间振荡导致固定点永不收敛。
                    if node:
                        if self._attempt_replace(
                            path, False, f"simplify boolean {label} -> false"
                        ):
                            changed = True
                elif isinstance(node, str):
                    # 空串先试; 再用 ddmin 删字符。
                    if self._attempt_replace(path, "", f"simplify string {label} -> empty"):
                        changed = True
                    elif self._reduce_string_at(path, self._navigate(path)):
                        changed = True
                elif isinstance(node, (int, float)):
                    if self._reduce_number_at(path, node):
                        changed = True
                # None 无法进一步简化

        walk(())
        return changed

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self) -> MinimizeResult:
        # 初始评估 (第 1 次): 锁定首个错误的关键字。
        try:
            errors = sorted(
                self.validator.iter_errors(self.current), key=_error_sort_key
            )
        except Exception as exc:  # noqa: BLE001 - 多为 $ref 无法解析等 schema 问题
            raise InvalidSchemaError(f"schema 求值失败: {exc}") from exc
        if not errors:
            raise InstanceValidError("原始实例通过了 schema 校验, 无需最小化")
        self._initial_error = errors[0]
        self.locked_keyword = errors[0].validator
        self.budget.consume()
        self.trace.append(
            {
                "evaluation": self.budget.used,
                "action": "initial validation (lock keyword)",
                "accepted": True,
                "keyword": self.locked_keyword,
                "message": errors[0].message,
            }
        )

        stopped_early = False
        try:
            # 固定点循环: 对象字段 -> 数组块 -> 标量, 直到一轮无变化。
            # 三个阶段每轮都必须执行 (不能用 or 短路), 否则某阶段先行缩减后,
            # 排在后面的阶段会被长期饿死 (如标量布尔/数字迟迟不被尝试)。
            while True:
                changed_obj = self._object_pass()
                changed_arr = self._array_pass()
                changed_scalar = self._scalar_pass()
                if not (changed_obj or changed_arr or changed_scalar):
                    break
        except _BudgetExhausted:
            stopped_early = True

        return MinimizeResult(
            schema=self.schema,
            original_instance=self.original_instance,
            minimal_instance=self.current,
            keyword=self.locked_keyword,
            evaluations=self.budget.used,
            max_evaluations=self.budget.max_evaluations,
            stopped_early=stopped_early,
            trace=list(self.trace),
        )


def minimize(schema: Any, instance: Any, max_evaluations: int) -> MinimizeResult:
    """公开入口: 校验 schema 后执行确定性最小化。"""
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidSchemaError(str(exc)) from exc

    return Minimizer(schema, instance, max_evaluations).run()
