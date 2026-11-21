name: deploy-smoke
version: 1.0.0
owner: ops-win
applies_to: 运维
inputs: repo_path, target_schema
outputs: probe_report
criteria_template: 健康检查路由在位且声明 /api/health；迁移目录最高版本等于目标版本；仓库文档索引在位
reviewer_hint: 质检
summary: 部署后三秒冒烟，判断这一版是不是真的起来了（不连服务，只看仓库里的实物）
