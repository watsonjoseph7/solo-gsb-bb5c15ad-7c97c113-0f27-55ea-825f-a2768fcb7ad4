# Ensemble Beat Grid API

合奏节拍网格服务：提交一小节的**目标时长**和**至少两个声部**（每个声部是依次排列的
发声/休止事件，时长为正有理数字符串），服务用**精确分数**校验各声部总时长是否等于
目标；通过后保存小节，并可按 ID 查询由所有事件边界并集切分出的**半开时间段网格**。

技术栈：Python 3.12 · FastAPI · SQLite（标准库 `sqlite3`）· Docker / Compose

## 启动（一条命令）

```bash
docker compose up --build
```

该命令完成依赖安装、SQLite 数据库初始化（应用启动时自动建表）并启动服务，**无需任何
额外步骤**；服务监听容器内 **8000** 端口，Compose 将宿主机 **8000** 端口映射到它。

- API 地址： http://localhost:8000
- 交互式文档（Swagger UI）： http://localhost:8000/docs

## 配置

| 环境变量         | 默认值          | 说明                     |
| ---------------- | --------------- | ------------------------ |
| `DATABASE_PATH`  | `/data/app.db`（容器内）/ `./app.db`（本地） | SQLite 数据库文件路径 |
| （端口映射）      | `8000:8000`     | 在 `docker-compose.yml` 中修改 |

Compose 中数据库文件挂载在命名卷 `beat-grid-data` 上，容器重启数据不丢失。

## API

### `POST /measures` — 提交小节

```bash
curl -s -X POST http://localhost:8000/measures \
  -H 'Content-Type: application/json' \
  -d '{
    "target_duration": "4/4",
    "voices": [
      {"name": "melody", "events": [
        {"kind": "sound", "duration": "1/2"},
        {"kind": "rest",  "duration": "1/4"},
        {"kind": "sound", "duration": "1/4"}
      ]},
      {"name": "bass", "events": [
        {"kind": "rest",  "duration": "1/4"},
        {"kind": "sound", "duration": "3/4"}
      ]}
    ]
  }'
```

- 时长格式：正有理数字符串，如 `"2"`、`"1/4"`、`"3/8"`（拒绝 `0`、负数、小数、
  分母为 0）。所有时长以 `fractions.Fraction` 精确累加，无浮点误差。
- 任一一声部总时长 ≠ 目标时长时返回 **422**，逐声部给出 `expected` / `actual` /
  `difference`（= actual − expected，负值表示不足），且**不保存任何数据**：

```json
{
  "detail": {
    "message": "voice durations do not add up to the target duration; measure was not saved",
    "mismatches": [
      {"voice": "melody", "expected": "1", "actual": "3/4", "difference": "-1/4"}
    ]
  }
}
```

- 全部匹配时返回 **201** 及保存的小节（含每个事件的 ID `v{声部序号}e{事件序号}`、
  规范化的起止偏移）。

### `GET /measures/{id}` — 查询小节

返回小节、各声部及全部事件（种类、时长、起止偏移）。

### `GET /measures/{id}/grid` — 查询节拍网格

对所有事件的起止点取并集，按相邻点生成**半开时间段** `[start, end)`；每段列出各声部
正在发声的事件 ID，休止为 `null`。半开区间保证相邻事件边界处既不丢音也不重复计音：

```json
{
  "measure_id": 1,
  "target_duration": "1",
  "voices": ["melody", "bass"],
  "segments": [
    {"start": "0",   "end": "1/4", "events": {"melody": "v1e1", "bass": null}},
    {"start": "1/4", "end": "1/2", "events": {"melody": "v1e1", "bass": "v2e2"}},
    {"start": "1/2", "end": "3/4", "events": {"melody": null,   "bass": "v2e2"}},
    {"start": "3/4", "end": "1",   "events": {"melody": "v1e3", "bass": "v2e2"}}
  ]
}
```

### `POST /measures/{id}/quantization-preview` — 量化到等分刻度（预览，不写库）

把已保存小节**所有声部的不同事件边界**（各事件起止点的并集）统一映射到把目标时长
等分为 `divisions` 份的刻度网格上（刻度间隔 = 目标时长 / `divisions`）。映射必须：

1. **固定首尾**：边界 `0` 映射到刻度 0，边界 `目标时长` 映射到刻度 `divisions`；
2. **严格递增**：随边界顺序映射到严格递增的不同刻度（多个边界不能挤到同一刻度）；
3. **共享边界一致**：多个声部在同一时刻的边界只映射一次，量化后各声部在该点完全对齐；
4. **逐点限偏**：每个边界的偏移绝对值 `|量化后 − 原边界|` 都不超过 `max_shift`。

