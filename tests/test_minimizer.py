"""核心算法测试, 可用 `python tests/test_minimizer.py` 或 pytest 运行。"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.minimizer import (  # noqa: E402
    InstanceValidError,
    InvalidSchemaError,
    minimize,
)


def test_delete_irrelevant_object_fields():
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "integer"}},
    }
    # 缺少必填字段 a (首个错误 = required), b 等都是无关噪声。
    instance = {"b": 1, "noise": {"x": [1, 2, 3]}, "c": "hello"}
    r = minimize(schema, instance, 1000)
    assert r.keyword == "required"
    # 触发 required 错误的最小实例是空对象。
    assert r.minimal_instance == {}
    assert r.stopped_early is False
    assert r.trace[0]["evaluation"] == 1
    assert r.trace[0]["keyword"] == "required"
    assert all("action" in e and "accepted" in e for e in r.trace)


def test_valid_instance_raises():
    try:
        minimize({"type": "integer"}, 3, 100)
    except InstanceValidError:
        pass
    else:
        raise AssertionError("expected InstanceValidError")


def test_invalid_schema_raises():
    try:
        minimize({"type": "not-a-type"}, {}, 100)
    except InvalidSchemaError:
        pass
    else:
        raise AssertionError("expected InvalidSchemaError")


def test_array_chunk_removal():
    schema = {"type": "array", "minItems": 100}
    instance = ["x"] * 20
    r = minimize(schema, instance, 5000)
    assert r.keyword == "minItems"
    assert isinstance(r.minimal_instance, list)
    # 任意更短的数组都仍触发 minItems -> 可删到空数组。
    assert r.minimal_instance == []
    # 轨迹中必须出现数组块删除动作。
    assert any("array chunk" in e["action"] for e in r.trace)


def test_string_and_number_simplify():
    schema = {"type": "string", "const": "abcdefghij"}
    r = minimize(schema, "zzzzzzzzzz", 500)
    assert r.keyword == "const"
    # const 不匹配时, 标量无法既缩短又仍不匹配? 空串仍 != const -> 可缩成空串。
    assert r.minimal_instance == ""


def test_number_toward_zero():
    schema = {"const": 1}
    r = minimize(schema, 1024, 500)
    assert r.keyword == "const"
    # 0 != 1, 所以可以归零。
    assert r.minimal_instance == 0


def test_budget_stops_early():
    schema = {"type": "object", "required": ["a"]}
    instance = {k: 1 for k in map(str, range(50))}
    r = minimize(schema, instance, 3)
    assert r.stopped_early is True
    assert r.evaluations == 3


def test_deterministic():
    schema = {
        "type": "object",
        "required": ["z", "a", "m", "missing"],
    }
    instance = {
        k: {"nested": list(range(10)), "v": "abcdef"}
        for k in ("z", "a", "m", "x", "y", "q")
    }
    r1 = minimize(schema, instance, 5000)
    r2 = minimize(schema, instance, 5000)
    assert r1.minimal_instance == r2.minimal_instance
    assert [e["action"] for e in r1.trace] == [e["action"] for e in r2.trace]


def test_keyword_lock_prevents_drift():
    # 字符串长度 2 < minLength 5; 任何更短的字符串同样失败 -> 最小化为空串。
    schema = {"type": "string", "minLength": 5}
    r = minimize(schema, "ab", 500)
    assert r.keyword == "minLength"
    assert r.minimal_instance == ""
    assert r.evaluations <= 500


def test_keyword_lock_keeps_guilty_field():
    # a 类型错误 (锁定 type)。删除 a 后变成 required 错误, type 消失 -> 拒绝;
    # 只能删除无关字段 b, 再把数值 a 简化到 0 (仍是 number, 仍不匹配 string)。
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    }
    instance = {"a": 123, "b": 456}
    r = minimize(schema, instance, 500)
    assert r.keyword == "type"
    assert r.minimal_instance == {"a": 0}
    # 删除字段 a 的尝试必须存在且被拒绝。
    assert any(e["action"].endswith("$.a") and not e["accepted"] for e in r.trace)


def test_boolean_flip_lock():
    # 翻成 false 后实例变合法 (const 满足) -> 必须拒绝, true 已是最小。
    r = minimize({"const": False}, True, 100)
    assert r.minimal_instance is True
    # true/false 都非法时, 单向 true -> false 可被接受。
    r = minimize({"enum": [1]}, True, 100)
    assert r.minimal_instance is False


def test_negative_number_halves_toward_zero():
    # 0 是合法值 (归零被拒), 负数应向零截断减半: -7 -> -3 -> -1。
    r = minimize({"enum": [0]}, -7, 500)
    vals = [
        int(e["action"].rsplit("-> ", 1)[1])
        for e in r.trace
        if e["accepted"] and "number" in e["action"]
    ]
    assert vals == [-3, -1]
    assert r.minimal_instance == -1


def test_single_element_array_can_become_empty():
    r = minimize({"type": "array", "minItems": 2}, [1], 500)
    assert r.minimal_instance == []


def _run_all() -> int:
    tests = [
        test_delete_irrelevant_object_fields,
        test_valid_instance_raises,
        test_invalid_schema_raises,
        test_array_chunk_removal,
        test_string_and_number_simplify,
        test_number_toward_zero,
        test_budget_stops_early,
        test_deterministic,
        test_keyword_lock_prevents_drift,
        test_keyword_lock_keeps_guilty_field,
        test_boolean_flip_lock,
        test_negative_number_halves_toward_zero,
        test_single_element_array_can_become_empty,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
