"""D36：巡检报告 AI 摘要因 NameError 静默降级的回归。

缺陷（本地 + 服务器均稳定复现）：`_render_report` 的函数体引用了自由变量
`quota_role`（`call_llm(..., model_role="fast", quota_role=quota_role)`），
但该变量既不在形参、也不在函数体内赋值 → `NameError` → 被
`except Exception` 静默吞掉 → **每份巡检报告都降级为「AI 摘要不可用（LLM 未配置或不可达）」**，
而 LLM 明明可用——误导用户以为模型不可用，且该功能实际从未生效。

修法：`_render_report` 形参补 `quota_role`，调用处传入。
本测试用 AST 静态检查"函数体引用的自由变量是否都已绑定"，比只断言单个参数更通用。
"""
import ast
import builtins
import inspect as _inspect
import pathlib

from app.services import inspect as I

SRC = pathlib.Path(I.__file__).read_text(encoding="utf-8")


def _fn(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(SRC)
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def test_render_report_declares_quota_role():
    sig = _inspect.signature(I._render_report)
    assert "quota_role" in sig.parameters, (
        "_render_report 形参缺 quota_role：函数体引用它会 NameError，AI 摘要会静默失效（D36）"
    )


def test_render_report_has_no_unbound_free_vars():
    """通用守卫：函数体引用的每个名字，必须在形参 / 函数内赋值 / 模块级 / 内置里。"""
    fn = _fn("_render_report")
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assigned = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assigned |= {n.target.id for n in ast.walk(fn) if isinstance(n, ast.AnnAssign)
                 and isinstance(n.target, ast.Name)}
    # 推导式/for 的绑定变量
    assigned |= {n.target.id for n in ast.walk(fn) if isinstance(n, ast.comprehension)
                 and isinstance(n.target, ast.Name)}
    # 推导式外层的 for ... in 目标（ast.For）
    assigned |= {n.target.id for n in ast.walk(fn) if isinstance(n, ast.For)
                 and isinstance(n.target, ast.Name)}
    # except ... as e
    assigned |= {n.name for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)
                 and isinstance(n.name, str) and n.name}
    # with ... as x
    assigned |= {it.optional_vars.id for n in ast.walk(fn) if isinstance(n, (ast.With, ast.AsyncWith))
                 for it in n.items if isinstance(it.optional_vars, ast.Name)}
    # 嵌套函数的形参（如 def g(k): ...）
    assigned |= {a.arg for n in ast.walk(fn) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n is not fn for a in n.args.args + n.args.kwonlyargs}
    # 嵌套 function 的局部赋值（含 lambda 参数）
    assigned |= {a.arg for n in ast.walk(fn) if isinstance(n, ast.Lambda) for a in n.args.args}
    # 嵌套函数的**名字**也是外层函数的局部变量（def g(k): → g 绑定在本函数内）
    assigned |= {n.name for n in ast.walk(fn)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn}
    assigned |= {n.name for n in ast.walk(fn) if isinstance(n, ast.ClassDef)}

    tree = ast.parse(SRC)
    module_level = set()
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            module_level |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, ast.Assign):
            module_level |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_level.add(n.name)

    known = params | assigned | module_level | set(dir(builtins))
    used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    unbound = sorted(used - known)
    assert not unbound, f"_render_report 引用了未绑定的自由变量（运行时 NameError）：{unbound}"
