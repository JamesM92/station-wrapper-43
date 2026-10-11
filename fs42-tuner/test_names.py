"""A name used but never defined or imported anywhere in its file is a NameError waiting for the one code path that
reaches it (a deleted helper passes every test that does not call it). Checks every module here.
Run: python3 test_names.py"""
import ast
import builtins
import glob
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def undefined(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]:
                bound.add(arg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for n in node.names:
                bound.add((n.asname or n.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound})


class Names(unittest.TestCase):
    def test_no_module_uses_a_name_that_nothing_defines(self):
        bad = {os.path.basename(p): undefined(p) for p in sorted(glob.glob(os.path.join(HERE, "*.py"))) if undefined(p)}
        self.assertEqual(bad, {})


if __name__ == "__main__":
    unittest.main()
