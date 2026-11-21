"""多程用例的本地时间基准。"""
from datetime import datetime

MULTICITY_NOW = datetime(2026, 11, 19, 12)


def freeze_webui_clock(monkeypatch, instant):
    """固定 WebUI 的本地当前时间，保留 datetime 的日期解析。"""
    class FixedDatetime(datetime):
        @classmethod
        def now(cls):
            return instant

    monkeypatch.setattr("webui.datetime", FixedDatetime)
