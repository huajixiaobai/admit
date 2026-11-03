# admit

给自建 Agent 的准入判断层：**该不该主动动手**。

一个会自己干活的 Agent，缺的从来不是"能不能做"，而是"该不该做"。
`admit` 只回答后一半问题，别的什么都不管。

- 纯 Python 标准库，**零依赖**
- 能在手机上跑（proot / 任何 Python 3.9+ 环境）
- 判断与执行完全分离，判断过程无副作用

```python
from admit import Gate, Rule, Trigger

gate = Gate(
    rules=[Rule("来自白名单", lambda t: t.sender == "dsh")],
    allowed_senders=["dsh", "user"],
    mode="observe",          # 先只看不动
)

d = gate.judge(Trigger(sender="dsh", explicit=True))
print(d.explain())
# [observe] 本会执行｜explicit｜default/dsh
#   原因：条件满足，但当前为 observe 模式，只记录不执行
#     - 来自白名单: 通过

if d.accepted:               # observe 下永远是 False
    do_the_thing()
```

## 三个设计决定

### 一、四象限，而不是单一开关

两个维度交叉，得到四种情况：

```
                  是否明确点名
                 否          是
        ┌──────────┬──────────┐
允许  是│ inferred │ explicit │
        ├──────────┼──────────┤
      否│ ignored  │ ignored  │
        └──────────┴──────────┘
```

只有 `explicit` 才可能被放行。`inferred`（没点名但我猜他想让我做）
永远不自动执行——这正是绝大多数"Agent 自作主张"事故的来源。

### 二、三档模式，而不是二值开关

| 模式 | 行为 |
|---|---|
| `off` | 不判断也不动手，省算力 |
| `observe` | **判断、记录、绝不执行**。上线前的观察期 |
| `auto` | 判断通过就执行 |

`observe` 是这个库存在的核心理由。它让你能先跑一周，
看看"如果开了 auto 会发生什么"，再决定要不要真的开。

### 三、失败一律不放行

这条比前两条都重要：

- 规则函数抛异常 → 视为不通过，拒绝
- 没有配置任何规则 → 谁都不许动手
- 来源推断不出来 → 当作不可信身份处理
- 任意一条规则失败 → 整体拒绝

**规则短路也算事故。** 大多数框架用了 `any()`/`all()` 提前返回，
导致后面的规则根本没跑到。`admit` 强制每条规则都被评估一次，
日志里能看到完整的判断过程，而不是只看到第一个不通过的理由。

## 安装

```bash
pip install admit
```

或者直接复制 `admit/` 目录到你的项目里——它没有依赖，复制走就行。

## 命令行

```bash
# 单次判断
admit judge --sender dsh --explicit --mode observe

# 扫描信箱目录（文件即队列）
admit run --inbox /sdcard/dsh_inbox \
          --rules admit_rules.json \
          --mode observe \
          --allow dsh,user \
          -v --dry-run

# 内置自检，不用装测试框架
admit selftest
```

## 规则文件

```json
{
  "rules": [
    { "name": "来源可信", "type": "sender_prefix", "value": "dsh" },
    { "name": "体积可控", "type": "max_size", "value": 65536 }
  ]
}
```

内置规则类型刻意只有四种：`sender_prefix`、`filename_suffix`、
`max_size`、`not_contains`。

**规则类型越多，配置就越像一门编程语言，最后必然变成没人看得懂的 DSL。**
真的要复杂逻辑，用 `Rule(name, fn)` 直接写函数，别往 JSON 里塞。

## 接进现有的脚本

`admit` 可以在 `handler.sh` 里当一道闸：

```bash
# 先判断，只有 auto 模式且通过才返回 0
python3 -m admit.cli judge --sender dsh --explicit \
        --rules admit_rules.json --mode observe
if [ $? -eq 0 ]; then
    sh do_the_work.sh
fi
```

注意 `judge` 在 `observe` 下返回 1，所以上面的写法在观察期永远不会执行。
这正是想要的效果。

## 关于灵感来源

这个库的**思路**来自 DeepSeek 在 2026-10-31 开源的
[`dsh-superagent-prototype`](https://github.com/deepseek-ai/dsh-superagent-prototype)
（MIT 许可）——特别是它的 `policy.gate()` 四象限矩阵、
`off/observe/auto` 三档模式、以及"必须对每条规则恰好判断一次"的约束。

但那个项目是**深度绑定 dsh 运行时的 TypeScript bundle**，
你没法把它单独拿出来用在别处。

`admit` 是**独立重写的 Python 实现**，不包含任何来自原项目的代码。
它的存在理由是让这套思路能在任何地方跑起来——包括一台只有 6GB 可用空间的安卓手机上。

如果你在找一个功能完整的主动 Agent 框架，去用原项目。
如果你只是想让自己的脚本在动手之前先问一句"我该不该动"，用这个。

## 状态

Alpha。API 可能变。

测试：`python3 -m unittest discover -s tests -v`（20 项，全过）

## 许可

MIT。见 [LICENSE](LICENSE)。