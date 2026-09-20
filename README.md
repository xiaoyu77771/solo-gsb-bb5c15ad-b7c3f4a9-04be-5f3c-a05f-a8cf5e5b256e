# JSON Schema 反例最小化 API

基于 **Python + FastAPI** 的确定性反例最小化（counterexample minimization）服务。

给定一份 JSON Schema（Draft 7）和一个**未通过**校验的 JSON 实例，服务会：

1. 用 schema 校验原始实例；**若实例本身有效，直接返回 `422`**。
2. 锁定**首个校验错误的关键字**（jsonschema `validator` 名，如 `required` / `type` /
   `minItems` …；错误按 JSON path、关键字、信息排序，保证选择确定）。
3. 以**确定性 delta debugging**反复执行三类缩减：
   - 删除对象字段（字段名按字典序、前序 DFS）；
   - 删除数组块（经典 ddmin：分块删除 → 只保留单块 → 粒度倍增）；
   - 简化标量（字符串先试空串再按字符 ddmin；数字先归零再反复减半并保持
     int/float 种类；布尔仅单向 `true → false`，防止振荡）。
4. **只接受仍触发同一关键字的候选**（错误列表中任意一条的关键字等于锁定值即可，
   防止缩减后漂移成另一种错误）。
5. 三阶段循环直到固定点，或评估预算耗尽。
6. 返回最小实例、完整评估轨迹、实际/最大评估次数以及是否因预算提前停止。

## 单条启动命令

```bash
docker compose up --build -d
```

默认访问地址与端口：**http://localhost:8000**

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：`GET http://localhost:8000/health`
- 服务信息：`GET http://localhost:8000/info`

> 停止：`docker compose down`；查看日志：`docker compose logs -f`

## 必要配置

无需任何必填配置即可启动。以下环境变量均有默认值，可在启动前通过 shell
环境或 `.env` 文件覆盖（compose 已透传）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_PORT` | `8000` | 宿主机映射端口（容器内固定监听 8000） |
| `DEFAULT_MAX_EVALUATIONS` | `1000` | 请求未传 `max_evaluations` 时使用的默认评估预算 |
| `MAX_BUDGET_LIMIT` | `100000` | 单条请求允许的最大预算，超出返回 `400`（防滥用） |

例：改用 9000 端口、把预算上限提到 100 万：

```bash
HOST_PORT=9000 MAX_BUDGET_LIMIT=1000000 docker compose up --build -d
```

## API

### `POST /minimize`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema` | object \| boolean | 是 | JSON Schema（Draft 7），允许布尔 schema |
| `instance` | 任意 JSON | 是 | 未通过校验的实例（反例） |
| `max_evaluations` | integer ≥ 1 | 否 | 最大校验评估次数（含初始校验），默认 1000，硬上限 100000 |

响应 `200`：

| 字段 | 说明 |
| --- | --- |
| `keyword` | 锁定的首个校验错误关键字（布尔 schema `false` 时为 `null`） |
| `original_instance` | 原始反例（原样回显） |
| `minimal_instance` | 找到的最小实例（在预算与确定性缩减算子意义下） |
| `evaluations` | 实际执行的校验次数 |
| `max_evaluations` | 本次使用的预算 |
| `stopped_early` | `true` 表示因预算耗尽提前停止（否则表示到达固定点） |
| `trace` | 评估轨迹：`evaluation`（序号）、`action`（缩减动作/路径）、`accepted`（候选是否仍触发关键字而被接受）、`keyword`（候选首个错误关键字）、`message` |

错误：

- `422 Unprocessable Entity` —— **原始实例通过了校验**（不是反例）；或请求体不符合接口定义。
- `400 Bad Request` —— schema 自身非法，或 `max_evaluations` 超过服务端上限。

### 示例

```bash
curl -s -X POST http://localhost:8000/minimize \
  -H 'Content-Type: application/json' \
  -d '{
    "schema": {"type": "object", "required": ["a"]},
    "instance": {"b": 1, "noise": [1, 2, 3]},
    "max_evaluations": 500
  }'
```

```json
{
  "keyword": "required",
  "original_instance": {"b": 1, "noise": [1, 2, 3]},
  "minimal_instance": {},
  "evaluations": 3,
  "max_evaluations": 500,
  "stopped_early": false,
  "trace": [
    {"evaluation": 1, "action": "initial validation (lock keyword)", "accepted": true,  "keyword": "required", "message": "'a' is a required property"},
    {"evaluation": 2, "action": "delete field $.b",                   "accepted": true,  "keyword": "required", "message": "'a' is a required property"},
    {"evaluation": 3, "action": "delete field $.noise",               "accepted": true,  "keyword": "required", "message": "'a' is a required property"}
  ]
}
```

原实例有效时：

```bash
curl -i -X POST http://localhost:8000/minimize \
  -H 'Content-Type: application/json' \
  -d '{"schema": {"type": "integer"}, "instance": 42}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":"原始实例通过了 schema 校验, 无需最小化"}
```

## 确定性说明

- 错误排序键：`(JSON path, 关键字, 信息)`，首个错误的关键字被锁定。
- 对象字段遍历按 key 字典序；数组块从左到右；数字按固定候选序列；
  不依赖集合迭代顺序、字典插入顺序或任何随机源。同一请求重复调用，
  `minimal_instance` 与 `trace` 完全一致。

## 本地开发（不使用 Docker）

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --port 8000
python3 tests/test_minimizer.py   # 核心算法测试（无需 pytest）
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI 路由、请求/响应模型、错误码
│   └── minimizer.py     # 关键字锁定 + 确定性 delta debugging 核心
├── tests/
│   └── test_minimizer.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```
