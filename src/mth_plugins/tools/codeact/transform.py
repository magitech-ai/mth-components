"""Pure CodeAct AST transform helpers (no runtime dependencies)."""

from __future__ import annotations

import ast
import builtins
from typing import Any

SAFE_STDLIB_MODULES = frozenset(
    {
        "math",
        "json",
        "re",
        "datetime",
        "random",
        "collections",
        "itertools",
        "functools",
        "string",
        "copy",
        "heapq",
        "bisect",
        "typing",
    }
)

FORBIDDEN_NAMES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "eval",
        "exec",
        "open",
        "__import__",
        "builtins",
    }
)

SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs",
        "all",
        "any",
        "bin",
        "bool",
        "bytes",
        "bytearray",
        "callable",
        "chr",
        "complex",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "getattr",
        "hasattr",
        "hash",
        "hex",
        "id",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "locals",
        "map",
        "max",
        "min",
        "next",
        "oct",
        "ord",
        "pow",
        "print",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "type",
        "vars",
        "zip",
        "__build_class__",
        "Exception",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "RuntimeError",
        "StopIteration",
    )
}


class FinalAnswerError(Exception):
    """Raised by final_answer() to terminate execution with a value."""

    def __init__(self, value: Any) -> None:
        super().__init__(str(value))
        self.value = value


class ASTSafetyVisitor(ast.NodeVisitor):
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            base_mod = alias.name.split(".")[0]
            if base_mod in FORBIDDEN_NAMES or base_mod not in SAFE_STDLIB_MODULES:
                raise ValueError(f"Forbidden module import: '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not node.module:
            raise ValueError("Relative imports are forbidden")
        base_mod = node.module.split(".")[0]
        if base_mod in FORBIDDEN_NAMES or base_mod not in SAFE_STDLIB_MODULES:
            raise ValueError(f"Forbidden module import: '{node.module}'")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # simplified: check root
        root = None
        if isinstance(node.value, ast.Name):
            root = node.value.id
        if root and root in FORBIDDEN_NAMES:
            raise ValueError(f"Forbidden attribute access: '{root}.{node.attr}'")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_NAMES:
            raise ValueError(f"Forbidden function call: '{node.func.id}'")
        self.generic_visit(node)


class WriteProtectionVisitor(ast.NodeVisitor):
    """Prevents code from shadowing or overwriting protected tools or keywords."""

    def __init__(self, protected: set[str]) -> None:
        self.protected_names = protected

    def _check_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name) and target.id in self.protected_names:
            raise ValueError(
                f"Cannot overwrite protected tool or keyword '{target.id}'"
            )
        if isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._check_target(elt)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_target(node.target)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._check_target(node.target)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name in self.protected_names:
            raise ValueError(
                f"Cannot overwrite protected tool or keyword '{node.name}'"
            )
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node.name in self.protected_names:
            raise ValueError(
                f"Cannot overwrite protected tool or keyword '{node.name}'"
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name in self.protected_names:
            raise ValueError(
                f"Cannot overwrite protected tool or keyword '{node.name}'"
            )
        self.generic_visit(node)


class CodeActTransformer(ast.NodeTransformer):
    def __init__(self, tool_names: set[str]) -> None:
        self.tool_names = tool_names

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in self.tool_names:
            dispatch_call = ast.Call(
                func=ast.Name(id="__dispatch__", ctx=ast.Load()),
                args=[ast.Constant(value=node.func.id), *node.args],
                keywords=node.keywords,
            )
            return ast.Await(value=dispatch_call)
        if isinstance(node.func, ast.Name) and node.func.id == "final_answer":
            return ast.Call(
                func=ast.Name(id="__final_answer__", ctx=ast.Load()),
                args=node.args,
                keywords=node.keywords,
            )
        return node


def transform_code(code_str: str, tool_names: set[str]) -> tuple[ast.Module, set[str]]:
    parsed = ast.parse(code_str)

    ASTSafetyVisitor().visit(parsed)

    protected = set(tool_names) | {
        "final_answer",
        "__dispatch__",
        "__final_answer__",
        "__last_expr__",
    }
    WriteProtectionVisitor(protected).visit(parsed)

    transformer = CodeActTransformer(tool_names)
    transformed = transformer.visit(parsed)
    ast.fix_missing_locations(transformed)

    body = list(transformed.body)
    top_names: set[str] = set()
    for i, stmt in enumerate(body):
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    top_names.add(t.id)
        elif isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                top_names.add(stmt.target.id)
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name):
                top_names.add(stmt.target.id)
                if stmt.value is not None:
                    body[i] = ast.Assign(targets=[stmt.target], value=stmt.value)
                else:
                    body[i] = ast.Pass()
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_names.add(stmt.name)

    if body and isinstance(body[-1], ast.Expr):
        last_expr = body[-1].value
        body[-1] = ast.Assign(
            targets=[ast.Name(id="__last_expr__", ctx=ast.Store())],
            value=last_expr,
        )

    init_last = ast.Assign(
        targets=[ast.Name(id="__last_expr__", ctx=ast.Store())],
        value=ast.Constant(value=None),
    )

    global_stmt = ast.Global(names=sorted(top_names | {"__last_expr__"}))

    func_body = [
        global_stmt,
        init_last,
        *body,
        ast.Return(value=ast.Name(id="__last_expr__", ctx=ast.Load())),
    ]

    func_def = ast.AsyncFunctionDef(
        name="__codeact_runner__",
        args=ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg="__dispatch__"),
                ast.arg(arg="__final_answer__"),
            ],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=func_body,
        decorator_list=[],
        returns=None,
        type_params=[],
    )

    mod = ast.Module(body=[func_def], type_ignores=[])
    ast.fix_missing_locations(mod)
    return mod, top_names
