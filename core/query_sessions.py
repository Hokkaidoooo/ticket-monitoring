"""查询角色的固定目录与匿名会话能力契约。"""
from __future__ import annotations

import os
from collections.abc import Mapping

from core.paths import resolve
from core.yield_stats import user_data_root


class AnonymousSessionUnsupportedError(ValueError):
    """渠道无法建立与已登录设备隔离的匿名会话。"""

    status = "unsupported"


class _FrozenDict(dict):
    """保留 JSON 映射格式，同时禁止修改已解析的角色。"""

    def _immutable(self, *args, **kwargs):
        raise TypeError("查询角色配置已冻结")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def _configured_bool(crawler_cfg: dict, key: str) -> bool:
    if not isinstance(crawler_cfg, dict):
        raise TypeError("crawler 配置必须是字典")
    value = crawler_cfg.get(key, False)
    if value is not True and value is not False:
        raise ValueError(f"crawler.{key} 必须是布尔值")
    return value


def anonymous_query_enabled(crawler_cfg: dict) -> bool:
    """匿名标志只接受真正的布尔值，缺省沿用账号会话。"""
    return _configured_bool(crawler_cfg, "anonymous_query")


def _absolute_root(path, default: str = "") -> str:
    if path is not None and not isinstance(path, (str, os.PathLike)):
        raise ValueError("会话目录必须是路径字符串")
    return os.path.abspath(resolve(os.fspath(path) if path else "", default))


def _ensure_separate_roots(account_root: str, monitor_root: str) -> None:
    account = os.path.normcase(os.path.realpath(account_root))
    monitor = os.path.normcase(os.path.realpath(monitor_root))
    if os.path.splitdrive(account)[0] != os.path.splitdrive(monitor)[0]:
        return
    if os.path.commonpath((account, monitor)) in (account, monitor):
        raise ValueError("匿名监控目录与账号目录必须独立，不能重合或互相包含")


def resolve_query_sessions(crawler_cfg: dict) -> dict:
    """一次解析两个角色；环境变化或后续配置修改不改变本次目录。"""
    monitor_anonymous = _configured_bool(crawler_cfg, "monitor_anonymous")
    verify_account = _configured_bool(crawler_cfg, "verify_account_in_background")
    account_root = _absolute_root(user_data_root(crawler_cfg))
    monitor_root = account_root
    if monitor_anonymous:
        monitor_root = _absolute_root(
            crawler_cfg.get("anonymous_user_data_dir"), "anonymous_data")
        _ensure_separate_roots(account_root, monitor_root)
    return _FrozenDict({
        "monitor": _FrozenDict({
            "user_data_dir": monitor_root,
            "anonymous_query": monitor_anonymous,
        }),
        "account": _FrozenDict({
            "user_data_dir": account_root,
            "anonymous_query": False,
        }),
        "monitor_anonymous": monitor_anonymous,
        "verify_account_in_background": verify_account,
    })


def crawler_for_session(crawler_cfg: dict, resolved_roles: Mapping,
                        role: str = "monitor") -> dict:
    """复制采集配置并注入固定角色，不再次解析配置或环境路径。"""
    if not isinstance(crawler_cfg, dict):
        raise TypeError("crawler 配置必须是字典")
    if role not in ("monitor", "account"):
        raise ValueError(f"未知查询角色：{role!r}")
    if not isinstance(resolved_roles, Mapping):
        raise TypeError("已解析的查询角色必须是映射")
    session = resolved_roles[role]
    root = session["user_data_dir"]
    if not isinstance(root, str) or not os.path.isabs(root):
        raise ValueError("已解析的会话目录必须是绝对路径")
    anonymous = anonymous_query_enabled(session)
    return dict(crawler_cfg, user_data_dir=root, anonymous_query=anonymous)


def ensure_session_supported(platform: str, crawler_cfg: dict) -> None:
    """App 使用现有设备会话，不能承诺匿名隔离；须在设备操作前拒绝。"""
    anonymous = anonymous_query_enabled(crawler_cfg)
    if anonymous and str(platform).startswith("app_"):
        raise AnonymousSessionUnsupportedError(
            f"{platform} 暂不支持匿名查询；请使用浏览器渠道，或切换账号核价")
