# mcp-abaqus 测试分层

按离线逻辑、MCP通信和真实求解分层。六组离线检查及无求解器stdio检查可直接运行；真实求解冒烟会提交3个微型算例及一次datacheck。
stdio脚本自行启动独立的新服务进程，读取当前安装的代码；它们不会替用户重启已连接客户端中的旧服务。

| 脚本 | 需要 | 耗时 | 管什么 |
|---|---|---|---|
| `lint_dat.py` | 纯 Python | < 1 s | `.dat` 静默吞行口径：正例 / 多 step 惯常告警 / 恰好到上限 / 超上限截断 |
| `lint_render.py` | 纯 Python | < 1 s | 模板渲染契约：`str()` 语义、`\n` 注入、空数据行、卡序不重排、`unused_param`、去重身份键 |
| `step0_reference.py` | 纯 Python + fixture | < 1 s | 第 0 步的 8 项期望值由 `tests/fixtures/step0_reference/` 自己算出（含 `.msg` 0 B/616 B 的第二信号） |
| `batch_states.py` | 纯 Python | < 1 s | `done` / `stalled` / `datalint_unchecked` 的语义边界（unknown、pending、缺 .dat） |
| `mixed_batch.py` | 纯 Python + fixture | < 1 s | 一好一坏两 case 的真实产物：`datalint` 只对位置敏感、`.msg` 的 `with a jump` 4 vs 0 |
| `batch_contracts.py` | Python + MCP依赖，模拟求解器启动 | 数秒 | 20个测试：名称与批次冲突、参数预检、缺失产物、取消与停止失败、只读告警分页 |
| `batch_stdio.py` | **真 MCP stdio，不需要Abaqus** | 数秒 | 客户端收到重名拒绝、状态与审计字段；完整告警分页不会推进队列 |
| `post_reload_smoke.py` | **真 Abaqus + 真 MCP stdio** | 约40秒起，视环境而定 | 实际求解、静默丢行检测、告警摘要与查询、缺失`.dat`的审计标记 |

## 跑法

在项目根目录运行：

```powershell
$testFiles = @(
    'tests/lint_dat.py', 'tests/lint_render.py', 'tests/batch_states.py',
    'tests/mixed_batch.py', 'tests/step0_reference.py', 'tests/batch_contracts.py',
    'tests/batch_stdio.py'
)
foreach ($testFile in $testFiles) {
    & .\.venv\Scripts\python.exe $testFile
    if ($LASTEXITCODE -ne 0) { throw "Failed: $testFile" }
}

.\.venv\Scripts\python.exe tests/post_reload_smoke.py
```

`post_reload_smoke.py` 起一个**独立的 stdio server**（不碰用户会话里那个），提交 3 个微型作业到
`abq_jobs/smoke_relooad/run-<时间>-<随机后缀>/`（已被 `.gitignore` 覆盖），全部断言对着客户端拿到的 JSON 做。
各次运行产物均保留，不再自动清理旧目录；缺失`.dat`测试在`finally`中恢复文件。`e2e_batch.py`也使用唯一批次ID，避免覆盖已有队列。

## reload 之后的确认顺序（第 0 步零成本）

改了 `server.py` 之后，跑着的 MCP server 不会自己换代码——必须 `/reload-mcp`（或开新会话）。
`/reload-mcp` 杀的是 server 进程，不杀 detached 的 Abaqus 作业（每个 case 目录有 `<job>.mcp-job.json`，
批次状态在磁盘上、每次 `batch_status` 都 reconcile），所以**只需要等手上那一次调用返回**，不用等整个算例。

1. **第 0 步（0 个作业、不碰 Abaqus、不占 license）**：期望值的**证据**有两份，数字必须相同：
   - 离线：`tests/step0_reference.py`（读 `tests/fixtures/step0_reference/`，永远在）；
   - 线上：`list_batches` 取被 `.keep` 标记的历史参照批次（`smoke_relooad/run-*/` 里带 `.keep` 的那个）。
   下面是对值清单：
   参照批次在 `smoke_relooad/run-1112-124544/.keep`（`b1112-124546`）。
   旧版冒烟曾清理未标记批次；当前版本保留全部历史目录。若已运行算例产物缺失，应报告
   `unknown`、`stalled`及相应`datalint_unchecked`，不再退回`not_started`。
   `datalint` 是**每次调用时从磁盘上的 `.dat` 现算**的，所以 reload 后会直接追认历史批次——不用重跑任何作业。
   期望**值**而不只是字段名：

   ```
   suspicious_cases      == ["S02"]          # 空列表 ≠ 通过，可能是没生效
   datalint_truncated    == false
   stalled               == []               # done 但没跑完的 case（unknown：有产物无完成标记）
   datalint_unchecked    == []               # 语义：该批**所有已到终态**的 case 都有 .dat
                                             # （排队中的 case 不算——它还没生成 .dat）
                                             # 非空 = 这些 case 跑完了但产物不在了，历史审计有缺口
   S01: dat_present=true  datalint == []
   S02: dat_present=true  datalint == [{line:98, keyword:"FIXED MASS SCALING"}]
   S03: dat_present=true  datalint == []
   ```

   **`dat_present` 是把"干净"和"文件不在"分开的那一位**：`lint_dat` 对缺失的 `.dat` 回
   `findings=[] , present=false`，如果只看空列表，删掉的 `.dat` 会伪装成"通过"。
   顺便比 PID：新进程启动时间应晚于最后一次 commit。
2. **第 1 步**：`./.venv/Scripts/python tests/post_reload_smoke.py`（3 个微型作业及一次datacheck），
   验实际状态、审计字段、渲染告警摘要与`batch_warnings`查询。
3. 两条都绿再动真算例。

前提是 editable 安装：`site-packages` 里只有 `_editable_impl_mcp_abaqus.pth`（内容指向本仓 `src`），
没有 `mcp_abaqus` 真副本。**如果哪天重装成非 editable，`/reload-mcp` 只会重启一个装着旧副本的进程，
四个字段照样不出现，而且看起来像"reload 没用"。**

## 两条真机经验

- **小 deck 用 `cpus=1`**：单元素/几十单元的模型切不出多个 domain，packager 直接
  `***ERROR: The requested number of domains cannot be created due to restrictions in domain
  decomposition`。这个报错长得像"模型坏了"，其实是并行划分失败；`submit_batch` 的 `cpus=0`
  会走默认 4 核，所以微型冒烟必须显式给 `cpus=1`。
- **冒烟要断言 `summary == {"completed": 3}`**：只断 `done == True` 时，三个 case 全部 `error`
  也能通过（`done` 只是"都到终态了"）。这条是把上面那个坑钉死的唯一办法。
