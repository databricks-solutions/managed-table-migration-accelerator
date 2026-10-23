"""No notebook unbinds one of its own imports with a module-scope ``except ... as <alias>``.

Python deletes the ``as`` name when the handler exits, so a degrade-honestly branch in a
notebook cell that reuses an import alias (``except Exception as ex`` with
``from migration import executor as ex``) silently unbinds the module, and the next cell that
calls ``ex.…`` dies with ``NameError`` — only on the workspaces where that branch fires.
"""

import ast
from pathlib import Path

NOTEBOOKS = Path(__file__).resolve().parents[2] / "notebooks"
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _shadowing_handlers(src: str) -> list[tuple[int, str]]:
    tree = ast.parse(src)
    imported = {a.asname or a.name.split(".")[0]
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names}
    found: list[tuple[int, str]] = []

    def walk(node, in_scope: bool) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ExceptHandler) and ch.name in imported and not in_scope:
                found.append((ch.lineno, ch.name))
            walk(ch, in_scope or isinstance(ch, _SCOPES))

    walk(tree, False)
    return found


def test_detector_flags_a_module_scope_handler_and_ignores_one_inside_a_function():
    src = (
        "from migration import executor as ex\n"
        "try:\n    pass\nexcept Exception as ex:\n    pass\n"
        "def f():\n    try:\n        pass\n    except Exception as ex:\n        pass\n"
    )
    assert _shadowing_handlers(src) == [(4, "ex")]


def test_no_notebook_shadows_an_import_alias():
    offenders = [f"{p.relative_to(NOTEBOOKS)}:{line} `{name}`"
                 for p in sorted(NOTEBOOKS.rglob("*.py"))
                 for line, name in _shadowing_handlers(p.read_text(encoding="utf-8"))]
    assert offenders == []
