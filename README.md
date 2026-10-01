# 种质资源入库与活力复检服务

本项目是面向种质资源库的 Python 后端服务，用于登记采集或引进材料、建立种子批次、管理低温库位和容器移动、执行发芽活力检测、生成复检日程并处理环境与质量告警。档案、库存、检测和发放审批都保存在本地 SQLite 中，关键写入带版本或幂等键，适合在单个 Linux 应用容器内运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `data/germplasm.db`，也可以通过 `GERMPLASM_DATABASE_PATH` 指向其他 `.db`、`.sqlite` 或 `.sqlite3` 文件。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查为 `GET /api/system/health`。首次使用可调用 `POST /api/auth/bootstrap` 创建管理员，再通过 `POST /api/auth/login` 取得 Bearer 会话令牌。种质业务接口统一位于 `/api/germplasm`。

## 测试与构建检查

```bash
python -m pytest
python -m compileall -q app tests
```

下面两条命令分别检查 HTTP 入口和完整的入库演示链路：

```bash
python -m app.cli smoke
python -m app.cli demo
```

## 业务边界

- `app/germplasm/accessions.py` 管理来源、资源档案、护照信息与接收状态。
- `app/germplasm/inventory.py` 管理批次、库位容量、容器摆放、移动、领用和冻结。
- `app/germplasm/viability.py` 管理检测规程、取样、重复计数、活力结果与复检日程。
- `app/germplasm/quality.py` 管理温湿度读数、偏离告警和种质发放审批。
- `app/germplasm/reservations.py` 管理有期限的库存预约：批准即锁定、到期/取消/质量恶化释放、拣货与整单回退、余额与分配溯源。
- `app/api`、`app/services` 和 `app/repositories` 提供身份、权限、审计、后台作业及维护能力。

## 库存预约

审批通过时不再只记录一个批次编号，而是在同一立即事务内按 **资源限制 → 质量冻结 → 最近有效活力 → 先到期先用（FEFO）** 的顺序选择一个或多个批次，按 `sequence_no` 逐批锁定重量（可跨批拆分），并在每条预约行的 `rule_json` 中快照当时的活力结果、实物/已锁定重量与命中的规则，作为出库分配顺序的证据。

- 并发审批由 `BEGIN IMMEDIATE` 串行化，配合 `reservation_lines` 上的超卖触发器双重兜底，保证不超卖；审批重放返回既有预约，不重复占用。
- 预约默认 7 天有效（审批时可在 1–90 天内指定 `reservation_days`）。到期、申请取消或批次被质量冻结时释放余额并写入 `reservation_events`；质量释放后自动按同一规则从其他批次补足缺口，补不齐则整单退回待审批。
- 已进入拣货的预约不再被到期/取消/质量冻结抢占，只能由具备 `distribution.manage` 权限的人员接管（`/takeover`）或整单回退（`/rollback`，自动把已拣重量归还库存）；普通拣货员需要 `distribution.pick`。
- 服务启动时自动回收过期预约；也可通过 `POST /api/germplasm/reservations/reclaim-expired` 或 `python -m app.cli reclaim-expired` 手动回收。
- `GET /api/germplasm/lots/{id}` 与 `GET /api/germplasm/distributions/{id}` 均展示实物、已预约、可再预约、已出库数量；`GET /api/germplasm/lots/{id}/reservations` 返回该批次面向各申请的分配顺序、规则快照、拣货流水与最终运单/收货方，出库人员可据此证明每份材料的去向。

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时写事务。资源档案、库位、容器摆放、检测任务和发放申请采用版本号防止旧请求覆盖新状态；入库、移库、取样和传感读数使用业务键去重。活力检测保留采用的规程版本和每个重复的观察计数，完成后可依据作物及风险策略生成下一次复检日期。会话令牌只保存摘要，审计记录不保存明文密码或令牌。
