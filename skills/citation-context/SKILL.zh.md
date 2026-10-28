---
name: citation-context
description: 输入一篇论文（DOI、arXiv ID 或精确标题），抓取它的后向引用（它引了谁）与前向引用（谁引了它），按相关性排序并分簇，产出可核验的中文脉络叙述和单文件交互 HTML。当用户说“理一下这篇论文的引用脉络 / 它引了谁 / 谁引用了它 / 相关工作和它的关系 / 帮我做一份引用图谱”，或给出 DOI 要求分析引用关系时使用。
license: MIT
---

# Citation Context（中文版）

把一篇论文放进引用网络里：谁在它前面、谁在它后面、它和谁最相关，最后给出一份能点开看的报告。

> 英文主文件见 [SKILL.md](SKILL.md)；完整参数与 JSON 契约见 [references/](references/)（英文）。

## 何时使用

- 用户给出 DOI / arXiv ID，想弄清这篇论文的来龙去脉；
- 准备组会、开题、审稿意见，需要“承前—方法来源—启后—空白”这条线；
- 想知道某篇论文被谁引用、又建立在谁之上。

**不适用**：只想搜论文；想精读全文；中文文献（CNKI/万方无公开 API）。

## 快速开始

```bash
python scripts/citegraph.py doctor
python scripts/citegraph.py resolve "10.1364/prj.401755"
python scripts/citegraph.py graph   --run runs/<slug>
python scripts/citegraph.py label   --run runs/<slug>
python scripts/citegraph.py score   --run runs/<slug> --explain
python scripts/citegraph.py cluster --run runs/<slug>
python scripts/citegraph.py pack    --run runs/<slug>
#   → 由 agent 写 runs/<slug>/narrative.md
python scripts/citegraph.py lint    --run runs/<slug>
python scripts/citegraph.py layout  --run runs/<slug>
python scripts/citegraph.py render  --run runs/<slug>
```

## 产物

| 文件 | 说明 |
|---|---|
| `graph.json` | 节点、边、六项得分、证据、`schema_version` |
| `clusters.json` | 共被引分簇（id、名字、成员） |
| `pack.json` | 叙述唯一可用的输入；`constraints.allowed_nids` 是合法引用集 |
| `narrative.md` | 四段式叙述（agent 写） |
| `lint.json` | 校验结果（编号、数字、簇覆盖、方向） |
| `report.html` | 单文件、离线、可点击 |

## 叙述硬约束

1. 每个 `[n]` 必须在 `allowed_nids` 内；
2. 数字只能取自 `stats` / `nodes`；
3. 不写“证明了 / 首次 / 突破”，改用“被用于”或删掉；
4. 第 2 段必须覆盖每个 ≥3 篇的簇；
5. 引用语境只能转述，不能照抄，也不能保留原方括号编号；
6. `sampling.complete` 为 false 时必须写明“共 N 条、取 M 条”。

## 配置

`venue_weights`（期刊 1.0 / 会议 0.8 / 图书 0.7 / 仓储 0.35 / 预印本 0.2 / 未知 0.5）、
`weights`、`text_scale`、`limits.backward/forward`、`free_mode`。改完只需重跑 `score`。

## 安全

key 放 `~/.config/citation-context/config.json` 或环境变量 `OPENALEX_API_KEY`；
内置 `config.json` 是空模板。`doctor` 掩码显示，错误 URL 脱敏，`export-dist` 会拒绝含 key、
邮箱或本机路径的包。

## 常见问题

- `429 budget` → 等 UTC 零点 / 配 key / 加 `--free-mode`；
- `cluster_uncovered` → 第 2 段漏了一整条线；
- `render` 拒绝 → 没跑 `score`。

## 更多

- [workflow](references/workflow.md) · [schema](references/schema.md)
- [security](references/security.md) · [troubleshooting](references/troubleshooting.md)

## 许可证

MIT。
