#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验源码历史、贡献者署名与发布资料的一致性。

判据（与 `docs/RELEASE_PROCESS.md` 一一对应）：

| 项 | 要求 | 谁受害 |
|---|---|---|
| 历史策略 | 完整历史、单一 root、禁 merge；fork 保留基线，上游遵循收尾排列 | 上游代码与贡献者作者溯源 |
| 工作树干净 | 无未提交改动 | 资产与源码可能不同批 |
| 站点构建戳 | 本地烘焙戳 == 运行时口径 | 在线演示与源码不符 |
| README 计数 | 与 `pytest --collect-only` 一致 | 门禁假红/假绿 |
| 版本说明 | CHANGELOG 里有当前版本一节 | 用户看不到变更 |
| 标签/正文/部署/镜像/资产 | 见 `tools/release_align_check.py` | 见其输出 |

用法：

    python tools/release_guard.py              # 全量（含远端，需要 token）
    python tools/release_guard.py --offline    # 只跑本地项（CI 用，不需要 token）
    python tools/release_guard.py --hint       # 漂移时打印收敛步骤
    python tools/release_guard.py --json       # 机器可读（定时任务用来开 issue）

退出码：0 全部通过；1 有漂移；2 环境问题（缺 token / git 异常）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALIGN = os.path.join(ROOT, "tools", "release_align_check.py")
sys.path.insert(0, ROOT)

PASS, FAIL = "OK", "FAIL"
_rows = []


def row(name, ok, detail=""):
    _rows.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})
    print("%-4s %-42s %s" % (PASS if ok else FAIL, name, str(detail)[:180]),
          flush=True)
    return ok


_CWD = {"repo": ROOT}


def sh(*args):
    return subprocess.run(args, cwd=_CWD["repo"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True).stdout.strip()


def version():
    try:
        s = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', s)
        return m.group(1) if m else "?"
    except OSError:
        return "?"


# ------------------------------------------------------------------ 本地项
def history_policy():
    """缺少配置时沿用上游发版形态；fork 必须显式指定完整基线提交。"""
    path = os.path.join(_CWD["repo"], ".github", "history-policy.json")
    if not os.path.exists(path):
        return {"mode": "upstream"}
    with open(path, encoding="utf-8") as source:
        policy = json.load(source)
    if policy == {"mode": "upstream"}:
        return policy
    if not isinstance(policy, dict) or set(policy) != {"mode", "base_commit"}:
        raise ValueError("历史策略须为 upstream，或含 base_commit 的 fork 配置")
    if policy["mode"] != "fork" or not isinstance(policy["base_commit"], str):
        raise ValueError("历史策略的模式或基线类型无效")
    if not re.fullmatch(r"[0-9a-f]{40}", policy["base_commit"]):
        raise ValueError("fork 基线必须是完整的 40 位提交 SHA")
    return policy


def fork_history(base_commit):
    """完整提交祖先关系保留上游代码及作者，允许其后的线性开发。"""
    if sh("git", "cat-file", "-t", base_commit) != "commit":
        return False, "fork 基线不是提交对象"
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_commit, "HEAD"],
        cwd=_CWD["repo"], capture_output=True, text=True, encoding="utf-8")
    if result.returncode == 1:
        return False, "fork 基线 %s 不在 HEAD 的祖先中" % base_commit
    result.check_returncode()
    count = sh("git", "rev-list", "--count", base_commit + "..HEAD")
    return True, "root=1, fork 基线 %s, 后续提交 %s 笔" % (base_commit, count)


