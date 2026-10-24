"""前端样式自检：把 CSS 里"写错了也不报错"的几类问题挡在发布之前。

运行方式（纯标准库，无需 pytest）::

    python tests/check_css.py

为什么需要它：
    CSS 是静默失败的语言 —— 少一个大括号、引用了不存在的变量、把 id 写成
    ``#view-sshh``，浏览器都只会**默默忽略**那段规则，页面上表现为"样式没生效"，
    而且往往要等到某个人在手机上打开才发现。本脚本检查四件事：

    1. **括号配平** —— 每个 css 文件的花括号必须成对（注释与字符串先剔除）；
    2. **视图显隐不变式** —— 直接作用在 ``#view-*`` 本体上的 ``display`` 规则必须限定
       ``.is-active``，否则会盖掉 router 隐藏非当前视图的 ``.view{display:none}``，
       表现为「切换模块后旧页面还留在屏幕上」（真实事故，见 view_display_violations）；
    3. **变量有定义** —— ``var(--x)`` 的 ``--x`` 必须在某个文件里定义过
       （或自带兜底值 ``var(--x, 默认)``，否则拿到的是无效值）；
    4. **id 选择器真的存在** —— ``#foo`` 必须能在 HTML 的 id 属性或 JS 里找到，
       视图容器 ``#view-*`` 由 router.js 动态生成，单独放行；
    5. **加载顺序与完整性** —— ``mobile.css`` 必须排在最后（它整层覆盖前层布局），
       且 css/ 目录里的每个文件都要被 index.html 引用，杜绝"加了文件忘了挂"。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
CSS_DIR = STATIC / "css"
INDEX = STATIC / "index.html"
JS_DIR = STATIC / "js"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条检查结果。"""
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def strip_comments(css: str) -> str:
    """剔除 /* ... */ 注释，避免注释里的花括号干扰计数。"""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def parse_rules(css: str) -> list[tuple[str, str]]:
    """把 CSS 拆成 ``(选择器, 声明块)`` 列表（不含 @media 等嵌套块的选择器前缀）。

    只做够用的解析：本项目的样式没有嵌套规则，花括号成对，注释已剔除。

    Args:
        css: 已剔除注释的 CSS 文本。

    Returns:
        ``[(selector, declarations), ...]``。
    """
    rules: list[tuple[str, str]] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        rules.append((match.group(1).strip(), match.group(2)))
    return rules


def view_display_violations(css_files: dict[str, str]) -> list[str]:
    """找出会让「非当前视图」继续显示的规则。

    背景（真实事故）：手机适配层里写了 ``#view-converter { display: block }``，
    ID 选择器优先级高于 router 用来隐藏非当前视图的 ``.view { display: none }``，
    结果切到别的模块后转换页依然可见 —— 用户看到的就是「点了底部标签栏没反应，
    一直卡在转换页」。

    不变式：凡是**直接作用在 ``#view-*`` 本体上**并声明了 ``display`` 的规则，
    必须同时限定 ``.is-active``。作用在后代上的规则（``#view-x .panel``）不受影响
    —— 父级 ``display:none`` 会连带隐藏子树。

    Args:
        css_files: 文件名 → 已剔除注释的 CSS 文本。

    Returns:
        违规描述列表；无违规则为空。
    """
    violations: list[str] = []
    for filename, css in css_files.items():
        for selector, body in parse_rules(css):
            if "display" not in body:
                continue
            # 逐条选择器检查，处理 "A, B" 这种列表
            for part in selector.split(","):
                part = part.strip()
                if not re.match(r"^#view-[\w-]+", part):
                    continue
                # 命中 #view-xxx 之后若紧跟空格/组合符，说明作用在后代上，放行
                rest = part[len(re.match(r"^#view-[\w-]+", part).group(0)):]
                if rest.startswith((" ", ">", "+", "~")):
                    continue
                if ".is-active" not in rest:
                    violations.append(f"{filename}: {part}")
    return violations