在所有可行映射中，使**偏移绝对值总和最小**；总和相同时，取**刻度序列字典序最小**
（越靠前的边界越优先取更小刻度）。全部运算使用 `fractions.Fraction` 精确完成。

- 请求体：`divisions` 为 **1～128** 的整数；`max_shift` 为**非负有理数字符串**
  （允许 `"0"`，其余规则同时长格式）。越界或格式错误返回 **422**（请求校验错误）。
- 小节 ID 不存在返回 **404**。
- 不存在满足上述条件的映射时返回 **422**，detail 给出 `reason` / `divisions` /
  `max_shift`；该接口为**只读预览，不写入数据库**，已保存小节保持不变。

```bash
curl -s -X POST http://localhost:8000/measures/1/quantization-preview \
  -H 'Content-Type: application/json' \
  -d '{"divisions": 8, "max_shift": "1/8"}'
```

例：小节时长 `1`，声部边界在 `1/3`、`1/2` 等处；8 等分刻度间隔 `1/8`，`1/3` 就近
映射到刻度 3（`3/8`，偏移 `+1/24`），`1/2` 恰在刻度 4，偏移总和 `1/24`：

```json
{
  "measure_id": 1,
  "target_duration": "1",
  "divisions": 8,
  "grid_step": "1/8",
  "max_shift": "1/8",
  "total_abs_shift": "1/24",
  "boundaries": [
    {"original": "0",    "tick": 0, "quantized": "0",   "shift": "0"},
    {"original": "1/3",  "tick": 3, "quantized": "3/8", "shift": "1/24"},
    {"original": "1/2",  "tick": 4, "quantized": "1/2", "shift": "0"},
    {"original": "1",    "tick": 8, "quantized": "1",   "shift": "0"}
  ],
  "voices": [
    {"name": "x", "events": [
      {"id": "v1e1", "kind": "sound", "start": "0",   "end": "3/8", "duration": "3/8"},
      {"id": "v1e2", "kind": "rest",  "start": "3/8", "end": "1",   "duration": "5/8"}
    ]},
    {"name": "y", "events": [
      {"id": "v2e1", "kind": "rest",  "start": "0",   "end": "1/2", "duration": "1/2"},
      {"id": "v2e2", "kind": "sound", "start": "1/2", "end": "1",   "duration": "1/2"}
    ]}
  ]
}
```

各字段含义：

| 字段               | 说明                                                                 |
| ------------------ | -------------------------------------------------------------------- |
| `grid_step`        | 等分刻度间隔（= `target_duration / divisions`，精确分数）            |
| `total_abs_shift`  | 全部不同边界偏移绝对值之和（最小化目标，精确分数）                   |
| `boundaries`       | 原边界 → 新边界对应关系：`original` / `tick`（刻度序号，0 起）/ `quantized` / `shift`（= quantized − original，可为负） |
| `voices[].events`  | 各声部**原事件 ID** 与种类，以及量化后的 `start` / `end` / `duration`；时长严格为正且逐声部首尾拼成目标时长 |

`max_shift` 过紧、或刻度数不足以容纳所有边界（内部不同边界数超过 `divisions - 1`）时
不可行，例如 3 等分网格（刻度在 `1/3`、`2/3`）下边界 `1/2` 距两个相邻刻度都是 `1/6`，
`max_shift = "1/8"` 时返回 **422**：

```json
{
  "detail": {
    "message": "no quantization of the measure's event boundaries onto the requested grid stays within max_shift; nothing was saved",
    "reason": "boundary 1/2 has no grid tick within max_shift 1/8",
    "divisions": 3,
    "max_shift": "1/8"
  }
}
```

### `GET /measures/{id}/rhythm-matches?voice={声部名}` — 检索节奏等价声部

以指定小节中的一个声部为**源声部**，在全部已保存小节中检索与它**节奏等价**的其他声部
（不含源声部自身）。两个声部节奏等价，当且仅当存在某个循环位移 `shift`（0 ≤ shift < N）
使下列条件同时成立：

1. 两段事件**数量相同**（都为 N）；
2. 候选的第 `j` 个事件与源的第 `(j + shift) mod N` 个事件**发声/休止种类逐项一致**；
3. 每一对对应事件的**时长之比都是同一个正有理数** `ratio`（= 候选时长 / 源时长，即整体
   变速比例；`"1"` 表示速度不变）——允许整体拉伸或压缩，但不允许内部相对节奏变形。

