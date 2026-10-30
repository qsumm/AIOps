"""D105 机械守卫：pages/*.js 的表单/弹层原语必须「用到即有定义」。

背景（真机实测）：runbooks.js 的 openCreate() 使用了 frow()，但该原语在 cases/rules/
services 是各自文件内的局部函数，runbooks.js 既没定义也没 import —— 点击「新建变更单」
直接 ReferenceError，抽屉弹不出来，用户视角是"按钮没反应"（D86 busy 漏 import 的同款问题，
但当时的守卫只锁 busy 一个符号，没有泛化）。

本守卫泛化两条规则：
  规则1（定义检查）：对每个 static/js/pages/*.js，若调用了静态资源原语集合中的名字，
                    则该名字必须出现在本文件（function/const 定义或 import）中。
  规则2（语法检查）：node --check（ESM）每个 static/js/*.js；本机无 node 时跳过（部署机无 node）。
"""
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static" / "js"

# 局部原语（各页面自行定义、不经 ui.js 导出）：漏定义高危区
LOCAL_PRIMITIVES = {"frow"}


def _ui_exports() -> set:
    """从 ui.js 解析全部导出名（动态取，避免守卫里硬编码第二份清单）。

    D113 真机教训：旧实现只读**第一个** export {...} 行，漏掉了 helpTip ——
    dashboard.js 调用未导入的 helpTip 抛 ReferenceError，又被 .catch() 吞掉，
    首页「待我处理」卡永远停在"加载中…"。守卫必须覆盖所有导出形态与所有块。
    """
    src = (STATIC / "ui.js").read_text(encoding="utf-8")
    names = set(re.findall(r"export\s+(?:async\s+)?function\s+(\w+)", src))
    names |= set(re.findall(r"export\s+(?:const|let|var)\s+(\w+)", src))
    for m in re.finditer(r"export\s*\{([^}]+)\}", src, re.S):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            # 支持 `fmtTime as ft` 形态：两种名字都算已定义候选
            names |= {n.strip() for n in part.split(" as ")}
    return names


def _page_defined(src: str) -> set:
    """页面文件内"已就绪"的名字：import 进来的 + 本文件定义的。"""
    defined = set()
    for m in re.finditer(r"import\s*\{([^}]+)\}", src):
        for part in m.group(1).split(","):
            names = [n.strip().split(" as ")[-1].strip() for n in part.split(" as ")]
            defined |= {n for n in names if n}
    defined |= set(re.findall(r"function\s+(\w+)\s*\(", src))
    defined |= set(re.findall(r"const\s+(\w+)\s*=", src))
    defined |= set(re.findall(r"let\s+(\w+)\s*=", src))
    return defined


def test_page_form_primitives_are_defined():
    ui = _ui_exports()
    primitives = ui | LOCAL_PRIMITIVES
    errors = []
    for page in sorted((STATIC / "pages").glob("*.js")):
        src = page.read_text(encoding="utf-8")
        defined = _page_defined(src)
        for name in sorted(primitives):
            # 只检查"被调用"的原语（排除 import 行本身与注释）
            called = re.search(rf"(?<![\w.$]){name}\s*\(", src)
            if called and name not in defined:
                errors.append(f"{page.name}: 使用了 {name}() 但本文件未定义也未导入")
    assert not errors, "原语定义缺失（点击即 ReferenceError）：" + "；".join(errors)


def test_imported_names_exist_in_target_modules():
    """D107 白版守卫：import { x } from "./y.js" 的 x 必须真实存在于 y 的导出集。
    背景：thLabel 被误插进 api.js 的 import 行——api.js 无此导出 → 整个 ESM 模块图
    拒绝执行 → 公网全站白版（真机实锤）。本守卫做静态交叉检查，任何模块的
    "导入名字 ∉ 目标模块导出集" 直接红。相对路径只解析 static/js 内部；vendor/外部跳过。"""
    def exports_of(path: Path) -> set:
        src = path.read_text(encoding="utf-8")
        out = set(re.findall(r"export\s+(?:async\s+)?(?:function|const|let|var|class)\s+(\w+)", src))
        out |= set(re.findall(r"export\s+async\s+function\s+(\w+)", src))
        m = re.search(r"export\s*\{([^}]+)\}", src)
        if m:
            for part in m.group(1).split(","):
                names = [n.strip().split(" as ")[-1].strip() for n in part.split(" as ")]
                out |= {n for n in names if n}
        return out

    cache, errors = {}, []
    for js in sorted(STATIC.rglob("*.js")):
        if "vendor" in js.parts:
            continue
        src = js.read_text(encoding="utf-8")
        for m in re.finditer(r"import\s*\{([^}]+)\}\s*from\s*[\"'](\.[^\"']+)[\"']", src):
            target = (js.parent / m.group(2)).resolve()
            if not target.exists():
                errors.append(f"{js.relative_to(ROOT)}: 导入目标不存在 {m.group(2)}")
                continue
            if target not in cache:
                cache[target] = exports_of(target)
            # import 侧的 as 是"改本地名"——校验对象是 as 前面的**原导出名**
            for name in [n.strip().split(" as ")[0].strip() for n in m.group(1).split(",") if n.strip()]:
                if name not in cache[target]:
                    errors.append(f"{js.relative_to(ROOT)}: 从 {m.group(2)} 导入的 {name} 不存在于其导出集")
    assert not errors, "导入名与导出集不匹配（会白版）：".rstrip() + ("；".join(errors) if errors else "")


def test_all_js_pass_node_esm_syntax_check():
    if not shutil.which("node"):
        import pytest
        pytest.skip("本机无 node，跳过 ESM 语法守卫（部署机不跑测试）")
    errors = []
    for js in sorted(STATIC.rglob("*.js")):
        if "vendor" in js.parts:  # 第三方库不查
            continue
        r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        if r.returncode != 0:
            errors.append(f"{js.relative_to(ROOT)}: {r.stderr.strip()[:200]}")
    assert not errors, "JS 语法检查失败：" + "；".join(errors)
