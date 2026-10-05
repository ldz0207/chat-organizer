# 设计参考与许可边界

查证日期：2026-10-05。以下用于吸收思路；不是对所有版本、设备或安装权限的兼容认证。本包说明、代码与合成测试独立编写，没有复制以下社区仓库源码或原文；本次核查未发现可据以复用其代码的许可证，不推定默认获准复制。

## 官方能力依据
- [OpenAI：Build plugins](https://developers.openai.com/plugins/build/plugins)：插件可封装技能与 hooks；安装、信任、实际部署和调用分别核验。它不证明任意桌面任务都有分组/改名 API
- [OpenAI：Hooks](https://learn.chatgpt.com/docs/hooks)：存在官方事件与命令 hook 机制。本包只借鉴“事件唤起轻量评估”的形式；使用时重新核对当前适用产品、官方事件名、payload、执行时机和权限。不在包中预注册 hook，不将其他产品 hooks 等同 WorkBuddy 能力

## 公开项目的有益思路
- [znbsf/codex-task-title-organizer](https://github.com/znbsf/codex-task-title-organizer)：自动标题/会话整理与轻量前台事件提示；借鉴去重、少量计数和不持久化整段 prompt 的设计，不采用其固定检查次数或容量逐出作为通用规则
- [prayer168/chatgpt-conversation-organizer：Codex tasks](https://github.com/prayer168/chatgpt-conversation-organizer/blob/main/references/codex-tasks.md)：借鉴保留既有分区/置顶、活动对象暂缓、写前复读；不能用社区技能说明替代官方可写接口证据
- [peakwheat-pixel/codex_conversation_organizer：Recovery](https://github.com/peakwheat-pixel/codex_conversation_organizer/blob/master/docs/RECOVERY.md)：借鉴外部改名使旧提案失效、撤销前独立核验与能力变化后失效缓存

## 针对本包取舍
保留用户首轮分类决定，分类越少越好；有界持续策略内轻量维护，不再要求用户每周专门安排。手动标题/归属优先；无能力、歧义或并发风险时退回建议。没有引入云端同步服务、遥测、额外访问权限、后台进程或产品私有数据库写入。