全部比较使用 `fractions.Fraction` **精确分数**完成，无浮点误差。若有多个可行位移，
取**最小**的 `shift`（从 0 起逐个尝试）。

- 查询参数 `voice` 必填，值为源声部名；源小节不存在、或该声部名不在源小节中时返回
  **404**；缺少 `voice` 参数返回 **422**。
- 结果按**小节 ID 升序**排列，同一小节内按声部的**原始保存顺序**排列；没有匹配时
  `matches` 为空数组。

```bash
curl -s 'http://localhost:8000/measures/1/rhythm-matches?voice=lead'
```

例：源小节声部 `lead` 为「发声 1/4、休止 1/4、发声 1/2」，另一小节的 `echo` 为
「发声 1、发声 1/2、休止 1/2」——整体时长放大 2 倍且循环位移 2 位后逐项对应：

```json
{
  "measure_id": 1,
  "voice": "lead",
  "matches": [
    {"measure_id": 1, "voice": "twin", "ratio": "1", "shift": 0},
    {"measure_id": 2, "voice": "echo", "ratio": "2", "shift": 2}
  ]
}
```

各字段含义：

| 字段         | 说明                                                                 |
| ------------ | -------------------------------------------------------------------- |
| `measure_id` | 候选小节 ID                                                          |
| `voice`      | 候选声部名（仅按 `(小节 ID, 声部名)` 排除源声部自身；同名的其他声部仍正常参与匹配） |
| `ratio`      | 候选/源的统一时长比例（正有理数字符串），`"2"` 表示候选整体慢一倍     |
| `shift`      | 循环位移位数：候选第 `j` 个事件对应源第 `(j + shift) mod N` 个事件，取最小可行值 |

### `POST /loop-alignments` — 两小节循环对齐（排练用）

接收两个**已保存小节**的 ID（同一小节可同时作为 A、B），把它们当作两条不同长度的
节奏循环，用精确分数求两条循环**首次同时回到起点**的时刻（两小节时长的最小公倍数），
并把两条循环的事件分别重复到该时刻。时间线按两条循环**全部事件边界**的并集切成
半开区间；每段分别在 `a` / `b` 下按各声部给出正在发声事件的**原事件 ID** 和
**从 1 开始的循环序号**，休止为 `null`。

- 任一小节 ID 不存在返回 **404**（detail 指明是 `a` 还是 `b`），不产生时间线。
- 展开的事件副本总数（= A 重复次数 × A 事件数 + B 重复次数 × B 事件数）超过
  **2000** 时返回 **422**，detail 给出 `expanded_events` / `limit` / `horizon` /
  `repetitions` 等信息，**不输出任何截断的时间线**（响应中没有 `segments`）。

```bash
curl -s -X POST http://localhost:8000/loop-alignments \
  -H 'Content-Type: application/json' \
  -d '{"a_measure_id": 1, "b_measure_id": 2}'
```

例：A 小节时长 `1`、B 小节时长 `3/2`，首次重合在 `3`（A 重复 3 次、B 重复 2 次）：

```json
{
  "a_measure_id": 1,
  "b_measure_id": 2,
  "horizon": "3",
  "length_a": "1",
  "length_b": "3/2",
  "repetitions": {"a": 3, "b": 2},
  "voices": {"a": ["melody", "bass"], "b": ["melody", "bass"]},
  "expanded_events": 21,
  "segments": [
    {
      "start": "0",
      "end": "1/4",
      "a": {"melody": {"event_id": "v1e1", "loop": 1}, "bass": null},
      "b": {"melody": {"event_id": "v1e1", "loop": 1}, "bass": null}
    },
    {
      "start": "1",
      "end": "5/4",
      "a": {"melody": {"event_id": "v1e1", "loop": 2}, "bass": null},
      "b": {"melody": null, "bass": null}
    }
  ]
}
```

`event_id` 是事件在**原小节**中的 ID（如 `v1e1`，与 `GET /measures/{id}` 返回的一致）；
`loop` 表示该发声来自第几次循环。半开区间 `[start, end)` 保证循环边界处既不丢音也不
重复计音：恰好在某次循环起点休止的声部，在该起点的段中为 `null`。

### `POST /measures/{id}/avoidance-preview` — 双声部冲突避让（预览，不写库）

