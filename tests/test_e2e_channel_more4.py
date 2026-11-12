# -*- coding: utf-8 -*-
"""由轮号钉归并生成的模块（tests/ 归并工程，映射见 _scratch/w2a_mapping.json）。

本文件按主题收拢下列源文件，测试函数名与原实现逐字保留：
- tests/test_r715_channel_icons_official.py

合并约定：import 区去重合并；每个源文件的模块级语句、辅助函数、
fixture 以 `# ---- merged from ... ----` 分段原样保留；
`if __name__ == "__main__"` 块移至文件尾。
"""
from support_webui_src import webui_full_src

import os
import pytest
import re
import sys


# ---- merged from tests/test_r715_channel_icons_official.py ----

pytest.importorskip("playwright",
                    reason="图标判据依赖 playwright（缺库 SKIP 不 ERROR）")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = None


def _src():
    return webui_full_src()


def test_every_plat_has_official_icon_in_registry():
    """注册表层面：SRX_PLATS 的每个键都要挂着**非空的 data:image 值**。

    判据只查「键名存在」会空转：把值删成 `"app_qunar":""` 时键名还在，
    门禁照样绿（2026-10-31 负控实测）。必须把键与其后的 data URI 一起匹配。
    """
    src = _src()
    i = src.index("const SRX_PLATS={")
    j = src.index("};", i)
    plats = re.findall(r"(\w+):\[", src[i:j])

    k = src.index("const SRX_BRAND_IMG={")
    line = src[k:src.index("\n", k)]
    am = re.search(r"const SRX_BRAND_ALIAS=\{(.*?)\};", src, re.S)
    alias = dict(re.findall(r"(\w+):'(\w+)'", am.group(1) if am else ""))
    missing = []
    for p in plats:
        key = p if re.search(r'"%s"\s*:' % re.escape(p), line) else alias.get(p)
        m = re.search(r'"%s"\s*:\s*"data:image/[a-z+]+;base64,([A-Za-z0-9+/=]+)"'
                      % re.escape(key or ""), line)
        if not m or len(m.group(1)) < 100:
            missing.append(p)
    assert plats, "SRX_PLATS 解析为空（判据本身失效）"
    assert not missing, "这些渠道没有可用的官方图标（会退化成字符牌）：%s" % missing


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p












