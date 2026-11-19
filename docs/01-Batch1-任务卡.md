# Batch 1 任务卡（首批交付）

## 交付范围

1. Python 项目骨架
   - `app/` 包结构，分层清晰：api / core / models / services / schemas
   - `pyproject.toml` 或 `requirements.txt`
   - 配置加载（环境变量 + `.env.example`）
   - 统一日志初始化
2. 数据库模型（SQLAlchemy 2.x，严格对齐台账字段）
   - `PipelineLedger`（对应 pipeline_ledger）
   - `TaskLedger`（对应 task_ledger）
   - `MessageArchive`（原始报文归档索引：trace_id、task_id、msg_type、sender、timestamp、raw_json、created_at）
   - `depends_on` 使用 JSON 列存储数组
   - 所有表只允许 INSERT / UPDATE 状态字段，不允许删除历史行（提供 repository 层约束）
3. SQLite 初始化脚本
   - `scripts/init_db.py`：建表 + 建索引
   - 索引要求：trace_id、task_id、status、(trace_id, status)
4. 报文模型（Pydantic v2）
   - `Envelope`：公共 6 字段校验
   - 8 种 msg_type 的 payload 模型最小骨架（业务字段透传，不硬编码业务语义）
   - 非法 msg_type / 缺失公共字段 → 校验失败
5. 归档写入服务
   - `append_archive(trace_id, task_id, envelope)`：写 JSONL 文件 + 写 MessageArchive 索引行
   - 归档路径规则可配置
6. README
   - 安装、初始化、启动说明
   - 目录结构说明
   - 明确写出"平台不做 DAG 拆解与校验"的边界声明

## 本批次不做

- REST 接口（Batch 3）
- 队列/路由（Batch 2）
- 超时、心跳、熔断（Batch 4）
- 前端（Batch 5）
- 测试（Batch 6，但代码需可测试）

## 验收标准

- [ ] 目录结构与交付范围一致，无多余业务模块
- [ ] 模型字段与基线台账逐字段一致（字段名、类型、可空性）
- [ ] 无任何 DAG 拆解、校验、审批判定逻辑
- [ ] 台账无 delete 能力
- [ ] `scripts/init_db.py` 可执行成功，生成库文件
- [ ] 报文 Envelope 能拒绝非法 msg_type
- [ ] README 完整可照做