对已保存小节中的**两个不同声部**做碰撞避让预览。把两个声部的每个**发声事件**
（`kind = "sound"`，**休止事件不参与**）视为**不可拆分**的整体：当两个发声事件的
半开区间具有**正长度交集**时构成冲突——仅在边界相接（一个事件的 `end` 恰好等于另一
事件的 `start`）不算冲突，交集长度必须严格大于 0。

消除冲突的唯一手段是把任意冲突事件**整体静音**（静音一个事件即同时解除它参与的全部
冲突）。服务在所有可行静音方案中，用**精确分数**求**被静音事件原时长之和最小**的一组
方案。该问题等价于以两声部发声事件为二分部顶点、事件原时长为点权、冲突关系为边的
**二分图最小权顶点覆盖**，内部用分数容量的**最小割 / 最大流**精确求解；所有时长均为
`fractions.Fraction`，无浮点误差。

- 请求体：`voice_a`、`voice_b` 为两个声部名（1～100 字符）。小节 ID 不存在返回
  **404**；任一声部名不在该小节中、或两个声部名相同返回 **422**（detail 给出原因）。
- 响应给出：`muted`（建议整体静音的事件：事件 ID、所属声部、原起止、**原时长**）、
  `total_muted`（被静音事件原时长之和，最小化目标，精确分数字符串）、`conflicts`
  （**全部原始冲突事件对**：双方事件信息与正长度重叠时长 `overlap`）。没有冲突时
  `muted` / `conflicts` 均为空、`total_muted` 为 `"0"`。
- 该接口为**只读预览，不写入数据库**，已保存小节保持不变。

```bash
curl -s -X POST http://localhost:8000/measures/1/avoidance-preview \
  -H 'Content-Type: application/json' \
  -d '{"voice_a": "lead", "voice_b": "bass"}'
```

例：小节时长 `1`，`lead` 在 `[0, 1/3)` 发声，`bass` 在 `[0, 1/4)`、`[1/4, 3/8)`
连续发声。两处冲突的正长度重叠分别为 `1/4` 与 `1/12`；静音 `lead` 的整事件花费
`1/3`，而静音 bass 的两个事件要花 `1/4 + 1/8 = 3/8`，故最优方案是整体静音
`lead` 的 `v1e1`，总静音时长 `1/3`：

```json
{
  "measure_id": 1,
  "target_duration": "1",
  "voice_a": "lead",
  "voice_b": "bass",
  "muted": [
    {"id": "v1e1", "voice": "lead", "start": "0", "end": "1/3", "duration": "1/3"}
  ],
  "total_muted": "1/3",
  "conflicts": [
    {
      "a": {"id": "v1e1", "voice": "lead", "start": "0", "end": "1/3", "duration": "1/3"},
      "b": {"id": "v2e1", "voice": "bass", "start": "0", "end": "1/4", "duration": "1/4"},
      "overlap": "1/4"
    },
    {
      "a": {"id": "v1e1", "voice": "lead", "start": "0", "end": "1/3", "duration": "1/3"},
      "b": {"id": "v2e2", "voice": "bass", "start": "1/4", "end": "3/8", "duration": "1/8"},
      "overlap": "1/12"
    }
  ]
}
```

各字段含义：

| 字段              | 说明                                                                 |
| ----------------- | -------------------------------------------------------------------- |
| `muted`           | 一组整体静音的发声事件：`id` / `voice` / `start` / `end` / `duration`（**原事件时长**，非重叠时长）；按声部存储顺序排列 |
| `total_muted`     | `muted` 中各事件原时长之和（最小化目标值，精确分数）                 |
| `conflicts`       | 全部原始冲突对：`a` / `b` 为双方发声事件（ID、声部、起止与时长），`overlap` 为正长度重叠时长；边界相接与休止均不出现在其中 |

> 当存在多组同样最优的静音方案（最小总时长相同）时，接口返回其中一组；最优总时长
> `total_muted` 唯一。返回的 `muted` 保证覆盖 `conflicts` 中的每一对冲突。

小节不存在与声部参数错误示例：

```json
{"detail": "measure not found"}
```

```json
{
  "detail": {
    "message": "voice_a and voice_b must be two different voices",
    "voice_a": "lead",
    "voice_b": "lead"
  }
}
```

```json
{
  "detail": {
    "message": "voice not found in measure",
    "missing_voices": ["drums"]
  }
}
```

### `POST /measures/{id}/time-preview` — 速度变化下的秒级时间线（预览，不写库）

