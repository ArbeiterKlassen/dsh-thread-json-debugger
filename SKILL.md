---
name: dsh-thread-json-debugger
description: 诊断与修复 DSH 会话日志（session.v4.jsonl.zstd）的结构性损坏，并审计上下文/缓存成本失控的原因。当会话打不开、加载报错、提示 history 损坏、工具调用与结果对不上、seq 断号、turn 没闭合，或输入 token 涨得快、缓存命中率掉、技能目录注入太贵、同一工具被反复调用时使用。只读诊断在前，修复必须写到副本。Codex 版 codex-thread-json-debugger 的 DSH 适配版。
---

# DSH Thread JSON Debugger

DSH 没有 Codex 的 SQLite + rollout 双层结构，也没有 `history_base` 字节偏移。它的会话就是**一个 zstd 多帧串联的 JSONL**：首行 header，之后每行一个事件，`seq` 从 0 连续编号，`surfaceOp` 决定这条事件是追加还是替换掉一段。

因此 Codex 版那套"保持字节长度不变的等长修补"在这里完全不适用——那套约束存在的理由是偏移表，DSH 没有。这里要做的是：**读懂事件流 → 找出结构性错误 → 在副本上重排/补桩/截断 → 再校验一遍**。

## 三个脚本

都在 `scripts/`，纯 Python 标准库（zstd 走 ctypes 绑 `libzstd`，缺库回退 `zstd` CLI）。

```bash
# 1. 只读诊断：结构是否可解析、seq 是否连续、调用与结果是否配对
python scripts/scan_session.py                    # 默认最新会话
python scripts/scan_session.py --session 94811e6d  # id / id 前缀 / 目录 / 日志路径
python scripts/scan_session.py --all --limit 10    # 全部会话，问题最多的排前面
python scripts/scan_session.py --json

# 2. 上下文成本审计：钱花在哪、缓存怎么掉的
python scripts/audit_context.py
python scripts/audit_context.py --top 30 --quiet   # 只看结论 + 更多明细

# 3. 修复：先出计划，再写副本
python scripts/repair_session.py --truncate-tail --dry-run
python scripts/repair_session.py --truncate-tail --copy C:\Users\deepd\tmp\fixed\session.v4.jsonl.zstd
```

退出码：`scan_session.py` 0 干净 / 1 有问题 / 2 找不到会话；`repair_session.py` 0 成功（或干跑无操作）/ 1 无法修复 / 2 前置条件不满足。

## 修复开关（组合使用，按需）

| 开关 | 修什么 | 什么时候用 |
|---|---|---|
| `--truncate-tail` | 丢掉末尾无法解析的行（崩溃留下的半个 JSON 行） | 打不开会话、`scan_session.py` 报 `JSON parse error` |
| `--drop-seq 12,13` | 删掉指定 seq 的事件，随后重排 | 单条事件写坏了但其余完好 |
| `--drop-duplicate-results` | 同一个 `callId` 出现多条 `tool/result` 时只留最后一条 | 重放/复制导致的 `duplicate tool/result for callId ...` |
| `--stub-missing-results` | 为没有结果的 `tool/call` 追加 stub 结果 | provider 报 `No tool output found for tool call`、历史被截断 |
| `--renumber` | 把 seq 压成 0..N-1 并重映射所有引用 | 上面几条都改动了事件数量 |

引用重映射覆盖 `seq / startSeq / endSeq / baseSeq / anchorSeq / headerSeq` 六个标量键，以及 `sourceEventSeqs / eventSeqs / seqs` 三个数组键——`surfaceOp` 的替换区间、`tool/result` 的来源指向、`developer/message` 的 headerSeq 都在其中。

## 审计怎么读

`audit_context.py` 输出四块：

1. **模型与窗口**：`request/header`、`request/context` 里的 provider/model/effort/contextWindow；一个会话里出现多个模型是正常的（标题摘要类请求走小模型）。
2. **token 账**：`input / cacheRead / output / reasoning` 合计与命中率。命中率长时间低于 85% 说明前缀一直在被打断——最常见的原因是 `developer/message` 里的 `tool-addition`（工具注册表中途变了）和技能目录注入。
3. **载荷大户**：按重放字节排的事件列表。`user/message` 里塞着整套 `available_skills` 清单（本机实测单条 200–300KiB），这是**每轮都要重发**的固定成本；技能越多越贵。
4. **重复调用**：同一工具同参数被调用多次的清单，以及最差的那几次请求（input 大、cacheRead 接近 0）。

## 硬规则

1. **诊断永远只读**。`scan_session.py` / `audit_context.py` 不改任何字节。
2. **修复必须写副本**（`--copy`）。就地改（`--in-place --force`）只在 DSH 完全关闭时做，而且它不会修正内存里那份会话状态——重开前先备份。
3. **写完必须复检**：`repair_session.py` 落盘前会自己再跑一遍 `validate`，但落盘后仍要 `scan_session.py --session <副本>` 确认 `PASS structure valid`。
4. **别手改 zstd**。日志是"一帧一批"的 zstd 串联，逐行重写会让帧数变化（文件可能略变大），这是预期，不是损坏。
5. **不认识的 `type` 不要删**：`validate` 只对已知类型做形状检查，未知类型原样保留。

## 已验证的事实

- 事件信封：`{type, seq, time(epoch ms), data}`；`assistant/message` 还带 `stream`（原始 chunk 数组，只占体积、不参与重放）与 `usage{inputTokens,outputTokens,cacheReadTokens,reasoningTokens}`。
- `tool/call.data = {turn, step, callId, name, arguments}`，`arguments` 是 JSON 字符串；`tool/result.data = {turn, step, message:{role:"tool", source:{kind:"tool",callId}, toolCallId, content, isError, id}}`。
- 运行中的会话尾部必然有 `tool/call is in flight` 与 `turn opened ... never ended` 两条 warning，是正常状态，不要当错误修。
- 实测故障件（`C:\Users\deepd\tmp\faults\`）：末行截半 → `line 959: JSON parse error: Unterminated string starting at: line 1 column 105 (char 104)`，`--truncate-tail` 修好；复制一条结果 → `line 39: duplicate seq 36`，`--drop-duplicate-results --renumber` 修好；删掉一条历史结果 → 大量 `seq gap/regression`，`--stub-missing-results --renumber` 补 32 个 stub 后 PASS。
- 本机真实会话审计样本：108 请求，`input 625,450 / cacheRead 10,967,680 / output 101,300`，命中率 94.6%；最大载荷是 4 条技能目录注入的 `user/message`（263–292KiB）。

## 与其他技能的关系

- 图片导致的请求体过大 / 413 → 用 `dsh-thread-image-cleaner`（append-only 的 `image/offload`），本技能只管结构性损坏和成本审计。
- 工具输出太多（读了整份大文件）→ DSH 自带的 `dsh-compaction-tool-result-pruner` 会裁剪，审计里能看到哪些事件是主要来源。
- 会话日志位置：`<DSH_HOME>\sessions\--C-Users-<user>--\<session-id>\session.v4.jsonl.zstd`，投影缓存是 `<DSH_HOME>\storages\session_projcache\sessions\<id>.json`（含 title 与累计 tokenUsage）。
