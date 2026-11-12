# mcp-abaqus

本地 Abaqus 作业管理的 MCP server（stdio）。让任意 MCP 客户端（Hermes / Claude Desktop / Codex）
在不打开 CAE 的前提下：校验 deck、提交作业、盯 .sta/.msg/.dat、停作业、读 ODB 场输出与历史输出、
跑参数扫描队列、断点续跑、出云图 PNG、跨作业对比。

实测环境：Windows 10 + Abaqus 2018（3DEXPERIENCE R2018x）+ Intel Fortran 2018 + VS2015。

## 工具（24 个）

| 工具 | 作用 |
|---|---|
| `abaqus_environment` | 查启动器/环境脚本/允许目录/默认 CPU；启动失败先跑这个 |
| `datacheck` | 同步跑 `datacheck`，返回 verdict + .dat/.msg ERROR 块（10s–3min） |
| `submit_job` | 分离进程提交 `abaqus job=… interactive`，立刻返回 pid + workdir |
| `job_status` | 状态（running/completed/aborted/error）、最后增量、总步时间、错误消息原文 |
| `tail_output` | 尾随 .sta/.msg/.dat/.log/console，支持正则过滤 |
| `list_jobs` | 列目录下所有作业及其状态 |
| `stop_job` | `abaqus terminate`，不走再 taskkill /T /F，清掉残留 .lck |
| `odb_summary` | 步骤/帧/字段清单、实例节点单元数、集合名、history region |
| `odb_field_extremes` | 某帧某分量的 min/max + 所在节点/单元号 |
| `odb_node_series` | 指定节点的场量随帧演化（裂尖氢浓度 CH、U2 位移这类曲线） |
| `odb_history` | 读 history output（能量、支反力等） |
| `dat_tables` | 从 .dat/.msg 里抓数值表：列标题、行列范围、逐列 min/max/last，可导 CSV |
| `submit_batch` | 参数扫描：模板里 `{{占位符}}` 渲染多份 deck，按 `max_parallel` 排队跑 |
| `batch_status` | 扫描进度（顺带把空出的槽位补上新算例） |
| `batch_warnings` | 只读分页查询完整渲染告警，可按算例筛选，不启动排队作业 |
| `list_batches` / `stop_batch` | 列批次 / 整批取消 |
| `restart_job` | `recover` 从 .res 续跑，或 `oldjob` 用 `*RESTART, READ` 起新作业 |
| `odb_contour` | 用 `abaqus cae noGUI` 渲染云图 PNG（含图例/步骤信息） |
| `compare_jobs` | 多作业并排表：状态、增量、总时间、错误数、可选某字段 min/max |
| `odb_xy` | **XY 曲线图**：任意 x/y 组合（时间/帧/节点分量/场最大最小/支反力正分量和/应变比 2ln(a0/a)）+ 参考竖线，matplotlib 出图（轴标题/DPI/多曲线） |
| `model_builder` | **按论文/图纸建可算 deck**：轴对称为主（光棒 / 缺口棒），Python 生成结构化 CAX4RT 网格，物理段（材料/VUMAT 常数/BC/step/输出）从模板 deck 的 `*End Assembly` 之后原样拼接 |
| `part_builder` | **按图纸建部件**：把图纸尺寸写成 spec → 画草图（矩形/圆/多段线 + 孔 + 圆角）→ 拉伸/旋转 → 划网格 → 出 .inp + 网格预览 PNG |
| `cae_model_builder` | **建可编辑 CAE 模型**：真 CAE 几何内核（草图→特征→CAX4RT 网格）→ 出 `.cae`（File→Open 打开和手绘一样，草图可右键 Edit）+ 完整 `.inp`（材料卡从模板 deck 的 `*Material` 块原样拼）。geometry 用参数（r_gauge/r_grip/l_gauge/l_trans/l_end）或任意闭合轮廓 points；loading 可改步长/位移/质量缩放/化学势边界。与 model_builder 的分工：要 GUI 可编辑用这个，纯算用 model_builder |

所有工具返回一个 JSON 字符串；失败是 `{"ok": false, "error": "..."}`，不会抛异常打断客户端。

## 批次身份、状态与审计

