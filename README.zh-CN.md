# quick-exec-mcp — 给 Amazon Quick 一个真的 shell

[English](README.md)

Amazon Quick 自带的 agent 工具（`run_python`、`ripgrep` 之类）跑在 `quickwork-sandbox`
里：连不上 `localhost`，也看不到大部分文件系统。本地 MCP server 不一样——Quick 是把它
当普通子进程 fork 出来的，不在那个沙箱里。

所以这个 server 的命令落在真机上，以你的身份执行。同步执行、后台 job、超时杀进程树、
审计日志，外加一道很窄的拦截，只挡那几条没人会故意去跑的命令。

命令走登录 shell（`zsh -lc`），PATH 和 Terminal 一致。这点比听起来重要：装在
`~/.local/bin`、`~/.toolbox/bin` 或者版本管理器里的东西，只有 source 过 profile 才存在。

## 安装

```bash
git clone <this repo> && cd quick-exec-mcp

uv sync                                # 建 venv
./.venv/bin/python test_exec_mcp.py    # 70 项检查，走真实 MCP stdio
python install.py install              # 注册到 Quick
```

然后在 Quick 里 **Settings → Capabilities → Connectors → Local Shell (exec) →
Refresh**。第一句话让它 `call shell_info`——它会报出命令实际看到的用户、主机名和
`PATH`，这是确认「真的在沙箱外」最快的办法。

需要 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)。`install.py` 只用标准库，
任何 `python3` 都能跑。

```
python install.py status       Quick 目前注册了哪些
python install.py install      装上并启用
python install.py disable      保留条目但关掉
python install.py enable       重新打开
python install.py uninstall    删掉条目
```

它直接改当前 profile 的 `mcp_config.json`——跟 Connectors UI 写的是同一个文件——
每次写之前备份成 `mcp_config.json.bak`。重装会保留 `secret://` 引用和你手改的 env。
文件位置、schema 和最费时间的那几个坑，都记在
[docs/how-quick-loads-mcp.md](docs/how-quick-loads-mcp.md)。

## 提供的工具

| 工具 | 说明 |
|---|---|
| `shell_execute` | 同步执行并等结果。返回 `exit_code` / `stdout` / `stderr` / `duration_s` / `timed_out` |
| `shell_start` | 后台执行，返回 `job_id`。用于构建、安装、dev server |
| `shell_job_output` | 读某个后台 job 到目前为止的输出（快照，可反复读） |
| `shell_job_wait` | 阻塞等 job 结束再返回输出，比轮询省事 |
| `shell_job_kill` | 杀掉后台 job 及其整个进程树 |
| `shell_list_jobs` | 列出本次会话起过的所有 job |
| `shell_info` | 报告本 server 配置 + 实时探测 `whoami` / `hostname` / `PATH` |

`shell_execute` 支持 `cwd`（默认 `$HOME`，支持 `~`）、`timeout`（默认 120s）、
`stdin`、`env`（覆盖式合并）。

## 环境变量

写进 Quick 的 `mcp_config.json` 里那条 `exec` 的 `env` 字段。

| 变量 | 默认 | 说明 |
|---|---|---|
| `QUICK_EXEC_SHELL` | `/bin/zsh` | 用哪个 shell |
| `QUICK_EXEC_SHELL_ARGS` | `-lc` | `-l` 是登录 shell；去掉就不 source profile |
| `QUICK_EXEC_DEFAULT_CWD` | `$HOME` | 未指定 `cwd` 时的工作目录 |
| `QUICK_EXEC_DEFAULT_TIMEOUT` | `120` | 默认超时（秒） |
| `QUICK_EXEC_MAX_TIMEOUT` | `3600` | `timeout` 参数的上限 |
| `QUICK_EXEC_MAX_BYTES` | `60000` | 每条流的输出上限，约 15k token |
| `QUICK_EXEC_READER_GRACE` | `2` | 子进程退出后还继续排空管道多少秒 |
| `QUICK_EXEC_MAX_FINISHED_JOBS` | `50` | 保留多少个已结束的 job，超了丢最旧的 |
| `QUICK_EXEC_AUDIT_LOG` | `~/.quick-exec-mcp/audit.jsonl` | 审计日志路径 |
| `QUICK_EXEC_LOG_LEVEL` | `WARNING` | 调成 `INFO` 会把每次调用都打到 stderr |
| `QUICK_EXEC_ALLOW_DANGEROUS` | 未设置 | 设为 `1` 关掉危险命令拦截 |

## 审计日志

每条命令都会 append 一行 JSON 到 `~/.quick-exec-mcp/audit.jsonl`——时间、命令、cwd、
exit code、耗时——被拦下的也记。想知道 Quick 到底在你机器上跑了什么，就看这个：

