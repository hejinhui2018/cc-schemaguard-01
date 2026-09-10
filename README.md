# datacontract — 数据契约治理工具

零依赖（仅 Python 3.11 标准库）的数据契约治理库与 CI 门禁工具。回答两类问题：

1. **单份契约的两个版本是否兼容**（`check`）：结构化 diff → 前向/后向兼容性判定 →
   有序迁移步骤。用于拦上游自己的发版。
2. **这次上游发版会不会打挂下游**（`evaluate`）：把上游变更投影到每个下游
   「实际消费的数据结构」上，按下游给结论、按执行者出迁移步骤、给出治理放行口径。
   用于上游一次改动挂几十个消费方时的发版治理。

clone 之后即可运行，不需要任何外部服务、数据库或网络。

## 快速开始

```bash
# 单份契约两版本对比（现有能力）
python -m datacontract check examples/user_v1.json examples/user_v2_breaking.json

# 多下游发版评估（新能力）
python -m datacontract evaluate examples/user_v1.json examples/user_v2_breaking.json \
    --consumers examples/consumers

# 跑全部测试
python -m unittest discover -s tests
```

退出码：`0` 放行；`1` 阻断；`2` 用法错误或输入非法。

## 契约写法

契约是 JSON 文件，顶层形如 `{"version": "1.0", "defs": {...}, "root": <类型声明>}`；
若没有 `root` 且顶层本身带 `type`/`$ref`，顶层即根类型声明。

类型声明：

```jsonc
{
  "type": "object",
  "fields": [
    {"name": "id", "type": "integer"},                          // 必填
    {"name": "nick", "type": "string", "optional": true},       // 可选
    {"name": "status", "type": {"type": "string", "enum": ["A", "B"], "default": "A"}},
    {"name": "tags", "type": {"type": "array", "items": "string"}},
    {"name": "address", "type": "Address"}                      // 引用 defs 中的类型
  ],
  "additionalProperties": false
}
```

- 标量：`string` / `integer` / `number` / `boolean` / `null`，可带 `enum`、`default`；
- 字段的 `type` 可直接写标量名、`$ref` 名或嵌套声明；
- `defs` 内支持前向引用与递归引用（如树形结构）；
- 字段级 `default` 挂在字段类型上，`{"$ref": "X"}` 上不能直接挂 default。

## `check`：单份契约两版本对比

```bash
python -m datacontract check OLD.json NEW.json [--format human|json] [--fail-on error|warning|never]
```

- **前向兼容（forward）**：按老契约写的消费者能否读取新契约产生的数据；
- **后向兼容（backward）**：按新契约写的消费者能否读取历史遗留的老数据；
- 每条结论都带规则 ID、严重级别、数据路径和导致它的具体变更，可逐条追溯；
- 不兼容时输出 expand-contract 模式的有序迁移步骤（每步标注执行者、
  可回滚性、关联规则与路径）。

### 兼容性规则

F=前向（老消费者读新数据），B=后向（新消费者读老数据）。
ERROR 阻断，WARNING 提示；完全良性的变更不产生 finding。

| 变更 | 前向 | 后向 |
|---|---|---|
| 新增字段 | F001 警告（严格解析者注意） | B001 ERROR（必填且无默认值） |
| 删除字段 | F002 ERROR（删必填） | B002 警告 |
| 可选→必填 | 安全 | B003 ERROR |
| 必填→可选 | F004 ERROR | 安全 |
| 枚举新增取值 | F010 ERROR | 安全 |
| 枚举移除取值 | 安全 | B011 ERROR |
| 枚举替换 | F012 ERROR | B012 ERROR |
| 新增枚举约束 | 安全 | B013 ERROR |
| 移除枚举约束 | F014 ERROR | 安全 |
| integer→number | F020 警告 | 安全 |
| number→integer | 安全 | B020 警告 |
| 其他类型变化 | F021 ERROR | B021 ERROR |
| 默认值增/删/改 | F030/F031/F032 警告 | B030/B031/B032 警告 |
| additionalProperties 变化 | F040 警告 | B040 警告 |

## `evaluate`：多下游发版评估

`check` 只能回答「这份契约相对上一版是否兼容」。当上游一次改动挂着几十个消费方时，
治理组需要的是：**哪些下游会出问题、问题出在哪、这次发版该不该放行**。

```bash
python -m datacontract evaluate OLD.json NEW.json \
    [--consumer 画像.json ...] [--consumers 画像目录/ ...] \
    [--format human|json] [--fail-on critical|any|never]
```

- `--consumer FILE`：单个下游画像，可重复；
- `--consumers DIR`：加载目录下全部 `*.json` 画像，可重复；
- 不提供任何画像时退化为只评估上游自身兼容性（前向不兼容会标注「影响面未知」）。

### 下游消费画像

每个下游维护一份画像，声明「我实际消费的数据结构」：

```json
{
  "name": "payment-service",
  "critical": true,
  "strict": false,
  "contract": {
    "root": {
      "type": "object",
      "fields": [
        {"name": "id", "type": "integer"},
        {"name": "status", "type": {"type": "string", "enum": ["ACTIVE", "DISABLED"]}}
      ]
    }
  }
}
```