- `cases[].name` 在同一批次内忽略大小写后必须唯一；拒绝尾随点和 Windows 保留名称。整批名称、缺失参数和嵌套占位符在写入算例之前检查，避免后续算例覆盖先前输入。
- `name` 是算例身份字段，不是模板参数；模板请使用 `{{E}}` 等独立参数名。
- 显式 `batch_id` 或对应输出目录已存在时拒绝覆盖。重新计算请使用新 ID；省略时生成带随机后缀的 ID，同秒提交也可区分。
- `batch_status` 会推进队列；只查看渲染告警请使用 `batch_warnings`。队列仍需客户端持续调用，当前不提供独立后台调度器。
- 已提交且进程已退出、但缺少完成记录或产物的算例报告 `unknown`，计入 `stalled`，不会退回 `not_started` 造成无限等待。`done=true` 表示没有仍在运行或排队的算例，不代表全部计算成功；应同时检查 `summary`、`stalled` 和审计字段。
- 已启动后取消的算例仍检查 `.dat`，保留 `datalint`；缺失时计入 `datalint_unchecked`。未启动即取消的算例通过 `started=false`、`audit_required=false` 区分。停止失败、进程或锁仍在时保持 `running`，不提前报告结束。
- `dat_present=false` 表示未能读取 `.dat`，不能把空 `datalint` 当作检查通过；`datalint_truncated` 表示静默丢行检查结果被截断。

`submit_batch` 和 `batch_status` 都返回渲染告警摘要：

| 字段 | 含义 |
|---|---|
| `render_warnings` | 最多10组去重告警，优先展示未使用参数和多行参数；每组算例名预览有数量限制 |
| `render_warnings_total` | 完整去重告警组数 |
| `render_warnings_truncated` | 是否有告警组未包含在摘要内 |

完整明细使用 `batch_warnings(batch_id, offset=0, limit=50, case_name="")`。`limit` 为1—100，`case_name` 按完整名称精确匹配。返回 `warnings`、`total` 和 `next_offset`；按 `next_offset` 翻页，直到它为 `null`。这里的 `total` 是筛选后的**原始逐算例告警条数**，不同于摘要的去重组数，所有算例名均保留。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ABAQUS_CMD` | `D:\ABAQUS2018\Commands\abaqus.bat` | 启动器，找不到时回退到 PATH |
| `ABAQUS_ENV_BAT` | `…\scratch\abaqus_env.bat` | ifort+VS 环境脚本；设空字符串可关闭 |
| `ABAQUS_MCP_ROOTS` | cwd、`%USERPROFILE%`、`D:\files` | 允许访问的目录（`;` 分隔），所有路径都过这个沙箱 |
| `ABAQUS_MCP_WORKDIR` | `<第一个 root>/abq_jobs` | 默认工作区；相对路径以它为基准 |
| `ABAQUS_MCP_CPUS` | 4 | 默认 CPU 数 |
| `ABAQUS_MCP_ODB_TIMEOUT` | 900 | 单次 ODB 读取超时（秒） |
| `ABAQUS_MCP_CHECK_TIMEOUT` | 600 | datacheck 超时（秒） |

## 接入 Hermes

```bash
hermes mcp add abaqus \
  --command "C:/Users/Alex/AppData/Local/hermes/profiles/research-assistant/workspace/mcp-abaqus/.venv/Scripts/python.exe" \
  --args -m mcp_abaqus
```

本机已配置好，工具名形如 `mcp__abaqus__submit_job`。改配置后 `hermes mcp list` / `hermes mcp test abaqus` 验证。

## 接入 Codex

`~/.codex/config.toml` 追加（本机已配好，2026-11-11）：

```toml
[mcp_servers.abaqus]
command = 'C:\Users\Alex\AppData\Local\hermes\profiles\research-assistant\workspace\mcp-abaqus\.venv\Scripts\python.exe'
args = ['-m', 'mcp_abaqus']
startup_timeout_sec = 120

