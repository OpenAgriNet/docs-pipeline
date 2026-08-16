"""Every global a function references must resolve in its own module.

The router split moved 100 handlers out of ``pipeline.api`` and trimmed its
imports to match. A name that is used by a helper left behind but only imported
by the module that moved away resolves at import time and fails at *call* time,
so neither the import checks, the OpenAPI schema nor a green suite can see it —
only executing that line does. ``PIPELINE_STAGES`` was dropped exactly this way
and broke ``/documents/{workflow_id}/stage-io`` and ``.../graph``.
"""

from __future__ import annotations

import builtins
import dis
import importlib
import subprocess
import sys
import types

import pytest

MODULES = [
    "pipeline.api",
    "pipeline.api_support",
    "pipeline.app",
    "pipeline.routers.admin",
    "pipeline.routers.content",
    "pipeline.routers.documents",
    "pipeline.routers.documents_actions",
    "pipeline.routers.search",
    "pipeline.routers.tenants",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports_cleanly_as_first_pipeline_import(module_name):
    """Catch import-order cycles hidden by modules cached earlier in pytest."""
    completed = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

# Names that are unresolvable on ``main`` too — pre-existing, not regressions.
KNOWN = {
    ("pipeline.app", "_AsyncGeneratorContextManager"),  # asynccontextmanager wrapper
    ("pipeline.routers.documents", "Response"),  # predates the split
}


def _globals_used(fn):
    seen = set()
    stack = [fn.__code__]
    while stack:
        code = stack.pop()
        for instr in dis.get_instructions(code):
            if instr.opname == "LOAD_GLOBAL":
                seen.add(instr.argval)
        stack.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
    return seen


def _functions(module):
    for name, obj in vars(module).items():
        if isinstance(obj, types.FunctionType) and obj.__module__ == module.__name__:
            yield name, obj
        elif isinstance(obj, type) and obj.__module__ == module.__name__:
            for meth_name, meth in vars(obj).items():
                if isinstance(meth, types.FunctionType):
                    yield f"{name}.{meth_name}", meth


@pytest.mark.parametrize("module_name", MODULES)
def test_every_referenced_global_resolves(module_name):
    module = importlib.import_module(module_name)
    missing = sorted(
        f"{module_name}.{fn_name} -> {global_name}"
        for fn_name, fn in _functions(module)
        for global_name in _globals_used(fn)
        if not hasattr(module, global_name)
        and not hasattr(builtins, global_name)
        and (module_name, global_name) not in KNOWN
    )
    assert not missing, "names used but never imported: " + "; ".join(missing)
