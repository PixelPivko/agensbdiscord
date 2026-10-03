import ast
from pathlib import Path


def test_no_dynamic_execution_or_shell_imports():
    files = [Path("bot.py"), *Path("cogs").glob("*.py"), *Path("services").glob("*.py")]
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile", "__import__"}, str(file)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"system", "popen", "create_subprocess_exec", "create_subprocess_shell"}, str(file)
            if isinstance(node, ast.Import):
                assert all(n.name not in {"subprocess", "pickle"} for n in node.names), str(file)
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"subprocess", "pickle"}, str(file)
