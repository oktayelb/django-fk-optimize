"""Which local name means which model, in one module.

This is the half of "resolve a name to a model" that is exact.  `Alarm` in a
view file means whatever that file imported, and the import statement says so
outright -- no inference, no cross-module search.  Only when a file uses a bare
name it never imported does the scan fall back to the vocabulary's by-name
lookup, which refuses ambiguous answers.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from dataclasses import field as dataclass_field


@dataclass
class ModuleImports:
    # "Alarm" -> "alarms.models.Alarm"
    names: dict[str, str] = dataclass_field(default_factory=dict)
    # "models" -> "alarms.models", for `models.Alarm` style access
    modules: dict[str, str] = dataclass_field(default_factory=dict)

    @classmethod
    def from_ast(cls, tree, package: str | None = None):
        found = cls()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # `import alarms.models` binds the *top* package unless
                    # aliased, so only the aliased form is usable as a prefix.
                    if alias.asname:
                        found.modules[alias.asname] = alias.name
                    else:
                        found.modules[alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                module = _absolute(node.module, node.level, package)
                if module is None:
                    continue
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name == "*":
                        continue
                    found.names[local] = f"{module}.{alias.name}"
                    found.modules.setdefault(local, f"{module}.{alias.name}")
        return found

    def qualname(self, node) -> str | None:
        """Dotted path for a Name or Attribute node, resolved through imports.

        Returns None for anything that is not a plain dotted chain -- a
        subscript or a call in the middle means this is not a module path.
        """
        parts = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if not isinstance(current, ast.Name):
            return None
        parts.append(current.id)
        parts.reverse()

        head = parts[0]
        if head in self.names and len(parts) == 1:
            return self.names[head]
        if head in self.modules:
            return ".".join([self.modules[head]] + parts[1:])
        return ".".join(parts)


def _absolute(module: str | None, level: int, package: str | None) -> str | None:
    """Turn a possibly-relative `from ... import` into a dotted module path."""
    if not level:
        return module
    if not package:
        # A relative import in a file whose package we were not told: better to
        # resolve nothing than to resolve it to the wrong app.
        return None
    parts = package.split(".")
    if level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (level - 1)]
    if not base:
        return None
    return ".".join(base + ([module] if module else []))
