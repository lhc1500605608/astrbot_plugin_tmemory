"""TMEAAA-459 回归：记忆列表删除按钮在 Dashboard 沙箱 iframe 中失效。

根因：AstrBot Dashboard 用
``sandbox="allow-scripts allow-forms allow-downloads"`` 的 iframe 加载插件页
（``astrbot/dashboard/dist/assets/PluginPagePage-*.js``），缺少 ``allow-modals``，
因此 ``window.confirm/prompt/alert`` 均被静默拦截（confirm 恒返回 false）。
页面里 ``deleteMemory`` 依赖 ``confirm()`` 做确认，导致点击删除后直接 return、
既无请求也无提示。

回归约束：
- 页面不得再直接调用原生 ``confirm()/prompt()/alert()``；
- 提供页内 ``confirmDialog`` / ``promptDialog``（Promise）替代；
- ``deleteMemory`` 等待页内确认、校验 ``res.ok``、刷新列表并给出失败提示。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "memory" / "index.html"


@pytest.fixture(scope="module")
def page_source() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_page_does_not_use_blocked_native_modals(page_source):
    """沙箱 iframe 无 allow-modals，原生 confirm/prompt/alert 一律不可用。"""
    assert not re.search(r"(?<!\w)(window\.)?confirm\s*\(", page_source)
    assert not re.search(r"(?<!\w)(window\.)?prompt\s*\(", page_source)
    assert not re.search(r"(?<!\w)(window\.)?alert\s*\(", page_source)


def test_page_defines_in_page_dialog_helpers(page_source):
    assert "function confirmDialog(" in page_source
    assert "function promptDialog(" in page_source
    assert 'id="dialogModal"' in page_source
    # 对话框以 Promise 返回结果
    assert "return new Promise(function(resolve)" in page_source


def test_delete_memory_uses_dialog_checks_ok_and_refreshes(page_source):
    match = re.search(
        r"async function deleteMemory\(id\)\s*\{(?P<body>.*?)\n\}", page_source, re.S
    )
    assert match, "deleteMemory not found"
    body = match.group("body")

    assert "confirmDialog(" in body, "delete must use the sandbox-safe confirm dialog"
    assert "/memory/delete" in body
    assert "JSON.stringify({ id: id })" in body
    # 只在后端确认 ok 时才提示成功
    assert re.search(r"if\s*\(\s*res\s*&&\s*res\.ok\s*\)", body)
    # 成功路径触发列表刷新
    assert "loadUserData(currentUser)" in body
    # 失败可见提示
    assert "toast(" in body and "error" in body


def test_delete_button_binds_memory_id(page_source):
    assert re.search(
        r'''onclick="deleteMemory\(' \+ m\.id \+ '\)"''',
        page_source,
    ), "记忆列表删除按钮必须绑定 deleteMemory(id)"
