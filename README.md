# SplitGuard — 训练/评测拆分审计系统

在启动训练实验前，确认训练集与评测集**不共享同一来源对象**。

- **Angular** (`frontend/`)：样本分组看板、冲突与风险展示、拆分方案操作
- **FastAPI** (`backend/`)：pandas + scikit-learn 生成拆分方案，独立验证
- **PostgreSQL** (`db/schema.sql`)：样本清单、来源关系、随机种子、清单哈希

## 核心规则

1. **分组隔离（硬约束）**：同一来源主体及其全部派生件（裁切/增强/转码）只出现在一侧。
   派生样本沿 `source_relations` 边继承来源身份；声明来源与继承身份冲突会被显式报告。
   **绝不为了比例好看把同一主体拆到两边。**
2. **时间边界（软约束）**：评测样本须晚于边界、训练样本须早于边界。主体跨边界时
   整组归一侧，产生的时间违例计入代价并逐条解释。
3. **类别比例（软约束）**：以目标评测占比最小化偏差。罕见类别来源组过少导致目标
   不可达时，报告给出最优改进移动及其代价（违例数、净代价），说明为何放弃。
4. **重复内容仅作辅助证据**：`content_hash` 不参与分组；若重复内容横跨两侧，
   验证阶段作为"未知关系"警告提示人工核查。
5. **确认即锁定**：用户确认后计算并锁定 `manifest_hash`（样本归属+参数+种子的
   SHA-256）。任何修改通过 `revise` 形成新版本，旧版本哈希永久保留。

## 冻结评测集保护（v2）

评测集被多个实验引用后不再重新随机划分：

- **证据版本化**：派生关系与"两个主体是同一人"的合并都是可撤销的证据，全部记入
  仅追加的 `evidence_events`（每个数据集单调递增版本号）。撤销也是事件，历史不改写。
- **增量关系检查**：新样本入库、新关系登记、合并/撤销时自动重算——与冻结评测集
  同组的训练样本进入**隔离候选**（`quarantine_candidates`），新拆分方案自动排除。
- **实验影响标识**：已完成实验保留当时清单快照（`manifest_snapshot`），新证据造成的
  污染以 `experiment_impacts` 追加记录标识受影响范围，**不删除记录假装从未发生**。
- **按证据版本重放**：`GET /experiments/{id}/contamination?evidence_version=N`
  用同一清单 + N 版证据重算污染判断，结果确定，可审计任意历史时点。
- **隔离解除不加回旧实验**：证据被推翻后隔离自动解除（或人工解除），但旧实验清单
  不可变；如需重新使用该样本，只能生成新拆分方案。
- **保留评测集再平衡**：`splits:generate` 传 `keep_eval_from_split_id` 即复制锁定
  版本的评测侧、仅重排训练侧；比例目标无法达到时逐类说明缺口与原因。

## 快速开始

```bash
docker compose up          # PostgreSQL + API + 前端
# 或本地开发：
cd backend && pip install -r requirements.txt
uvicorn app.main:app --port 8000
python app/seed.py         # 载入演示数据并走通 生成→验证→锁定 全流程
cd frontend && npm install && npx ng serve   # http://localhost:4200
```

## 演示数据覆盖的棘手场景（`backend/app/seed.py`）

| 场景 | 样本 | 系统行为 |
|---|---|---|
| 主体跨月份 | `alice` 1月+2月样本，边界 2026-02-01 | 整组归一侧，报告 6 条时间违例及原因 |
| 罕见类别 | `spoof` 仅 2 个来源组 | 25% 目标不可达，解释最优移动需 +30 代价 |
| 派生缺来源 | `mystery-transcode-01` 无 source、无关系 | 标记孤儿，验证阶段列为泄漏风险 |
| 重复内容 | 孤儿转码与 dave 原始帧同 hash | 仅辅助证据，横跨两侧时告警 |

## API 摘要

```
POST /datasets                          创建数据集
POST /datasets/{id}/samples             批量导入样本（含 source_key / content_hash）
POST /datasets/{id}/relations           登记派生关系（crop/augment/transcode）
GET  /datasets/{id}/groups              分组看板数据（含冲突、孤儿）
POST /datasets/{id}/splits:generate     生成拆分方案（目标比例+时间边界+种子）
GET  /splits/{id}                       方案详情与清单
POST /splits/{id}/verify                独立验证（隔离/时间/比例/未知关系风险）
POST /splits/{id}/confirm               确认并锁定 manifest_hash
POST /splits/{id}/revise                修改 → 生成新版本，旧版本转为 superseded
POST /relations/{id}/revoke             推翻派生关系证据（触发增量复查）
POST /datasets/{id}/merges              确认两个主体为同一人（可撤销）
POST /merges/{id}/revoke                撤销主体合并
GET  /datasets/{id}/evidence            证据事件日志（版本化）
POST /datasets/{id}/experiments         登记已完成实验（快照当时清单）
GET  /experiments/{id}                  实验详情 + 影响历史
GET  /experiments/{id}/contamination    按证据版本重放污染判断
GET  /datasets/{id}/quarantine          隔离候选列表
POST /quarantine/{id}/lift              人工解除隔离（不加回旧实验）
```

## 独立验证（`backend/app/verify.py`）

不复用拆分器的并查集，改用独立的图遍历重新推导分组，然后检查：

- **分组隔离**：每组样本是否只落在一侧（违例即失败）；
- **时间条件**：逐样本核对边界（违例如实列出，不判失败——它是已声明的代价）；
- **类别比例**：从原始表重算实际占比与差距；
- **未知关系残留风险**：孤儿派生样本（可能暗属对侧主体）+ 横跨两侧的重复内容。

## 测试

```bash
cd backend && python -m pytest tests/ -q
```

覆盖：分组隔离与派生继承、时间边界代价、罕见类别解释、孤儿/重复内容风险、
哈希锁定与版本链、验证器抓出人为破坏的隔离违例。