- `name`：下游标识，评估结论与迁移步骤都按它归因（多个画像不得重名）；
- `critical`：是否关键下游，治理放行口径使用（默认 `false`）；
- `strict`：是否严格解析（遇到未知字段字段即失败，默认 `false`）。
  为 `true` 时，上游在其解析范围内新增字段会从「提示」升级为「阻断」；
- `contract`：下游实际读取的字段与期望类型，写法与上游契约完全相同
  （支持 `defs`/`$ref`/嵌套）。**只写真正消费的字段**——上游多出来的字段
  不影响它，它用到的字段被改掉一定会被发现。

评估语义：画像描述的是下游**当前**（基于上游老契约）的消费方式，
`evaluate` 回答「这次上游变更会不会破坏它」——把 old → new 的结构化差异
投影到各下游的消费点上，逐条套用前向（老消费者读新数据）规则。

### 按下游解读结论

输出中每个下游独立一节：

```
下游评估（共 3 个，其中关键下游 1 个）：
  ✓ analytics（非关键）：通过（0 个警告）
  ✗ payment-service（关键）：3 个阻断项, 0 个警告
      ✗ $.id  [F021/error]  类型由 integer 变为 string，...
```

- `✓` = 按当前消费方式能正确读取新数据（可能有警告）；
- `✗` = 会被这次变更打挂，下面逐条列出是**哪些差异**导致的
  （路径 + 规则 ID + 说明，JSON 输出里还能追溯到具体 change）。

### 迁移计划怎么读

```
迁移步骤建议（按执行顺序；producer=上游，consumer:<名>=具体下游）：
   1. [producer/error] 将 $.name 标记为可选并标注废弃，但继续下发
       路径: $.name（规则 F002，可安全回滚）
       ...（受影响下游：audit-log、payment-service）
   2. [consumer:payment-service/error] 下游 payment-service 移除对 $.name 的强依赖
       ...
   3. [producer/error] 停止下发并从契约删除 $.name
       ...；必须等 audit-log、payment-service 全部完成迁移后才可执行
       回滚: 回滚不安全：老版本消费方仍然读取该字段，...
```

- `producer` = 上游要做的（先做加法/双写/保持下发，最后才收窄）；
- `consumer:<名字>` = 某个下游自己要做的，按名字分工，不再是「全体消费方」；
- 同一变更服务多个下游时，上游步骤只出现一次，detail 里标注受影响下游名单；
- 收窄（删除/停发/收紧）类步骤**不可安全回滚**，并显式列出「必须等哪些下游
  全部迁移完成」；expand 阶段的加法步骤都可安全回滚；
- 标注「上游自身数据资产修复」的步骤与下游无关，是上游读历史数据的自我修复。

### 治理放行口径

`evaluate` 给出三档结论（JSON 字段 `verdict`），不是只有通过/不通过：

| verdict | 含义 | 默认是否放行 |
|---|---|---|
| `pass` | 全部下游按当前消费方式都能读新数据 | 放行 |
| `pass_with_warnings` | 只有个别**非关键**下游会被打挂 | 放行（但报告逐条列出） |
| `blocked` | **关键下游**仍按老方式消费且过不了，或上游自身后向不兼容 | 阻断 |

`--fail-on` 控制 CI 阈值：

- `critical`（默认）：只有 `blocked` 才返回退出码 1；
- `any`：`pass_with_warnings` 也返回 1（任何下游受损都不放行）；
- `never`：只出报告，永远返回 0。

每条阻断/警告原因都能追溯到**具体下游 + 具体差异**（`blocking_reasons` /
`warning_reasons`，含下游名、数据路径、规则 ID），例如：

```
治理结论: 阻断 ❌
  ✗ 关键下游 payment-service 无法读取新数据: $.status [F010] 枚举新增取值 ...
  ! 非关键下游 audit-log 无法读取新数据: $.email [F001] 新数据会多出字段 ...
```

## 可编程 API

```python
from datacontract import (
    parse_file, parse_consumer_file,
    evaluate, evaluate_paths,          # 单份对比（check）
    evaluate_governance,               # 多下游评估（evaluate）
)

old = parse_file("old.json")
new = parse_file("new.json")
consumers = [parse_consumer_file("consumers/payment-service.json")]

ev = evaluate_governance(old, new, consumers, fail_on="critical")
ev.report.verdict              # GateVerdict.PASS / PASS_WITH_WARNINGS / BLOCKED
ev.passed                      # 按 fail_on 阈值是否放行
ev.exit_code()                 # 0 / 1
for a in ev.report.consumers:  # 按下游的结论
    a.name, a.compatible, a.errors, a.relevant_changes
ev.report.plan.steps_for("payment-service")  # 某个下游自己要做的迁移步骤
ev.report.plan.producer_steps                # 上游要做的
ev.to_dict()                   # 完整 JSON 结构
```

## 项目结构与测试

```
datacontract/
  model.py         规范化类型树（IR）
  parser.py        契约 JSON 解析与校验
  diff.py          结构化版本差异
  compatibility.py 兼容性规则矩阵（前向/后向）
  migration.py     expand-contract 迁移步骤模板
  consumer.py      下游消费画像与消费点投影
  governance.py    多下游评估、放行口径、按执行者拆分的迁移计划
  cli.py           check / evaluate 命令行入口
tests/             unittest 测试（154 个）
examples/          示例契约与下游画像
```

运行全部测试（一条命令）：

```bash
python -m unittest discover -s tests
```
