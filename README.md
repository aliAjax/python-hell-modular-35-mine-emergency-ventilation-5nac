# 矿井应急避险与通风协调

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8335`。领域对象包括矿井人员、气体传感、通风设备、逃生通道、避险硐室、事件和处置任务。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、失败场景和演练账测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8335
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8335/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建矿井事件、人员和设备记录后，依次执行撤离、搜救、通风恢复和事件关闭。`POST /api/offline-records` 用于合并现场离线记录，`source_id + record_id` 相同会幂等返回原记录。

## 规则重点

- 活跃任务按 `dedupe_key` 防止重复派工。
- 气体读数按阈值计算`severity`。
- 事件关闭前必须没有失联或已定位人员、没有活跃任务，并且所有通风设备恢复运行。

## 演练账（drill ledger）

演练中的停风机、封通道、占避险硐室不能直接写进真实状态，否则收尾一旦漏还原就会污染现场。系统为每个演练单独立账：

- `drill` 对象状态：`planned → active → (interrupted → reconciling) → closed`，另可 `abort`。
- 演练动作通过 `POST /api/drills/<id>/ledger` 提交，只生成 `drill_entry` 账目（`projected`），真实设备状态保持演练前的样子。账目按顺序回放投影状态，因此演练里的状态机连推（如 degrade → restore → stop）照常校验。
- 占用避险硐室时按账内驻留人数累加，超出硐室 `capacity`（核定容量）直接拒绝。
- 演练 `active` 期间，账内涉及的风机、通道、硐室拒绝真实动作（409，提示先移交）；演练未涉及的设备不受影响。
- 演练中途真出了事：对演练执行 `handover`（须指定一个未关闭的真实 `incident`），共享设备交给真实事件处置。此后真实动作一旦使设备状态偏离演练最新投影，冲突的演练账目立即作废（`voided`）；与现实一致的投影保留。
- `reopen` 按真实状态重算：每个设备只核对其最新存活投影，对不上的条目标记为 `mismatch` 并在 `GET /api/drills/<id>/ledger` 的 `discrepancies` 中列出，必须逐条 `confirm`（填写核实说明）后才能结演练；被后续动作取代的历史条目不参与重算。
- `complete` 结清演练账：存活投影置为 `settled`，`mismatch` 未确认时拒绝结清。`abort` 则把未结条目全部作废。
- 演练账存在 `projected` 或 `mismatch` 条目期间，真实事件不能 `close`。

演练相关接口：

- `POST /api/drills`：创建演练（`name`、`area_code`、ISO-8601 `planned_at`）。
- `POST /api/entities/<drill_id>/actions`：`start` / `handover`（带 `incident_id`）/ `reopen` / `complete`（带 `summary`）/ `abort`。
- `POST /api/drills/<id>/ledger`：演练记账，请求体 `{"action":"stop|block|occupy|...","target_id":"<设备id>","data":{...}}`。
- `GET /api/drills/<id>/ledger`：账目、差异清单和 `settled` 标志。
- 账目条目的 `confirm`/`settle`/`void` 走通用 `POST /api/entities/<entry_id>/actions`；账目条目不能通过通用创建接口手工新建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
