"""Extract literal browser-facing diagnostics without importing server code."""

import ast
import json
from pathlib import Path


def messages():
    result = set()
    for path in Path("src/talaria").glob("*.py"):
        tree = ast.parse(path.read_text())
        constants = {
            target.id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "APIError" and node.args:
                    argument = node.args[0]
                    if isinstance(argument, ast.Name):
                        argument = constants.get(argument.id, argument)
                    result.update(strings(argument))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                (path.name == "hermes.py" and node.name == "response_error")
                or (path.name == "app.py" and node.name in {"__call__", "unexpected_error"})
                or (path.name == "metadata.py" and node.name == "readiness_info")
                or (path.name == "relay.py" and node.name == "poll")
            ):
                # These small projections contain machine keys as well as text;
                # include only authored sentences, never dynamic diagnostic data.
                result.update(
                    value for value in strings(node) if " " in value and value.endswith(".")
                )
    return sorted(result)


def strings(node):
    # Interpolated errors intentionally keep the original diagnostic: translating
    # pieces of a sentence would break grammar and could alter native values.
    if isinstance(node, ast.JoinedStr):
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return [value for child in ast.iter_child_nodes(node) for value in strings(child)]


if __name__ == "__main__":
    print(json.dumps(messages(), ensure_ascii=False))
