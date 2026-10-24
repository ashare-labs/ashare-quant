"""Aquant import validation and byte-preserving migration of explicit Python imports."""

import ast
import builtins
import re

LEGACY = "jqdata"
CURRENT = "aquant"
MIGRATION_MESSAGE = (
    "AQUANT_LEGACY_IMPORT: jqdata was renamed to aquant. "
    "Prepare a reviewed migration with ashare-quant-migrate-imports; "
    "see docs/aquant-migration.md. Frozen historical source must remain unchanged."
)


def legacy_nodes(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == LEGACY for a in node.names):
                yield node
        elif isinstance(node, ast.ImportFrom) and not node.level:
            if (node.module or "").split(".")[0] == LEGACY:
                yield node


def validate_imports(source):
    if list(legacy_nodes(ast.parse(source))):
        raise ValueError(MIGRATION_MESSAGE)


def strategy_import(name, globals=None, locals=None, fromlist=(), level=0):
    # A local builtins copy gives even deferred imports an actionable diagnostic,
    # without modifying Python's process-wide import machinery or third-party modules.
    if level == 0 and name.split(".")[0] == LEGACY:
        raise ModuleNotFoundError(MIGRATION_MESSAGE, name=LEGACY)
    return builtins.__import__(name, globals, locals, fromlist, level)


def migrate_source(raw):
    """Return bytes and an audit record. Never execute source or rewrite its AST.

    Only absolute, explicit imports of the single runtime module can be automated.
    Dynamic imports, submodules and otherwise ambiguous legacy identifiers require
    review. An unaliased `import jqdata` retains its local name with an explicit alias;
    that preserves every existing variable reference without a runtime old-name entry.
    """
    source = raw.decode("utf-8-sig")
    tree = ast.parse(source)
    bom = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    lines = source.encode("utf-8").splitlines(keepends=True)
    offsets = [bom]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def position(node):
        return offsets[node.lineno - 1] + node.col_offset

    changes, issues = [], []
    handled = {
        alias.asname for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names if alias.name == CURRENT
    }
    for node in legacy_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module != LEGACY:
                issues.append(f"line {node.lineno}: legacy submodule needs manual review")
                continue
            start = position(node)
            # The grammar guarantees the module name between `from` and `import`.
            end = offsets[node.end_lineno - 1] + node.end_col_offset
            match = re.search(rb"\bjqdata\b", raw[start:end])
            if match is None:
                raise ValueError("Cannot locate import token")
            changes.append((start + match.start(), start + match.end(), b"aquant"))
        else:
            for alias in node.names:
                if alias.name.split(".")[0] != LEGACY:
                    continue
                if alias.name != LEGACY:
                    issues.append(f"line {node.lineno}: legacy submodule needs manual review")
                    continue
                start = position(alias)
                replacement = b"aquant" if alias.asname else b"aquant as jqdata"
                changes.append((start, start + len(LEGACY), replacement))
                handled.add(alias.asname or LEGACY)
    # Literal references may be dynamic import arguments. Never guess or replace
    # strings/comments, which can carry strategy decisions or immutable evidence.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            name = function.id if isinstance(function, ast.Name) else (
                function.attr if isinstance(function, ast.Attribute) else None
            )
            if name in {"__import__", "import_module", "exec", "eval"}:
                argument = node.args[0] if node.args else None
                if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                    issues.append(f"line {node.lineno}: dynamic code/import needs manual review")
                elif re.search(r"\bjqdata\b", argument.value):
                    issues.append(f"line {node.lineno}: dynamic legacy code/import needs manual review")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == LEGACY or node.value.startswith(LEGACY + "."):
                issues.append(f"line {node.lineno}: legacy module string needs manual review")
        if isinstance(node, ast.Name) and node.id == LEGACY and LEGACY not in handled:
            issues.append(f"line {node.lineno}: unbound legacy identifier needs manual review")
    if issues:
        return raw, {"status": "review_required", "issues": sorted(set(issues)), "changes": 0}
    result = raw
    for start, end, replacement in sorted(changes, reverse=True):
        result = result[:start] + replacement + result[end:]
    # Proof at the AST level: imports alone change; local names and all business
    # nodes stay identical. Byte edits additionally preserve comments/line endings.
    expected = ast.parse(source)
    for node in legacy_nodes(expected):
        if isinstance(node, ast.ImportFrom):
            node.module = CURRENT
        else:
            for alias in node.names:
                if alias.name == LEGACY:
                    alias.name = CURRENT
                    alias.asname = alias.asname or LEGACY
    if ast.dump(expected) != ast.dump(ast.parse(result.decode("utf-8-sig"))):
        raise ValueError("Migration changed more than import module names and equivalent bindings")
    return result, {"status": "changed" if changes else "unchanged", "issues": [],
                    "changes": len(changes), "ast_import_only": True}
