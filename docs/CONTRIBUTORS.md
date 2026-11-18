# 贡献者记录（Contributors）

## 为什么单独有一份

GitHub 的 Insights → Contributors（`GET /repos/{owner}/{repo}/contributors`）
**只统计 commit 的 `author`**，`Co-authored-by:` 不会让人出现在那个列表里
（2026-11-18 实测：主干上带 `Co-authored-by: Shengcun-Scanner` 的提交存在，
但 contributors API 只返回维护者）。

所以我们对**外部贡献**的处理方式是：把他的改动以**他自己的作者身份**落库
（`git am` / `cherry-pick` 保留原作者与日期），并且在 `docs/RELEASE_PROCESS.md`
§3 里把历史形态规定为：

```
root（维护者）
├─ 外部贡献提交 × N   作者 = 贡献者本人
└─ 维护者收尾提交 × 0..1（必须在最后）
禁止 merge 提交
```

这样既保住"线性、可对齐、单一 root"的规范，又让贡献者真正被 GitHub 承认。

## 贡献者与对应提交

| 贡献者 | 贡献内容 | 提交 |
|---|---|---|
| [@Shengcun-Scanner](https://github.com/Shengcun-Scanner)（Roni Saguey） | 报告并初版实现升级包解压防护（zip slip / 符号链接穿透），PR #17 | `security: 升级包解压禁止越界写入（原始实现来自 PR #17）` |
| [@ismoss](https://github.com/ismoss) | 报告 #18 / #19 并提交 PR #20：修飞猪 H5 登录死循环、登录阶段可见化、双端弹窗去闪、按托运分档阈值 | `修复渠道登录流程（飞猪 H5 登不进）+ 新增按托运分档阈值` |

维护者会在此基础上做加固与门禁（见 `docs/CHANGELOG.md`），但**原作者身份保留在提交里**。

## 怎么加新的人

1. 用 `git am` 或 `git cherry-pick`（或 GitHub 的 Rebase/Squash merge）落库，
   确保 `git log --format=%an` 显示的是**贡献者本人**；
2. 在本文表格里加一行（贡献内容 + 提交标题）；
3. 跑 `python tools/release_guard.py --offline --no-collect --history-only`
   确认形态仍然合规。