def history_shape():
    """共用完整历史与线性约束，再按配置校验基线或上游收尾排列。"""
    policy = history_policy()
    if sh("git", "rev-parse", "--is-shallow-repository") == "true":
        return False, "需要完整历史；请先执行 git fetch --unshallow"
    root = sh("git", "rev-list", "--max-parents=0", "HEAD").splitlines()
    if len(root) != 1:
        return False, "root 提交数为 %d（必须恰好 1）" % len(root)
    merges = sh("git", "rev-list", "--merges", "HEAD").splitlines()
    if merges:
        return False, "存在 merge 提交：%s" % ", ".join(m[:7] for m in merges[:3])

    if policy["mode"] == "fork":
        return fork_history(policy["base_commit"])

    root_author = sh("git", "log", "-1", "--format=%an <%ae>", root[0])
    commits = sh("git", "log", "--format=%H%x09%an <%ae>", "HEAD").splitlines()
    commits.reverse()                          # 老 → 新
    foreign, trailing_maint = [], 0
    for line in commits[1:]:                   # 跳过 root
        _sha, _tab, author = line.partition("\t")
        if author.strip() == root_author:
            trailing_maint += 1
        else:
            if trailing_maint:
                return False, ("维护者过程提交应合并进最后一笔收尾提交；"
                               "检出 %d 笔夹在外部贡献提交之前" % trailing_maint)
            foreign.append(author.strip())
    ok = trailing_maint <= 1
    detail = "root=1, 外部贡献 %d 笔, 收尾提交 %d 笔" % (len(foreign), trailing_maint)
    if not ok:
        detail += "（收尾提交最多 1 笔）"
    return ok, detail


def check_history():
    ok, detail = history_shape()
    return row("history policy", ok, detail)