def main() -> int:
    """执行全部样式检查。"""
    css_files = sorted(CSS_DIR.glob("*.css"))
    check("css/ 目录存在样式文件", bool(css_files), f"{len(css_files)} 个")

    index_html = INDEX.read_text(encoding="utf-8")

    # ---------- 1. 括号配平 ----------
    bodies: dict[str, str] = {}
    for path in css_files:
        raw = path.read_text(encoding="utf-8")
        body = strip_comments(raw)
        bodies[path.name] = body
        opens, closes = body.count("{"), body.count("}")
        check(
            f"{path.name} 花括号配平",
            opens == closes,
            "" if opens == closes else f"{{ {opens} 个 / }} {closes} 个",
        )

    # ---------- 1.1 视图显隐不变式 ----------
    bad_view_rules = view_display_violations(bodies)
    check("针对 #view-* 的 display 规则限定了 .is-active", not bad_view_rules,
          f"会盖掉 .view{{display:none}} 的规则：{bad_view_rules}")

    # ---------- 2. 变量有定义 ----------
    all_css = "\n".join(bodies.values())
    defined = set(re.findall(r"(--[\w-]+)\s*:", all_css))
    unresolved: list[str] = []
    for path in css_files:
        for match in re.finditer(r"var\(\s*(--[\w-]+)\s*(,)?", bodies[path.name]):
            name, has_fallback = match.group(1), bool(match.group(2))
            if name not in defined and not has_fallback and name not in unresolved:
                unresolved.append(name)
    check("var(--x) 均有定义或兜底值", not unresolved, f"未定义：{unresolved}")

    # ---------- 3. id 选择器存在 ----------
    # 动态生成：router.js 用 `view-${id}` 作为视图容器 id
    dynamic_prefixes = ("view-",)
    # HTML 里的 id
    html_ids = set(re.findall(r'id="([\w-]+)"', index_html))
    # JS 里显式创建/查询的 id（id="x" / id: 'x' / getElementById('x') / $('#x')）
    js_text = "\n".join(p.read_text(encoding="utf-8") for p in JS_DIR.rglob("*.js"))
    js_ids = set(re.findall(r"""id\s*[:=]\s*['"]([\w-]+)['"]""", js_text))
    js_ids |= set(re.findall(r"""getElementById\(['"]([\w-]+)['"]\)""", js_text))
    js_ids |= set(re.findall(r"""\$\(['"]#([\w-]+)['"]""", js_text))
    known = html_ids | js_ids

    used_ids: dict[str, str] = {}
    for name, body in bodies.items():
        for match in re.finditer(r"#([A-Za-z][\w-]*)", body):
            token = match.group(1)
            # 十六进制颜色（#fff / #d3dde8）不是 id 选择器
            if re.fullmatch(r"[0-9a-fA-F]{3,8}", token):
                continue
            used_ids.setdefault(token, name)
    missing = [
        f"#{token}（{owner}）"
        for token, owner in sorted(used_ids.items())
        if token not in known and not token.startswith(dynamic_prefixes)
    ]
    check("CSS 中的 #id 选择器均有对应节点", not missing, f"未找到：{missing}")

    # ---------- 4. 加载顺序与完整性 ----------
    links = re.findall(r'<link[^>]+href="css/([\w.-]+)"', index_html)
    check("index.html 引用了全部样式文件", set(links) == {p.name for p in css_files},
          f"页面引用 {links} / 目录 {[p.name for p in css_files]}")

    # mobile.css 依赖"排在最后"这一事实：它整层覆盖前层的布局规则
    if "mobile.css" in links:
        check("mobile.css 位于样式表最后（覆盖层顺序）", links[-1] == "mobile.css",
              f"实际顺序：{links}")
    else:
        check("mobile.css 已挂载", False, "index.html 未引用")

    # ---------- 汇总 ----------
    print()
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print(f"  · {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
