# Codex 自定义分组：按聊天活动持续上移

这是可选的Windows Codex desktop专用适配器。安装技能不会运行它；用户明确授权保留指定现有分组、持续按聊天活动上移后，Agent先核实本机当前能力并填写私有配置，再显式启动。模板、测试均为合成数据，不包含真实设备或会话映射。

## 何时使用与代价

先检查当前版本是否原生支持自定义组内的活动排序；支持时优先原生设置。一个实测版本中，自定义组按保存的itemKeys显示，普通最近更新排序未覆盖组内。此时UserPromptSubmit hook的存在/信任状态也不证明桌面真实投递；没有实证不得声称已触发。

此适配器通过原生`thread/list`读`recencyAt`，每3秒检查活动变化，经原版已安装的`codex-app-tools` MCP执行同组移动并原生读回。`updatedAt`会受输出和元数据维护影响，不能替代用户活动时间。空闲期间只有Python与metadata app-server常驻；Node侧栏工具仅在变更批次或默认每60秒的健康检查时启动，结束后退出。不调用模型、不发消息，不产生循环推理token。

Windows的“隐藏窗口”启动仍可能带来控制台宿主。2.3.1启动器优先配置python.exe同目录的pythonw.exe；CLI与Node子进程采用不继承控制台的DETACHED_PROCESS，stdio仍由重定向管道提供，见[微软进程创建标志](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)。当前实测省掉两个conhost，空闲两进程；Node调用时短暂增加一个进程。若同目录没有pythonw，保留原解释器，实际数量重新测量，不安装或猜其他运行时。

这是当前实测接口下兼顾资源与及时性的方案，不能承诺绝对最小内存。Node短暂启动增加单次启动开销；当前原生CLI元数据进程仍有内存成本。受支持且真正投递的活动事件，或原生组内排序，才有进一步减少轮询的条件。不要通过读写内部数据库、修改应用包或自造管道协议省资源。

## 接入前核实

1. 当前设备、桌面/CLI形态、账号范围和后端版本；本适配器只处理本地Codex，不覆盖ChatGPT/远程会话。
2. 实际执行器提供`CODEX_THREAD_ID`、`CODEX_CLI_PATH`、`CODEX_VERSION`、`CODEX_MCP_NODE_PATH`、`CODEX_APP_TOOLS_PIPE_PATH`。缺少时停止，不复制其他设备的值或伪造调用身份。
3. 当前CLI的`thread/list`支持`useStateDbOnly`、`recency_at`和`recencyAt`，能分页完整返回目标组的本机元数据；当前安装工具提供`list_threads`和`move_thread_to_sidebar_section`。
4. `move_thread_to_sidebar_section`省略`beforeThreadId`确实移到同组首位，且工具读回反映当前侧栏；独立backend保存位置不等于当前侧栏立即刷新。
5. 用户已明确持续授权具体已有组内排序、暂停方法及设备范围，保持人工归属。仅位置调整允许当前活跃会话；普通分类/改名仍执行原有并发保护。

App Server一般协议见[OpenAI官方源码说明](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)。本适配器使用的活动字段和桌面工具接入以当前版本实际schema/读回为证据，不把单机实验接口称为永久公开兼容保证。版本变化停止并重新核实。

## 配置与运行

将[合成配置](../examples/codex-recency-config.json)复制到仓库外的本机维护目录，替换全部占位值。`native_section_id`与侧栏`sidebar_section_id`是两个不同身份，分别从真实原生接口获取，不能互相猜测。组数和名称由实际授权决定。记录授权、版本证据和初始原生组/成员顺序快照，真实记录不提交Git。

必需依赖：Python 3.8+标准库、当前已安装Codex CLI、随真实执行器提供的Node及已安装原版codex-app-tools。本脚本不下载或安装它们。Windows真实验收版本见[验证记录](validation.md)。

默认只校验配置，不调用应用或启动worker：

```powershell
python scripts/codex_recency/watcher.py --config C:/example/private-device-config.json
# 返回 NOT_EXECUTED；声明配置不能证明真实用户授权。
```

核实能力与用户授权后，在具有真实接入环境的当前Codex执行器中启动：

```powershell
./scripts/codex_recency/start.ps1 -ConfigPath C:/example/private-device-config.json
./scripts/codex_recency/stop.ps1 -ConfigPath C:/example/private-device-config.json
```

启动器显示实际PID并复用仍在线的同一worker。OS文件锁防止本状态目录的第二写入者；不约束其他客户端。并发身份、成员或相对顺序漂移会停止，原生接口未提供跨客户端事务，不能宣称原子保障。

启动时仅协调每组最近的本机会话；以后只处理活动时间增长。同批从旧到新上移，最终最新在前。手动把旧记录加入组不算新活动；已在首位为no-op。每次写前复读原组，写后验证首位、成员集合和其他成员相对顺序，不分类、不改名、不变更项目/目录/运行状态。

## 停止、恢复与记录

`stop.ps1`写停止标记，worker完成当前提交的原生调用后退出。暂停不撤销已完成位置变化；恢复必须明确授权，核对状态后仅移除该停止标记并重新启动。权限拒绝、组身份变化或读回失败暂停，不自动切换接口、重复覆盖或撤销后来人工操作。

`state_dir`存watcher.json心跳、last-recency.json元数据检查点、moves.jsonl成功收据及stdout/stderr日志。只保留ID、活动时间和位置结果，不保留标题、路径或转录。日志仍是私有元数据，放仓库外。重启沿检查点继续，首次启用不是反复静态重排。

不自动注册服务、开机项或新定时任务。重开桌面后接入管道可能改变；按已授权的现有Agent入口恢复并使用新执行器环境。完整桌面关闭/重开未实测时明确保留该限制。

可选统一进度：配置`progress`对象，指定已安装模块的`module_dir`、已有服务`state_dir`、`port`、独立`task_id`与`title`。它是可选外部依赖；默认脚本仅标准库，本包不复制其他Skill代码、不启动新的看板服务。业务循环更新实际移动次数，空闲进度心跳约15秒。