```bash
tail -20 ~/.quick-exec-mcp/audit.jsonl | jq -c '[.ts, .kind, .exit_code, .command]'
```

## 危险命令拦截

默认拦下这几类（`QUICK_EXEC_ALLOW_DANGEROUS=1` 可关）：

- 对 `/`、`$HOME`、`/System`、`/Applications`、`/usr` 等系统根目录的递归删除
- `mkfs*`、`diskutil eraseDisk/reformat/partitionDisk`、`dd of=/dev/disk*`
- fork bomb
- 处于命令位置的 `shutdown` / `reboot` / `halt`
- `csrutil disable`、`spctl --master-disable`（关 SIP / Gatekeeper）

匹配时会同时用原文和去掉引号的版本去比——因为 `rm -rf "$HOME"` 这种带引号的写法，
模型写出来的概率不比不带引号的低。范围刻意收得很窄，日常操作照常放行：`rm -rf` 某个
具体目录可以，`rm -rf "$HOME/project"` 可以，`grep shutdown /etc/hosts` 也可以
（那里的 `shutdown` 是参数，不在命令位置）。

**挡的是模型手滑，不是防恶意。** 它就是正则匹配，任何间接写法（`$(echo rm) -rf /`）
都能绕过；而且能调到这个 server 的人，本来就能执行你能执行的一切。

## 行为细节

- **超时杀整棵进程树**：子进程用 `start_new_session=True` 独立进程组，超时时对整组
  先 SIGTERM 再 SIGKILL，不留孤儿。超时前的输出保留，`timed_out: true`。
- **`timeout` 是真的上限，连逃出进程组的进程也拦不住它**：任何调了 `setsid` 的进程
  （基本就是各种 daemon）会离开进程组、躲过 kill，并继续持有它继承来的 stdout 管道。
  如果去等那个管道的 EOF，这次调用就永久挂住，在 Quick 里看起来就是 connector 死了。
  所以排空管道有自己的 `QUICK_EXEC_READER_GRACE` 预算，返回里带
  `output_incomplete: true`。这种情况大约 `timeout + 5s` 返回：SIGTERM、升级到
  SIGKILL、再加宽限期。
- **`timeout: 0` 或负数表示「用默认值」**，不是「立刻放弃」。
- **截断保头也保尾**：超上限时保留前 1/3 和后 2/3，中间标注省略了多少字节。构建失败
  时有用的信息在末尾，只留开头等于把要看的东西正好丢掉。
- **stdout / stderr 分开返回**，不混流。
- **禁用交互式分页器**：注入 `PAGER=cat`、`GIT_PAGER=cat`、`TERM=dumb`、`NO_COLOR=1`，
  避免 `git log` 卡在 less 里或者塞一堆 ANSI 转义。都能用 `env` 覆盖。

## 测试

```bash
./.venv/bin/python test_exec_mcp.py
```

70 项检查，全部走**真实 MCP stdio**——`initialize`、`tools/list`、`tools/call`，
跟 Quick 发的是同一套 JSON-RPC——而不是 import 模块直接调函数。「Python 里跑得好、
过协议就炸」的问题只有这样才抓得到。覆盖：握手、tool schema、登录 shell 的 `PATH`
（跟真的 `zsh -lc` 对比）、流分离、stdin、env 覆盖、非法 UTF-8、超时后用 `pgrep`
查孤儿、彻底逃出进程组的孤儿、头尾截断、后台 job 生命周期、job 回收、审计日志。

破坏性的拦截用例——`rm -rf "$HOME"` 那一批——是**在进程内对 `_guard()` 断言的，
根本不会交给 shell**。把它们发给真实 server、然后靠一条正则去挡，等于离「删掉执行者的
home 目录」只差一次改坏正则。唯一走协议的那条把 `HOME` 指向一个临时目录，跑完再断言
那个目录还在——这样将来真出回归，毁掉的是临时目录。

## 安全边界

这个 server 给 Quick 的是**你这个用户的完整 shell 权限**——读写任何你能读写的文件、
读凭证文件、调 AWS CLI、推代码、发网络请求。绕开沙箱是它的设计目的，不是 bug。

- Quick 的 `global_default` 工具权限保持 `prompt`，让调用执行前先问你一声
- 偶尔翻一下 `~/.quick-exec-mcp/audit.jsonl`
- 不要装到你不信任的机器或 client 上
- 凭证别写进配置文件，用 Quick 的 secret store（`secret://`），重装会保留这些引用

## License

MIT，见 [LICENSE](LICENSE)。
