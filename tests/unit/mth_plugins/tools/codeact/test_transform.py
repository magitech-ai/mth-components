"""Unit tests for the CodeAct AST transform helpers."""

from __future__ import annotations

import ast

import pytest

from mth_plugins.tools.codeact.transform import (
    ASTSafetyVisitor,
    CodeActTransformer,
    FinalAnswerError,
    WriteProtectionVisitor,
    transform_code,
)


def test_transform_code_basic_last_expression() -> None:
    """Verifies a trailing expression becomes the returned value."""
    mod, top = transform_code("x = 1\nx + 2", set())
    assert isinstance(mod, ast.Module)
    assert "x" in top
    runner_defs = [n for n in mod.body if isinstance(n, ast.AsyncFunctionDef)]
    assert any(n.name == "__codeact_runner__" for n in runner_defs)


def test_transform_code_final_answer() -> None:
    """Verifies final_answer is transformed into the internal wrapper."""
    mod, _top = transform_code("final_answer(5)", set())
    src = ast.unparse(mod)
    assert "__final_answer__" in src


def test_transform_code_tool_call_becomes_awaited_dispatch() -> None:
    """Verifies a registered tool call becomes an awaited dispatch."""
    mod, _top = transform_code("read_file('a')", {"read_file"})
    src = ast.unparse(mod)
    assert "__dispatch__" in src
    assert "Await" in ast.dump(mod) or "await" in src


def test_transform_code_ann_assign_and_aug_assign() -> None:
    """Verifies annotated and augmented assignments are handled."""
    _mod, top = transform_code("x: int = 5\nx += 1", set())
    assert "x" in top


def test_transform_code_class_and_function_defs() -> None:
    """Verifies class and function definitions register their names."""
    code = "class Foo: ...\ndef bar(): ...\nasync def baz(): ..."
    _mod, top = transform_code(code, set())
    assert {"Foo", "bar", "baz"} <= top


def test_transform_code_syntax_error() -> None:
    """Verifies a syntax error raises ValueError-class SyntaxError."""
    with pytest.raises(SyntaxError):
        transform_code("def unclosed(:", set())


def test_transform_code_forbidden_import() -> None:
    """Verifies a forbidden import raises ValueError."""
    with pytest.raises(ValueError, match="Forbidden module import"):
        transform_code("import os", set())


def test_transform_code_forbidden_import_from() -> None:
    """Verifies a forbidden from-import raises ValueError."""
    with pytest.raises(ValueError, match="Forbidden module import"):
        transform_code("from os import path", set())


def test_transform_code_forbidden_attribute() -> None:
    """Verifies forbidden attribute access raises ValueError."""
    with pytest.raises(ValueError, match="Forbidden attribute access"):
        transform_code("x = os.system", set())


def test_transform_code_forbidden_function_call() -> None:
    """Verifies a forbidden function call raises ValueError."""
    with pytest.raises(ValueError, match="Forbidden function call"):
        transform_code("eval('1')", set())


def test_transform_code_relative_import() -> None:
    """Verifies a relative import raises ValueError."""
    with pytest.raises(ValueError, match="Relative imports are forbidden"):
        transform_code("from . import x", set())


def test_transform_code_write_protection() -> None:
    """Verifies reassigning a protected name raises ValueError."""
    with pytest.raises(ValueError, match="Cannot overwrite protected tool or keyword"):
        transform_code("final_answer = 5", set())


def test_transform_code_write_protection_aug() -> None:
    """Verifies augmenting a protected name raises ValueError."""
    with pytest.raises(ValueError, match="Cannot overwrite protected tool or keyword"):
        transform_code("final_answer += 1", set())


def test_transform_code_ann_assign_without_value() -> None:
    """Verifies an annotation without a value becomes a pass."""
    mod, top = transform_code("x: int", set())
    ast.unparse(mod)
    assert "x" in top


def test_transform_code_tuple_unpack_and_attribute_targets() -> None:
    """Verifies non-name assignment targets do not register top names."""
    _mod, top = transform_code("a, b = 1, 2\nobj.x += 1\nobj.y: int = 5", set())
    assert "a" not in top
    assert "b" not in top


def test_transform_code_non_tool_call() -> None:
    """Verifies a non-tool call is left untouched."""
    mod, _top = transform_code("print('hi')", set())
    src = ast.unparse(mod)
    assert "print" in src


def test_ast_safety_attribute_non_name_root() -> None:
    """Verifies attribute access on a non-name root is not flagged."""
    visitor = ASTSafetyVisitor()
    tree = ast.parse("x.y.z")
    visitor.visit(tree)  # should not raise


def test_ast_safety_visitor_allows_stdlib() -> None:
    """Verifies the safety visitor accepts an allowed stdlib import."""
    visitor = ASTSafetyVisitor()
    tree = ast.parse("import math\nfrom collections import deque")
    visitor.visit(tree)  # should not raise


def test_write_protection_visitor_direct() -> None:
    """Verifies the write-protection visitor rejects protected reassignment."""
    visitor = WriteProtectionVisitor({"x"})
    with pytest.raises(ValueError):
        visitor.visit(ast.parse("x = 1"))


def test_codeact_transformer_direct() -> None:
    """Verifies the transformer rewrites a tool call into a dispatch."""
    transformer = CodeActTransformer({"read_file"})
    tree = transformer.visit(ast.parse("read_file('a')"))
    assert "Await" in ast.dump(tree) or "await" in ast.unparse(tree)


def test_final_answer_error_carries_value() -> None:
    """Verifies FinalAnswerError stores its value."""
    err = FinalAnswerError(42)
    assert err.value == 42
