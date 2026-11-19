# AGENTS.md — 本仓库开发规则（Codex 必读）

## 环境事实
- 本机为 Windows，shell 是 **PowerShell (pwsh)**，不是 bash。
- 因此**禁止**：heredoc（`<< 'EOF'`）、`&&` 串联、bash 单引号转义、`python << EOF`。
- 历史问题：用 bash 语法写文件连续失败 3 轮，浪费大量时间。

## 写文件的唯一正确方式
1. **首选：直接使用 apply_patch 工具**增删改文件，不要经过 shell。
2. 次选：PowerShell here-string，且**一次只写一个文件**：
   ```powershell
   @'
   文件内容
   '@ | Set-Content -Path "app\models\ledger.py" -Encoding utf8
   ```
3. 严禁在 shell 命令里拼接超长多行字符串再转义引号；严禁 `'"` 混用。

## 写完必须校验
- `Get-Content <path> -TotalCount 5`
- `python -c "import app.models"`

## 边界
- 平台只做基础设施：路由、队列、持久化、超时、UI、权限。
- 不做 DAG 拆解、Schema 校验、验收判定、审批决策（属 Master 职责）。
- 台账只追加与更新状态，禁止任何删除能力。
