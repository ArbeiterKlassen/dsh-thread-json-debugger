# dsh-thread-json-debugger

诊断与修复 DeepSeek Harness（DSH）会话日志的**结构性损坏**，并审计会话的**上下文/缓存成本**失控。

这是 Codex 生态里 `codex-thread-json-debugger` 技能的 DSH 适配版。Codex 那边修的是 SQLite + rollout JSONL 且必须"等长字节修补"；DSH 这边是 zstd 多帧 JSONL、seq 单调递增，检修思路完全不同。

## DSH 会话日志长什么样

路径：`%USERPROFILE%\.dsh\sessions\<workspace-slug>\<session-id>\session.v4.jsonl.zstd`

- **zstd 多帧串联**：每次追加写一个独立 frame，不是单一压缩流。
- 解压后是 JSONL：第一行是 header，之后每行一个事件。
- header：`{"type":"session","version":4,"id":…,"createdAt":…,"cwd":…,"parentSession":…,"isSeeded":…,"delegationDepth":…,"agentPreset":"standard"}`
- 事件：`{"type":"tool/call","seq":12,"time":1770000000000,"data":{…}}`，**seq 从 0 起连续无缺口**。
- `surfaceOp` 只有两种：`"append"` 与 `{"op":"replace","startSeq":N,"endSeq":N}`，据此可以重放出"当前 surface 节点集合"。
- 消息载荷：`user/message` 在 `data.content`；`assistant/message`、`tool/result`、`system/message`、`developer/message` 在 `data.message.content`。

## 什么时候用

**修损坏：**

- 会话打不开 / 加载报错
- 工具调用和结果对不上（类似 Codex 的 `No tool output found for tool call ...`）
- `seq` 断号、重复、回退；turn/step 没闭合
- 客户端崩溃留下的半截行（torn tail）

**查成本：**

- 输入 token 涨得飞快、缓存命中率掉下来
- `/compact` 之后上下文又爆了
- 想知道哪条消息在反复重放、哪个工具被重复调用、技能目录注入占了多少

## 安装

```bash
git clone https://github.com/ArbeiterKlassen/dsh-thread-json-debugger.git \
  "$USERPROFILE/.dsh/skills/dsh-thread-json-debugger"
```

依赖：Python 3.9+，**纯标准库**（zstd 走 ctypes 绑 `libzstd`，找不到回退 `zstd` CLI）。

## 工具

```bash
cd "$USERPROFILE/.dsh/skills/dsh-thread-json-debugger/scripts"

python scan_session.py                      # 结构体检：问题 + 警告 + PASS/FAIL
python scan_session.py --all --limit 10     # 所有会话按严重程度排序
python audit_context.py                     # 成本审计：命中率 / 最大重放 / 重复调用
python audit_context.py --top 20 --quiet    # 只看结论

python repair_session.py --copy %TEMP%\fixed\session.v4.jsonl.zstd --truncate-tail
python repair_session.py --in-place --force --drop-duplicate-results --renumber
```

| 脚本 | 作用 |
|---|---|
| `scan_session.py` | 只读结构校验：header / seq 连续性 / 事件形状 / tool-call↔result 配对 / turn-step 平衡 / `image/offload` 声明 |
| `audit_context.py` | 成本审计：token 合计、缓存命中率、最大重放事件、`stream` 冗余、重复工具调用、工具注册表变更 |
| `repair_session.py` | 修复：截尾、丢重复、补缺失结果、重编号（**默认写副本**） |

### 修复开关

| 开关 | 作用 |
|---|---|
| `--truncate-tail` | 丢掉尾部无法解析的行（崩溃留下的半截帧） |
| `--drop-seq` | 按 seq 删除事件，随后重编号 |
| `--drop-duplicate-results` | 同一 callId 有多个结果时保留第一个 |
| `--stub-missing-results` | 给没有结果的 tool/call 补占位结果 |
| `--renumber` | 压平 seq 并同步重映射 `sourceEventSeqs` / `startSeq` / `endSeq` / `headerSeq` 等引用 |
| `--copy` / `--in-place --force` | 写副本（推荐）/ 原地改（要求 DSH 已关闭） |

## 已验证（故障注入后的真实数据）

| 注入的故障 | `scan_session.py` 报的错 | 修复 |
|---|---|---|
| 末行截半 | `line 959: JSON parse error: Unterminated string starting at: line 1 column 105 (char 104)` | `--truncate-tail` → 957 事件，0 problem |
| 复制一条 tool/result | `line 39: duplicate seq 36` + `seq gap/regression, expected 37 got 36` | `--drop-duplicate-results --renumber` |
| 删掉一个旧 tool/result | 多处 `seq gap/regression` | `--stub-missing-results --renumber`，补 32 个 stub |

三个修复件复检全部 `PASS structure valid`。

`audit_context.py` 在真实会话（126 请求）上的输出：

```
input 757,005 | cacheRead 13,275,520 | output 118,930 | hit 94.6%
max replayed event 293.9 KiB @ seq 1070 (user/message, 技能目录注入)
5× developer/message tool-addition (每次打断前缀缓存)
assistant stream chunks 1.7 MiB (只占磁盘，不参与重放)
```

## 硬规则

1. **先只读诊断，再动手修。** `scan_session.py` 和 `audit_context.py` 绝不写文件。
2. **修复写副本。** 原会话是你唯一的证据；`--copy` 是默认推荐路径。
3. **原地修要求 DSH 关闭。** 运行中的客户端持有 seq 计数器，`--in-place` 必须配合 `--force`，且脚本会拒绝在客户端运行时执行。
4. 未回应的 `tool/call` 若 seq 大于最后一个 `turn/end`，算 **in flight 警告**而不是损坏——别急着修。
5. 会话日志格式随 DSH 版本演进；不认识的类型会被跳过并记为未知，不会让校验崩溃。

## 许可

MIT
