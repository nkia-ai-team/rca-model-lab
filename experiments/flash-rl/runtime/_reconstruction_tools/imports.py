import ast, sys, sysconfig, os
from pathlib import Path
root = Path(sys.argv[1]); paths = [root, root / "src"]
stdlib = set(sys.stdlib_module_names)
def resolve(mod):
    parts = mod.split(".")
    for base in paths:
        p = base.joinpath(*parts)
        if p.with_suffix(".py").exists() or (p / "__init__.py").exists(): return str(p)
    return None
external, missing, py_compile_err = {}, [], []
for f in sorted(root.rglob("*.py")):
    try: tree = ast.parse(f.read_text())
    except SyntaxError as e: py_compile_err.append((f, e)); continue
    pkg = None
    rel = f.relative_to(root)
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import): mods = [(a.name, None) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:   # relative
                base = rel.parent.parts[1:] if rel.parts[0] == "src" else rel.parent.parts
                base = list(base)[:len(base) - node.level + 1] if node.level > 1 else list(base)
                m = ".".join(base + ([node.module] if node.module else []))
                mods = [(m, [a.name for a in node.names])]
            else: mods = [(node.module, [a.name for a in node.names])]
        for m, names in mods:
            top = m.split(".")[0]
            if top in stdlib or top == "__future__": continue
            r = resolve(m)
            if r is None:
                if resolve(top) is not None or top in ("rca_lab",): missing.append((str(rel), node.lineno, m))
                else: external.setdefault(top, set()).add(str(rel))
                continue
            if names:   # check from-import names exist (submodule or top-level def/assign)
                src = Path(r + ".py") if Path(r + ".py").exists() else Path(r) / "__init__.py"
                t2 = ast.parse(src.read_text()); defined = set()
                for n in ast.walk(t2):
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)): defined.add(n.name)
                    elif isinstance(n, ast.Assign):
                        for tg in n.targets:
                            for x in ast.walk(tg):
                                if isinstance(x, ast.Name): defined.add(x.id)
                    elif isinstance(n, (ast.Import, ast.ImportFrom)):
                        for a in n.names: defined.add((a.asname or a.name).split(".")[0])
                    elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name): defined.add(n.target.id)
                for nm in names:
                    if nm != "*" and nm not in defined and resolve(m + "." + nm) is None:
                        missing.append((str(rel), node.lineno, f"{m}.{nm}"))
print("syntax errors:", py_compile_err)
print("unresolved local imports:"); [print("  ", x) for x in missing]
print("external (third-party) roots:", {k: len(v) for k, v in sorted(external.items())})
