# AI Agent Harness 任务分组 · 2.1.0

可复用的中文技能包：识别当前宿主与设备，先提分类方案并取得确认，再通过该应用真实支持的原生方式归组；不支持时只生成建议。

- Codex、WorkBuddy 或其他 Harness 分别核验，互不套用接口/数据库
- 首次确认组名、组数与规则；可选一次授权范围明确的后续自动维护
- 可选自动标题：保护手动命名/锁定，真正支持事件时随使用触发，不需每周单独安排
- 保护手动标题、现有组、活跃会话和历史；执行前后核对并准备回退
- WorkBuddy 故障诊断独立处理，不自动 selfheal

## 使用
将完整 `group-agent-tasks` 文件夹按**目标应用当前官方方法**加载。本交付没有安装技能、修改任何真实应用或启动 Codex 任务；没有统一安装路径，也不保证所有宿主都支持技能加载。

可直接要求：“先识别当前应用和能力，再给我的任务提出分组方案，等我确认后再执行。”

离线示例（Python 3.8+，仅标准库，在包目录运行）：

```sh
python scripts/plan_preview.py examples/inventory.json examples/proposal.json
python scripts/upkeep_guard.py examples/upkeep.json --html > upkeep-review.html
python -m unittest discover -s tests -v
```

脚本输出到标准输出，不访问网络、不读取应用目录、不修改输入、不执行归组或标题写入；本包**不包含通用真实应用执行器或后台 daemon**。实际执行由加载技能的 Agent 使用当前已核实的原生工具/UI完成。

核心说明在 `SKILL.md`；按需参考 `references/`。`examples/` 全部为虚构数据，`tests/` 为离线测试。来源差异见迁移说明；真实宿主操作尚未测试，不能把离线通过当成跨产品兼容认证。