def check_contributors():
    """docs/CONTRIBUTORS.md 里记的每位贡献者，都要有以他作者身份落库的提交。

    GitHub 的 Contributors 只认 author；文档里记了名、历史里却没有他的提交，
    等于没给人家承认（2026-11-18 就是这么踩的）。
    """
    tool = os.path.join(ROOT, "tools", "contributors_check.py")
    if not os.path.exists(tool):
        return row("contributors credited with their own commits", True,
                   "跳过（无 tools/contributors_check.py）")
    r = subprocess.run([sys.executable, "-X", "utf8", tool, "--quiet"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    ok = r.returncode == 0
    detail = "全部有作者提交" if ok else (r.stdout or r.stderr or "").strip()[:160]
    return row("contributors credited with their own commits", ok, detail)


def check_clean():
    dirty = [x for x in sh("git", "status", "--porcelain").splitlines() if x.strip()]
    return row("working tree clean", not dirty,
               "%d 个未提交改动" % len(dirty) if dirty else "")


def check_site_stamp():
    try:
        from core.buildstamp import stamp_of
        want = "code:" + stamp_of(ROOT)
    except Exception as e:
        return row("site build stamp current", False, "算不出戳：%r" % (e,))
    site = os.path.join(ROOT, "docs", "site", "index.html")
    try:
        page = open(site, encoding="utf-8").read()
    except OSError:
        return row("site build stamp current", False, "缺 docs/site/index.html")
    m = re.search(r'<meta name="tm-build" content="([^"]+)"', page)
    parts = (m.group(1).split() if m else [])
    ok = len(parts) >= 2 and parts[0].startswith(version()) and parts[1] == want
    return row("site build stamp current", ok,
               (m.group(1) if m else "无戳") + ("" if ok else " want " + want))


def check_readme_counts(collect=True):
    if not collect:
        return row("README test counts current", True, "跳过（--no-collect）")
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
               PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest",
                          "--collect-only", "-q"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         env=env).stdout
    m = re.search(r"(\d+) tests collected", out)
    if not m:
        return row("README test counts current", False, "collect 失败")
    n = m.group(1)
    bad = []
    for f in ("README.md", "README_EN.md"):
        try:
            t = open(os.path.join(ROOT, f), encoding="utf-8").read()
        except OSError:
            bad.append(f)
            continue
        if ("%s 项" % n) not in t and ("%s tests" % n) not in t:
            bad.append(f)
    return row("README test counts current", not bad,
               ("README 写成别的数：%s（应为 %s）" % (", ".join(bad), n)) if bad else n)


def check_notes():
    try:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from release_align_check import changelog_section
        note, src = changelog_section(version())
    except Exception as e:
        return row("release notes for current version", False, repr(e)[:80])
    return row("release notes for current version", bool(note.strip()),
               src or "CHANGELOG 里没有 v%s 一节" % version())


# ------------------------------------------------------------------ 远端项
def check_remote():
    """远端项交给校验器本身（唯一真源），本工具只汇总它的结论。"""
    if not os.path.exists(ALIGN):
        return row("release alignment (remote)", False, "缺 tools/release_align_check.py")
    r = subprocess.run([sys.executable, "-X", "utf8", ALIGN], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    tail = [ln for ln in (r.stdout or "").splitlines() if ln.startswith(("FAIL", "==="))]
    fails = [ln for ln in tail if ln.startswith("FAIL")]
    ok = r.returncode == 0
    detail = "全部通过" if ok else "；".join(f[5:60].strip() for f in fails[:4])
    if not ok and not fails:
        detail = (r.stderr or r.stdout or "")[-160:].replace("\n", " ")
    return row("release alignment (tag/body/deploy/image/assets)", ok, detail)


HINT = """
收敛步骤（漂移时按这个顺序做，全部通过后再谈发版）：

  1) python tools/converge_history.py            # 多提交 → 单一 root commit（保留树）
  2) git push --force origin main                # 推公开历史
  3) 等 CI 绿：gh run list --branch main -L 1
  4) 触发构建：release-build（push_image=true）
  5) 下载四平台资产 → 校验 → 上传替换 → 更新正文（tools/release_fill_body.py）
  6) git tag -f v1.0 HEAD && git push origin refs/tags/v1.0 --force
  7) python tools/prune_deployments.py           # 部署只留 1 条
  8) python tools/release_guard.py               # 必须 0 漂移

细节见 docs/RELEASE_PROCESS.md「发版漂移自检与收敛」。
"""

FORK_HINT = """
检查 .github/history-policy.json 中的基线是否完整且属于 HEAD 的祖先，
并确认没有 merge 提交。上游贡献者的提交与作者由祖先关系保留。
工作树、版本说明、构建戳与发布资产仍须分别通过检查。
"""


def recovery_hint():
    return FORK_HINT if history_policy()["mode"] == "fork" else HINT


def main(argv=None):
    ap = argparse.ArgumentParser(description="发版漂移体检")
    ap.add_argument("--offline", action="store_true", help="只跑本地项（CI 用）")
    ap.add_argument("--repo", default="",
                    help="要体检的仓库路径（默认本仓；形态负控用）")
    ap.add_argument("--history-only", action="store_true",
                    help="只校验历史形态（pre-push 钩子用，最快）")
    ap.add_argument("--no-collect", action="store_true", help="跳过 pytest 用例数采集")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--hint", action="store_true", help="漂移时打印收敛步骤")
    ap.add_argument("--issue-body", default="", help="把报告写成 GitHub issue 正文")
    a = ap.parse_args(argv)

    if a.repo:
        _CWD["repo"] = os.path.abspath(a.repo)

    if a.history_only:
        ok, detail = history_shape()
        row("history shape", ok, detail)
        return 0 if ok else 1

    print("版本 %s · HEAD %s · 分支 main" % (version(), sh("git", "rev-parse", "--short", "HEAD")),
          flush=True)
    check_history()
    check_clean()
    check_contributors()
    check_notes()
    check_site_stamp()
    check_readme_counts(not a.no_collect)
    if not a.offline:
        check_remote()

    bad = [r for r in _rows if not r["ok"]]
    print("\n=== %d/%d 通过 ===" % (len(_rows) - len(bad), len(_rows)), flush=True)
    if bad:
        print("漂移项：%s" % "；".join(r["check"] for r in bad), flush=True)
        if a.hint:
            print(recovery_hint(), flush=True)

    if a.issue_body:
        lines = ["<!-- release-drift-report -->",
                 "自动生成，请勿手改（`tools/release_guard.py --issue-body`）。",
                 "", "版本 `%s` · HEAD `%s`" % (version(), sh("git", "rev-parse", "--short", "HEAD")),
                 "", "| 检查 | 结果 | 说明 |", "| --- | --- | --- |"]
        for r in _rows:
            lines.append("| %s | %s | %s |"
                         % (r["check"], "✅" if r["ok"] else "❌",
                            r["detail"].replace("|", "\\|")))
        if bad:
            lines += ["", "## 修复检查", "```", recovery_hint().strip(), "```"]
        open(a.issue_body, "w", encoding="utf-8", newline="\n").write(
            "\n".join(lines) + "\n")
        print("issue 正文已写：%s" % a.issue_body, flush=True)

    if a.json:
        print(json.dumps({"ok": not bad, "rows": _rows}, ensure_ascii=False))
    return 0 if not bad else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("发版检查无法完成：%s" % error, file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        sys.exit(2)
