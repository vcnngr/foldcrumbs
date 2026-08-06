# foldcrumbs

[![tests](https://github.com/vcnngr/foldcrumbs/actions/workflows/test.yml/badge.svg)](https://github.com/vcnngr/foldcrumbs/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/foldcrumbs.svg)](https://pypi.org/project/foldcrumbs/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) · [Italiano](README.it.md) · **中文**

面向编码 agent 的跨会话持久记忆 — **无需 Docker、无需向量数据库、无需外部服务**。

`/clear` 和压缩(compaction)会在每个会话中抹掉 Claude Code 的记忆。foldcrumbs 维护一个
存放类型化记忆文件的小文件夹，让 agent 重新打开时就已经知道你的决策、约定和代码库事实。
它还能对抗上下文腐化(context rot)：在上下文达到约 45% 时在后台对记忆做检查点，并提醒你
使用 `/compact` 或 `/clear` — 什么都不会丢失。

同一项目上的多个 CLI 实例（`claude`、`claude-work` 等）各自保留自己的存储，
但能以只读方式看到彼此的记忆 — 参见[联邦](#多实例单项目联邦)。

## 工作原理

```
STORE     markdown 文件 + MEMORY.md 索引，位于
          ~/.claude/projects/<project>/memory/
RECALL    使用 Claude Code 自带的 Grep/Read（无 LLM、无向量数据库）
          + SessionStart 注入索引
DISTILL   异步、仅本地 LLM（通过 env 使用 MLX/Ollama/OpenRouter），
          在约 45% 上下文和会话结束时进行 → 有门槛、去重
ANTI-ROT  PostToolUse 监控器 → 检查点 + 提醒（不强制压缩）
          PostCompact → 压缩后重新注入索引
HANDOFF   每个检查点还会写入一份实时工作状态快照，在 SessionStart
          重新注入 → /clear 后能恢复原来的任务
FEDERATE  每个已注册实例发布一个索引分片；每个会话还能以只读方式
          看到其他实例的记忆，并给出路径供 grep 使用
```

检索引擎就是 agent 本身：它在需要时对文件夹做 grep。LLM **只**用于异步蒸馏 —
因此召回是即时的，也从不依赖某个模型是否在线。

蒸馏还会执行一次**矛盾检查**：当一条新记忆与一条旧记忆涉及同一主题（一个被推翻的
决策、一件"已推迟"但后来已发生的事）时，会询问 LLM 新记忆是否使旧记忆过时 — 如果是，
旧记忆会被标记为 superseded（文件保留在磁盘上，但从索引中移除；`prune` 会清理它）。
仅靠去重无法捕捉这种情况：它只能合并几乎相同的文本。用 `FOLDCRUMBS_NO_AUTO_SUPERSEDE=1`
禁用；没有 LLM 时什么都不变。

纯 Python 标准库：hook 脚本永远不会因为缺少 import 而失败。

`MEMORY.md` 索引以**确定性顺序**写入（按不可变的创建时间排序，每种类型内最新的
在前），因此信任度提升、重新触碰或重新蒸馏都不会打乱已有条目。只有增删记忆才会改变
这个文件。这让 SessionStart 注入的前缀在不同会话间保持一致 — 从而搭乘 agent 自身的
提示缓存而不是打碎它 — 也让文件在 Syncthing 等同步工具下保持干净的 diff。

## 与 memanto 的不同

foldcrumbs 源于 [memanto](https://github.com/moorcheh-ai/memanto) 中的一些想法，但刻意
采取了不同的形态：

| | memanto | foldcrumbs |
|--|--|--|
| 检索 | Moorcheh 引擎（闭源） | agent 自己的 grep — 没有引擎 |
| 占用 | Docker + 引擎 + LLM + REST API | 一个文件夹 + hook |
| LLM | 检索和回答都需要 | 仅用于异步蒸馏；召回从不需要 |
| 抗腐化 | — | 上下文监控器 + 约 45% 时的检查点 |
| 依赖 | 服务栈 | 零运行时依赖（标准库） |
| 范围 | 工具无关的服务 | 按项目划分的记忆，位于 agent 侧 |

这里的原创工作是架构本身：基于 grep 的召回、文件存储 + 索引、抗腐化监控器、
可合并安装的 installer、hook 和 CLI。改编自 memanto 的部分见 **致谢**。

## 快速开始

从零到一个可用的存储，只需三十秒：

```bash
pip install foldcrumbs
cd your-project
foldcrumbs install          # 接入 Claude Code 的 hook + slash 命令
```

就这样。下一个 Claude Code 会话将以一个空但已激活的存储开始，
记忆会随着你的工作逐渐积累。用 `foldcrumbs status` 验证。

## 安装

```bash
pip install foldcrumbs                  # 从 PyPI（或：在 checkout 中 pip install -e .）
```

然后把它接入你的 agent：

```bash
foldcrumbs install                      # Claude Code，全局（~/.claude/settings.json）
foldcrumbs install --local              # Claude Code，项目级（.claude/settings.json）
foldcrumbs install --agent codex        # Codex：hooks.json + 打印 config.toml 的 MCP 片段
foldcrumbs install --agent opencode     # OpenCode：opencode.json MCP + 插件 + AGENTS.md 块
```
installer 是可合并且幂等的：它只追加自己的 hook 组，不动已有的 hook
（GSD、graphify 等）。会先写入一份 `.foldcrumbs-bak` 备份。

对于 Claude Code，installer 还会写入四个 slash 命令 — **`/remember`**、
**`/recall`**、**`/forget`**、**`/foldcrumbs`**（仪表盘）— 让记忆成为会话内的能力，
而不仅仅是后台层。不带参数的 `/remember` 会使用会话自身的模型（带确认）从当前对话中
蒸馏持久记忆 — 无需 LLM 后端。这些文件被标记为受管文件：编辑其中一个并删除标记行
即可取得所有权；`uninstall` 只删除我们自己的。请重启已打开的会话以生效。
hook 和 MCP 命令使用位于 `~/.foldcrumbs/runtime` 下的自包含运行时快照，因此
可编辑的 checkout 可以放在 `~/Documents` 这类受 macOS 保护的文件夹中而不破坏
agent 子进程。

在 TTY 上，install 会询问**如何蒸馏**（召回从不使用 LLM）：

```
1) claude-cli   Claude 订阅 — `claude -p`，无需 API key
2) codex        Codex 订阅 — `codex exec`，无需 API key
3) openai       OpenAI 兼容的 HTTP 端点（本地服务器或远程网关）
4) none         无 LLM — 仅关键词启发式（最后手段）
```

该选择按机器保存在 `~/.foldcrumbs` 中（不同步），因此一个共享存储可以让一个索引器
使用本地模型，其他索引器使用各自的 CLI 订阅。用 `foldcrumbs install --backend codex`
（或 `--no-backend-prompt`）跳过提问，随时用 `foldcrumbs backend <name>` 更改
（单独运行 `foldcrumbs backend` 显示当前选择）。

所有 agent 共享每个项目**同一个**记忆存储，因此在 Claude Code 中记录的决策
可以在 Codex 和 OpenCode 中被召回。

## 配置（env）

| 变量 | 默认值 | 含义 |
|-----|---------|---------|
| `FOLDCRUMBS_LLM_ENDPOINT` | `http://localhost:8081` | OpenAI 兼容端点（MLX 服务器） |
| `FOLDCRUMBS_LLM_MODEL` | `gemma-4-26b-a4b-it` | 模型名称 |
| `FOLDCRUMBS_LLM_API_KEY` | – | 可选 bearer token |
| `FOLDCRUMBS_CONTEXT_BUDGET` | `200000` | 监控器使用的上下文窗口大小（token） |
| `FOLDCRUMBS_CONTEXT_PCT` | `0.45` | 触发检查点 + 提醒的比例 |
| `FOLDCRUMBS_MIN_CONFIDENCE` | `0.7` | 写入门槛下限 |
| `FOLDCRUMBS_NO_AUTO_SUPERSEDE` | – | 设置后禁用蒸馏时的矛盾检查 |
| `FOLDCRUMBS_DIR` | 由 cwd 推导 | 覆盖记忆目录 |

通过修改 `FOLDCRUMBS_LLM_ENDPOINT` 把 LLM 换成远程网关或 OpenRouter — 召回
不受影响。

## CLI

```bash
python3 -m foldcrumbs status
python3 -m foldcrumbs remember "Recall is grep, no vector DB" --type decision --tag arch
python3 -m foldcrumbs remember "试用许可证覆盖 staging" --expires 2026-09-01   # 或 --expires 30d
python3 -m foldcrumbs recall "vector db" --type decision --tag arch   # 过滤器，可重复
python3 -m foldcrumbs index
python3 -m foldcrumbs distill transcript.txt    # 蒸馏持久记忆（LLM）
python3 -m foldcrumbs checkpoint transcript.txt # 写入一份恢复用交接（LLM）
python3 -m foldcrumbs handoff                   # 打印当前交接
python3 -m foldcrumbs answer "how does recall work?"
python3 -m foldcrumbs forget fact_wrong.md --apply   # 软删除（--hard 直接删除文件）
python3 -m foldcrumbs supersede decision_old.md --by decision_new.md
python3 -m foldcrumbs conflicts                      # 对账队列（含糊的对、主张）
python3 -m foldcrumbs decay                          # 归档低信任记忆（dry-run；--apply 写入）
python3 -m foldcrumbs restore fact_old.md            # 恢复一条已归档的记忆
python3 -m foldcrumbs import --from ~/.claude/projects/<slug>/memory --apply

python3 -m foldcrumbs profile list                   # 所有已注册的 profile
python3 -m foldcrumbs profile add kimi --kind dedicated
python3 -m foldcrumbs profile env kimi               # 选中它所需的那一行 env
```

`decay` 是归档 — 从不删除。一条信任度跌破阈值（0.3）**且**已 30 天未被触碰
的记忆会被移到 `status: archived`；它离开索引和召回，但留在磁盘上。`restore
<name>` 可以完整地恢复它，而 `prune --apply` 仍是那个单独的、显式的
彻底删除文件的动作。默认为 dry-run。

有些事实自带日期 — 一个会结束的试用、一次"推到九月"的延期、一个截止日期。
`remember --expires <日期>` 会把它刻在记忆上（`2026-09-01`、`2026-09-01T12:00`，
或相对的 `30d`/`2w`/`6m`；只写日期表示那一天结束）。过了这个日期，记忆就在所有
已归档记忆会消失的地方变得不可见 — 索引、召回、联邦、去重 — 而文件原封不动地
留在磁盘上。`decay` 随后是把它归档的清扫（并标注 `(expired)`），`status` 会显示
什么已过期、下一个到期的是什么，而移除或移动文件里的日期就是你说它仍然有效的方式。
只有用户的明确意图才会设置过期时间：蒸馏从不猜测日期，因此任何记忆都不会
得到一个它未曾要求的静默定时器。

### Profile — 每个 agent 一个存储

一个 **profile** 是一个已注册的、带有名称和形态的记忆根：

- **dedicated** — 所有项目共用一个记忆目录；这是长期运行的 agent
  （CI 机器人、审查 agent）想要的；
- **shared** — 在某个 config 目录下*按项目*各有一个记忆目录；这是
  Claude Code 这类交互式助手的工作方式（遵循 `CLAUDE_CONFIG_DIR`）。

```bash
foldcrumbs profile add kimi-review --kind dedicated            # 一个目录，所有项目
foldcrumbs profile add work   --kind shared --path ~/.claude-work
foldcrumbs profile env kimi-review
# → export FOLDCRUMBS_DIR=/Users/you/.foldcrumbs/profiles/kimi-review
```

没有 `profile use`。一个进程读取哪个存储，由它的环境在它**启动之前**决定 —
CLI 无法回头修改启动它的那个 shell。所以 `profile env` 会打印出唯一有效的那一行，
你把它放到 agent 进程诞生的地方（shell rc 文件、worker 的环境、Hermes profile 的
`.env`）。让进程指向一个 dedicated profile，它就会以只读联邦视图看到机器上
所有已注册的 shared 存储。

`profile import --agent hermes --apply` 会为多 agent 运行时的每个 agent 注册一个
profile，让每个 agent 拥有自己的记忆（默认为 dry-run）。
`profile remove` 取消注册但不触碰记忆本身。

## 管理存储

每条记忆都有一个状态：**active** →（**superseded** | **deleted** | **archived**）→ *文件移除*。
只有 active 记忆会出现在 `MEMORY.md` 和召回中。非 active 文件留在磁盘上 —
可审计、可恢复（`restore` 可以复活一条已归档的记忆）— 直到 `foldcrumbs prune --apply`
才真正删除它们。

记忆不再成立的三种方式：

**你说它是错的 — `forget`。** 接受 `MEMORY.md`（或召回结果）中显示的
确切文件名。与 `prune` 一样，默认为 dry-run：

```bash
foldcrumbs forget fact_wrong.md                 # dry-run：展示会发生什么
foldcrumbs forget fact_wrong.md --apply         # 标记 status: deleted，保留文件
foldcrumbs forget fact_wrong.md --apply --hard  # 立即删除文件
foldcrumbs forget "wrong deploy"                # 不是文件名 → 列出候选文件
```

MCP agent 通过 `forget` 工具获得同样的能力（仅软删除）。

**有东西取代了它 — `supersede`。** 你同时指向两边；旧记忆保留一条
指向新记忆的 `superseded_by` 链接，其信任度坍缩为 0：

```bash
foldcrumbs supersede decision_pypi_deferred.md --by fact_published_to_pypi.md
```

**蒸馏自己注意到 — 矛盾检查。** 去重只合并*几乎相同*的文本；一个被推翻的
决策读起来完全不同。所以在蒸馏时，当新记忆与旧记忆涉及同一主题（粗糙的词干
重叠用来挑选候选）时，会问 LLM 一个问题：*新记忆是否使旧记忆过时？*只有明确的
"是"才会做 supersede。例如：当蒸馏出"已发布到 PyPI"这条新事实时，旧的
"PyPI 发布已推迟"决策会被自动 supersede。失败安全（没有 LLM → 什么都不变）；
用 `FOLDCRUMBS_NO_AUTO_SUPERSEDE=1` 禁用。supersede 事件记录在
`~/.foldcrumbs/foldcrumbs.log`。

**自行淡出 — `decay`。** 一条没人信任、也没人触碰的记忆不是错的，只是旧了。
`foldcrumbs decay` 找出信任度跌破 0.3 **且**已 30 天未被写入或验证的
active 记忆，把它们移到 `status: archived`。已归档记忆离开索引、召回和联邦
分片 — 其他实例不再看到它们 — 但文件留在磁盘上。`foldcrumbs restore <name>` 可以
恢复其中一条。清扫是显式的且默认为 dry-run；它绝不是召回的副作用，因此读取
永远不会悄悄改变存储的内容。

## 多实例，单项目：联邦

同时运行 `claude`、`claude-work`、`claude-peo` 等意味着各自一个
`CLAUDE_CONFIG_DIR`，因此**各自一个存储** — 在一个实例中记录的决策对其他实例
不可见。联邦为每个实例提供其他实例在同一个项目上学到内容的只读视图，实时
且不复制任何东西。存储保持分离、各自持有：一个实例只写自己的存储。

```bash
foldcrumbs install          # 每个实例自注册
foldcrumbs roots            # 谁在联邦中，它们的记忆在哪里
```

之后每个实例在 SessionStart 看到的内容：自己的 `MEMORY.md`，和以前完全一样，
后面跟着一个独立的块，列出其他实例的记忆目录和其中的条目，每条都带有绝对路径。
`recall`、`answer` 和 MCP 工具会在所有这些之上搜索，并为结果标注来源。

```
<foldcrumbs-federated>
Memory from this project's other agent instances. … READ-ONLY from here …

- claude-work: /Users/you/.claude-work/projects/<project>/memory
- claude-peo:  /Users/you/.claude-peo/projects/<project>/memory

- [claude-work] Recall is grep, no vector DB — the retrieval engine is the agent
  /Users/you/.claude-work/projects/<project>/memory/decision_recall_is_grep.md
</foldcrumbs-federated>
```

有三个性质是刻意设计的，每一个都付出了代价才做对：

**没有共享写入。** 每个实例在 `~/.foldcrumbs/projects/<project>/roots/<root-id>.json`
下发布自己的索引分片；读取方合并它们。一个共享索引意味着两个实例并发扫描和
重写，而原子替换只能防止文件撕裂，防不了过时。排序使用一个全序键
（类型、日期、root id、文件名），因此每个实例无需一个用于达成共识的共享文件
就能推导出相同的顺序。

**`MEMORY.md` 不被触碰。** 联邦从不编辑它，因此当只有其他实例在写入时它保持
字节级一致 — 这正是让注入前缀搭乘 agent 提示缓存的原因。联邦视图追加在它
之后，位于交接已经会让其失效的区域。

**只读是被强制的，不是被请求的。** 该块告诉模型这些文件属于别人，但
`write_memory`、`upsert` 和 `mark_superseded_on_disk` 也会直接拒绝外部记录。
当蒸馏发现的新记忆与另一个实例存储中的记忆矛盾时，它把该主张记录在自己的
记录上，联邦视图会把该条目标记为有争议 — 对方的实例仍是唯一能撤回其文件的
一方。

用 `foldcrumbs roots remove <id>` 退出共享视图；存储本身不受影响，只有显式的
`install` / `roots add` 才会把它带回来。

值得了解的局限：联邦是按机器的（root 注册进 `FOLDCRUMBS_STATE_DIR`，因此
指向不同 state 目录的实例互相看不见 — `status` 能判断时会说明）；一个不可达的
root 会保留它最后发布的条目并加以标记，而不是显得被清空了；并且**升级包之后
要重新运行 `foldcrumbs install`** — hook 运行在安装时准备好的运行时快照上，
所以仅升级包不会更新它们。

## 在存储之间共享记忆：`import`

存储按 **实例 × 项目** 划分命名空间：记忆位于
`<config-dir>/projects/<encoded-cwd>/memory/`，其中 `<config-dir>` 遵循
`CLAUDE_CONFIG_DIR`。运行多个实例（例如 `~/.claude`、`~/.claude-work`）时，
一个存储变得丰富而另一个在同一项目上从空白开始是*结构性*的。

弥合这一差距有两条路，它们回答的是不同的问题。**联邦**（见上文）让一个实例
*看到*其他实例的记忆，实时且不复制 — 这是大多数时候你想要的。`import` 是
**收养**它：在 `claude-work` 中成熟的决策真正成为你的，合并时提升信任度，
并且在那个实例消失后依然存在。联邦是展示；import 是取得所有权。

命令的两边：

- **目标**（写入方）— *运行命令*的实例的存储，即你的 `CLAUDE_CONFIG_DIR`
  （默认 `~/.claude`）+ 你运行命令时所在的目录；
- **来源**（`--from`）— 任意路径：直接指向记忆目录，或按同样的约定解析的
  项目目录。

```bash
# 用主实例填充 work 实例的存储（在项目目录中运行）：
CLAUDE_CONFIG_DIR=~/.claude-work foldcrumbs import \
  --from ~/.claude/projects/<slug>/memory --apply

# 把 work 实例学到的东西提升回主实例：
foldcrumbs import --from ~/.claude-work/projects/<slug>/memory --apply
```

它做什么 — 以及刻意不做什么：

| | |
|--|--|
| 记录级合并 | 每条记忆经过 `upsert`：新的 → 创建，近似重复 → **验证**已有的（提升信任度，不产生重复） |
| 跳过噪声 | `MEMORY.md`、`HANDOFF*`、没有 frontmatter 的文件、superseded/deleted 记录 — 死历史留在原地 |
| 先 dry-run | 默认展示 `{created, validated, skipped}` 计划；`--apply` 写入并重建索引 |
| 幂等 | 重新运行只做验证 — 可以安全地用作周期性手动同步 |
| 单向 | 双向 = 运行两次，每个方向一次 |
| 无 LLM | 矛盾检查**不会**在 import 时运行（可预测性）；与本地记忆矛盾的导入记忆会共存，直到蒸馏复审或你手动 `supersede` |

对比 `migrate --from`，那是一次性迁移用的原始文件复制。如果*主*存储在多台机器间
同步（例如 Syncthing），一个自然的模式是中心辐射：只从一台机器向主实例 import，
再从主实例刷新各台机器上的实例。

## 在 `/clear` 和 `/compact` 之后存活

有两层能跨越上下文切换：

- **持久记忆**（决策、规则、偏好、事实）— 总是在 SessionStart / PostCompact 时
  通过 `MEMORY.md` 索引重新注入。
- **工作状态交接** — 对*当前*任务、进行中的文件和下一步的单一覆盖式快照，
  在每个检查点写入并重新注入，因此即使经历硬 `/clear` 也能恢复原来的任务。

在约 45% 上下文时 foldcrumbs 会提醒你；选择 `/compact`（继续工作）或 `/clear`
（全新开始）— 无论哪种，下一轮都会被重新注入。随时可以用
`foldcrumbs checkpoint` 强制生成快照。

## 本地 LLM

蒸馏需要任意一个 OpenAI 兼容的聊天端点 — 把 `FOLDCRUMBS_LLM_ENDPOINT` 指向
你运行的那个即可。它只用于异步蒸馏，因此模型的冷启动对编辑器不可见，而且
**召回完全不需要模型**。

常见的本地服务器（都提供 `/v1/chat/completions`）：

```bash
# MLX — 仅 Apple Silicon，Mac 上最快
mlx_lm.server  --model <gemma-mlx-repo> --port 8081     # VLM 用 mlx_vlm.server

# Ollama — 跨平台（macOS / Linux / Windows）
ollama serve                                            # 端点 :11434/v1

# llama.cpp / LM Studio / vLLM — 同样 OpenAI 兼容
```

然后例如 `export FOLDCRUMBS_LLM_ENDPOINT=http://localhost:11434 FOLDCRUMBS_LLM_MODEL=qwen2.5`。
远程网关或 OpenRouter 的用法相同 — 只有环境变量不同。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## MCP 服务器

foldcrumbs 附带一个极简 MCP 服务器（stdio、仅标准库 — 不依赖 `mcp` SDK），向任何
MCP 客户端提供 `remember`、`recall`、`answer` 和 `forget`：

```bash
foldcrumbs-mcp            # 或：python3 -m foldcrumbs.mcp_server
```
Codex 和 OpenCode 由 `foldcrumbs install --agent …` 接入它。注册上面的命令，即可
从任何支持 MCP 的工具直接使用。

## 各 agent 的接入方式

| Agent | 启动时注入 | 捕获 | 备注 |
|-------|-----------|---------|-------|
| Claude Code | SessionStart hook | PostToolUse 监控器 + SessionEnd | 完整生命周期 hook |
| Codex | SessionStart hook（`additionalContext`） | Stop + PostToolUse hook | 相同脚本；+ MCP 提供会话内工具调用 |
| OpenCode | AGENTS.md → agent 调用 `recall`（MCP） | 插件 `session.idle`/`session.compacted` | 没有可注入的 hook，因此由提示词驱动召回 |

## 路线图

- **阶段 1 ✓** — Claude Code：文件存储、grep 召回、蒸馏、抗腐化。
- **阶段 2 ✓** — Codex + OpenCode 通过标准库 MCP 服务器 + installer 共享同一存储。
- **阶段 2.5 ✓** — 联邦：多个 CLI 实例在不合并存储的前提下共享一个项目的
  只读视图。
- **阶段 2.7 ✓** — 记忆工程：召回强化与排序中的新鲜度、执行归档的 decay
  清扫、具名 profile（每个 agent 一个存储），以及 `/remember` `/recall`
  `/forget` `/foldcrumbs` slash 命令。
- **阶段 3** — embeddings + 开放向量数据库，仅在规模超出 grep 时引入；通过 OCR 摄取文档。

发布历史：[CHANGELOG.md](CHANGELOG.md)。

## 致谢

foldcrumbs 改编自 [memanto](https://github.com/moorcheh-ai/memanto) 的一些工具
（MIT，© Moorcheh / Edge AI Innovations）：类型化记忆分类和信任度/衰减模型、
会话蒸馏方法、转录读取辅助函数，以及上下文块的渲染思路。这些在这里针对文件存储
重新实现；没有使用 Moorcheh 检索引擎。完整声明见 [LICENSE](LICENSE)。感谢 memanto
的作者以 MIT 协议发布它。

## 许可证

MIT — 见 [LICENSE](LICENSE)。