把已保存小节每个事件的起止偏移，按请求给出的**速度段数组**换算成**自小节开始的秒数**。
偏移以**全音符时长为 1**；`start_bpm` / `end_bpm` 为**正十进制字符串**，表示**每分钟
四分音符数**（如 `"120"`、`"90.5"`）。每段内 BPM 随偏移**线性变化**，段与段之间允许
跳变；偏移 `x` 处 1 个偏移单位（全音符）历时 `240 / bpm(x)` 秒，事件起止的秒数即该
函数从 0 到对应偏移的**跨段积分**（线性变化段的积分为对数形式
`240·Δx·ln(b₁/b₀)/(b₁−b₀)`，恒定速度段为 `240·Δx/b`）。

- 请求体：`segments` 为**非空**数组，每段含 `end` / `start_bpm` / `end_bpm`：
  - `end` 为**正有理数字符串**（同时长格式）；各段 `end` **严格递增**，且**末段 `end`
    必须等于小节目标时长**；第一段起点为 `0`，其余各段起点为前一段的 `end`；
  - `start_bpm` / `end_bpm` 为该段起点、终点处的 BPM（正十进制字符串，拒绝 `0`、
    负数、分数、科学计数法等）。
- 同一偏移（包括跨声部共享的边界）只积分一次并**复用同一秒值**，因此共享边界的
  `start_seconds` / `end_seconds` 完全一致；秒值输出为**六位小数字符串**。
- 小节 ID 不存在返回 **404**；速度段不合法（空数组、`end` 非严格递增、末段不等于
  目标时长、BPM 或 `end` 格式错误等）返回 **422**。该接口为**只读预览，不写入数据库**。

```bash
curl -s -X POST http://localhost:8000/measures/1/time-preview \
  -H 'Content-Type: application/json' \
  -d '{
    "segments": [
      {"end": "1/2", "start_bpm": "120", "end_bpm": "120"},
      {"end": "1",   "start_bpm": "120", "end_bpm": "60"}
    ]
  }'
```

例：小节时长 `1`，前半段恒定 120 BPM（1 个偏移单位历时 2 秒），后半段从 120 线性
渐慢到 60。偏移 `1/4` → `0.5` 秒、`1/2` → `1` 秒、`3/4` → `1 + 2·ln(4/3)` ≈
`1.575364` 秒、`1` → `1 + 2·ln 2` ≈ `2.386294` 秒：

```json
{
  "measure_id": 1,
  "target_duration": "1",
  "voices": [
    {"name": "melody", "events": [
      {"id": "v1e1", "kind": "sound", "start_seconds": "0.000000", "end_seconds": "1.000000"},
      {"id": "v1e2", "kind": "rest",  "start_seconds": "1.000000", "end_seconds": "1.575364"},
      {"id": "v1e3", "kind": "sound", "start_seconds": "1.575364", "end_seconds": "2.386294"}
    ]},
    {"name": "bass", "events": [
      {"id": "v2e1", "kind": "rest",  "start_seconds": "0.000000", "end_seconds": "0.500000"},
      {"id": "v2e2", "kind": "sound", "start_seconds": "0.500000", "end_seconds": "2.386294"}
    ]}
  ]
}
```

各字段含义：

| 字段                            | 说明                                                     |
| ------------------------------- | -------------------------------------------------------- |
| `voices[].events[].id` / `kind` | 原事件 ID 与种类（`sound` / `rest`），与保存的小节一致   |
| `start_seconds` / `end_seconds` | 事件起止偏移积分得到的秒数，六位小数字符串               |

速度段不合法时的响应示例：

```json
{
  "detail": {
    "message": "invalid tempo segments; nothing was saved",
    "reason": "last segment ends at 1/2, not at the target duration 1"
  }
}
```

> 在浏览器中打开 http://localhost:8000/ 可查看服务与接口清单，
> http://localhost:8000/docs 为可直接发起请求的 Swagger UI。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # 本地启动（默认 ./app.db）
pytest                                 # 全部测试（纯逻辑 + API）
python3 -m unittest discover -s tests -p 'test_grid.py'   # 仅核心逻辑（零依赖）
```

## 项目结构

```
app/
  grid.py      # 核心逻辑：有理数解析、精确累加、边界并集切分网格、节奏等价匹配、循环对齐、等分刻度量化、双声部冲突静音（二分图最小权顶点覆盖）、速度段秒数积分（纯标准库）
  db.py        # SQLite 连接、建表、读写（纯标准库）
  schemas.py   # Pydantic 请求/响应模型与校验
  main.py      # FastAPI 应用与路由（启动时初始化数据库）
tests/         # 核心逻辑测试（stdlib 可跑）+ API 测试（TestClient）
Dockerfile
docker-compose.yml
```