[mcp_servers.abaqus.env]
ABAQUS_MCP_ROOTS = 'C:\Users\Alex\AppData\Local\hermes\profiles\research-assistant\workspace;D:\files;C:\Users\Alex'
ABAQUS_MCP_WORKDIR = 'C:\Users\Alex\AppData\Local\hermes\profiles\research-assistant\workspace\mcp-abaqus\abq_jobs'
ABAQUS_ENV_BAT = 'C:\Users\Alex\AppData\Local\hermes\profiles\research-assistant\workspace\mcp-abaqus\abaqus_env.bat'
```

重启 Codex / 开新会话生效。`ABAQUS_ENV_BAT` 指向项目内的持久副本（scratch 里那份 24h 会被清理）。
换机器时：`uv sync` 重建 .venv，把三个路径改成新机器路径，Abaqus/ifort/VS 装好即可。

## 手动起停

```bash
cd <本项目>
uv sync                     # 首次/改依赖后
.venv/Scripts/python -m mcp_abaqus        # 直接跑（stdio，Ctrl+C 退出）
```

MCP inspector 调试：`npx @modelcontextprotocol/inspector .venv/Scripts/python.exe -m mcp_abaqus`

## 测试

批次修复的无求解器验证：

```powershell
.\.venv\Scripts\python.exe tests\batch_contracts.py
.\.venv\Scripts\python.exe tests\batch_stdio.py
```

前者覆盖名称和批次冲突、取消、产物缺失及告警分页；后者通过真实MCP stdio验证接口返回，不启动Abaqus。完整分层与命令见 [tests/README.md](tests/README.md)。

```bash
.venv/Scripts/python tests/e2e_stdio.py            # 11 个工具的真实产物回归（快）
.venv/Scripts/python tests/e2e_stdio.py --with-odb # 再加 ODB 四件套（每次 abaqus python ~20s）
.venv/Scripts/python tests/e2e_run.py              # 真跑：datacheck → submit → 轮询 → stop
.venv/Scripts/python tests/e2e_batch.py            # 真跑：参数扫描排队 + 中途停掉再 recover
.venv/Scripts/python tests/post_reload_smoke.py    # 真跑：3个微型算例 + datacheck + 告警接口
```

`e2e_run.py` 会把 `D:\files\workProject\Explicit algorithm\WithH\Lin2024_reproduction` 的 deck 复制到
`abq_jobs/run_test/` 再跑，不动原目录。
`post_reload_smoke.py` 每次建立唯一目录并保留产物，不自动删除历史测试记录；临时移除的 `.dat` 在检查后恢复。

## 设计要点（踩过的坑）

- **stdio 是协议通道**：任何 `print()` 都会破坏 JSON-RPC，日志只走 stderr。
- **Abaqus 自带 Python 是 2.7**：`odb_extract.py` 里不能用 f-string、`pathlib`、关键字参数；
  `json.dumps` 在 2.7 返回 `str`（不是 unicode），`io.open(..., encoding=)` 会报
  `TypeError: must be unicode, not str`；`openOdb()` 只吃 `str`，json 读出来的 unicode 必须先 encode。
- **ODB 字段名有歧义**：本项目的 SDV 在 ODB 里是 33 个独立字段 `SDV1…SDV33`，不是单个 `SDV`。
  `odb_node_series(field="SDV19")` 直接可用；若别的 ODB 只有一个 `SDV` 场，传 `component="SDV19"` 也行（自动回退）。
- **.sta 两种格式**：Standard 是 9 列增量表，Explicit 是「增量号 总时间 步时间 CPU时间 dt」；
  按表头 `Abaqus/Standard|Explicit` 判别，且表头可能在第 4 行（前面还有 double precision 说明行）。
- **提交要分离进程**：`DETACHED_PROCESS` 让作业在 MCP server 重启/退出后继续跑；进度靠 .sta + .lck + pid 三处交叉判断。
- **Abaqus 失败也返回 0**：`Abaqus Error: ...` / `exited with errors` / `Problem during compilation` 都发生在
  returncode=0 的时候。所以 datacheck 的 verdict 与 job_status 的 state 是按「.dat 是否生成 + .dat/.msg 的
  ERROR 数 + console 里的失败关键字」判的，不看退出码。
- **工具链环境要提前取**：把 `call abaqus_env.bat` 拼进 `cmd /c "... & ..."` 会被 Windows 引号规则吃掉
  （表现为 `'ifort' 不是内部或外部命令`，编译静默失败）。正确做法是 `cmd /c call bat & set` 抓一次环境，
  之后所有子进程都用 `env=` 传下去。
- **本模型必须双精度**：`check_diffusion` 这类含 VUMATHT 的显式扩散算例在单精度下会
  `Zero or negative capacitance` → `package.exe aborted with system error`。提交时带
  `options={"double": "both"}`。
- **路径沙箱**：所有输入路径 resolve 后必须在 `ABAQUS_MCP_ROOTS` 内；作业名限 `[A-Za-z0-9._-]`；
  `options` 走白名单键，值禁 shell 元字符；`extra_cli` 只接受 `-` 开头且无元字符的 token。
- **`abaqus cae` 只认 `noGUI=<script>` 这一个 token**：`noGUI script.py` 会被拒绝，追加的 argv 也传不进脚本。
  出图脚本改成「生成一个 runner.py，里面用全局变量写好参数路径，再 execfile 主脚本」。
- **noGUI 里显示 ODB 必须先 `import visualization`**，否则 `displayedObject` 只接受 Part/Assembly，
  报 `found Odb, expecting StubType, Part, ...`。
- **`setPrimaryVariable` 的 outputPosition 要常量不给字符串**（ODB 的 `fo.location` 是 `'INTEGRATION_POINT'`
  这种字符串），且不变量（Mises）要用 `refinement=(INVARIANT,'Mises')`：S 场的 componentLabels 里
  通常没有 MISES，得先按不变量名映射。
- **参数扫描没有守护进程**：队列是 `<workdir>/.mcp-batches/<id>.json`，提交及 `batch_status` 调用时「对账」——
  数一遍真正在跑的（pid + .lck），把 pending 补进空槽。server 重启不丢队列。
- **XY 曲线不要走 CAE 的 XYPlot**：2018 的 `chart.setValues(xAxisTitle=…)`、`axis.setValues(title=…)`、
  `axis.axisTitle=…` 六种写法全被拒（`keyword error` / `readonly Attribute`），出图没有轴标题。
  改成「`abaqus python` 取点 → 宿主进程 matplotlib 画」（`odb_extract.py` 的 `mode_xy` + `xy.py`），
  轴标题、DPI、多曲线、竖参考线都可控。
- **`model_builder` 靠「集合名」拼接**：模板 deck 的 BC 引用 `Bottom/Top/Left/right/All`，生成的网格必须
  按同名重建（底端/顶端/对称轴/外表面/全部），否则拼出来的 deck 一算就报「set not found」。
- **模板只借物理段**：`*End Assembly` 之前（几何/网格）全部丢弃换新；之后（材料、BC、step、输出、
  amplitude）一字不改，这样已验证的 H-CGM 设置不会被重新发明一遍。
- **magic notch 用抛物线近似**：论文 M 型是 Tu 的 magic notch（a/R 恒定）；本项目用
  `r(z)=a+z²/(2R)`（根部曲率 1/R，|z|=√(2R(r_out−a)) 处收到棒身），Bridgman 三轴度
  0.33+ln(1+a/2R)=1.026，与论文 1.05 一致；要精确 profile 需 Tu 原文的曲线。
- **`.dat` 数值表没有 schema**：按「连续若干行全是数字」切块，取上方最近的非数字行当标题；
  CGM 载荷-位移、SDV 打印、能量汇总都能这么抓，但不要假设列含义，先看 `stats` 再定列。
- **建模脚本的四个坑**：
  1) `BaseSolidRevolve` 必须在草图里画一条**构造线**当旋转轴，否则一律 `Solid revolve feature failed`；
     剖面还不能跨 x=0。
  2) `writeInput` 只写装配体——零件必须先 `Instance(dependent=ON)`，否则 `.inp` 里一个节点都没有（229 字节）。
  3) 草图级 `FilletByRadius` 在 2018 里要的是边不是顶点（`arg2 expecting ConstrainedSketchGeometry`），
     改在零件级 `part.Round(edgeList=tuple(edges), radius=r)`，`edgeList` 必须是元组，不能直接给 findAt 的序列。
  4) 预览要显示网格只能用 `vp.partDisplay.setValues(mesh=ON)`；加上 `renderStyle`/`visibleEdges` 会抛异常
     被 except 吞掉，图还是无网格的着色图。
