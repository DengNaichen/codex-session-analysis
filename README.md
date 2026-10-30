# Codex 本地工作记录

零第三方依赖，Python 3.9+。原始 `~/.codex` 只读；数据不上传。网页只从本地服务器加载清洗后的数据，无 CDN、遥测或外部接口。

```sh
cd ~/Developer/codex-session-analysis
python3 -m unittest -v
python3 clean_sessions.py --output output/your-new-snapshot
python3 prepare_dashboard.py output/your-new-snapshot
python3 -m http.server 8765 --bind 127.0.0.1 --directory web
```

浏览器打开 http://127.0.0.1:8765。每次清洗指定新的目录，脚本拒绝覆盖已有目录。默认上海时区，可用 `--timezone` 修改。仅需删除该独立目录即可卸载；不更改 Codex 配置或原始记录。

## 数据文件

- `events.jsonl`：消息、操作类别与状态、轮次生命周期、语音转录；包含原始文件相对路径和 1-based 行号。
- `primary_messages.jsonl`：主会话的用户与助手消息，后续定性分析入口。
- `sessions.jsonl`：线程归属、项目目录、模型、事件数量与来源文件。
- `manifest.jsonl`：读取时文件字节数和 SHA-256；活动 session 后续可继续增长。
- `report.json`：计数、解析失败位置、限制说明。

## 清洗口径

以 `session_meta.id` 识别线程；`session_id` 可能是多个子线程共享的根 ID。区分 primary、subagent、child。多个文件对应同一线程时合并，按显式记录 ID 去重；没有 ID 时使用时间、轮次和内容组合。同一表示中不同 ID 的重复用户发言保留。每轮优先采用已完成 UI item，再采用 response_item，最后采用旧版 event message；不把多种镜像重复计算。消息按角色选来源；操作按轮次选来源，因此不完整镜像可能遗漏部分内容。

移除系统/开发者提示、推理、压缩历史、大段工具结果、调用参数和命令正文。用户消息中剥离已知环境/插件/AGENTS 注入块；保留用户正文和助手回复。过滤常见密钥、密码赋值、邮箱、用户名路径及 URL 查询参数；这只是启发式脱敏，不能保证匿名，也不宜直接公开数据文件。附件二进制不导出。

## 分析限制

用户角色不保证一定由真人输入，也可能包含自动化提示、粘贴内容、fork 继承历史。主会话筛选是分析起点，不是严格的人类活动证明。没有自动判断任务是否成功；操作 status/exit code 只代表相应操作。事件活跃时段、轮次耗时都不是人的实际工时。普通文本中项目名与业务内容保留，以支持本地分析。UI 操作与底层调用的粒度不同，工具频率应视为描述性指标。语音转录单独保存，尚未与文字消息去重。

## 回复用时与语义复核

```sh
python3 analyze_timing.py --cleaned output/initial
python3 prepare_dashboard.py output/initial
```

`output/initial/timing.json` 保存逐轮、逐条助手回复的计时与质量报告。整轮耗时优先取运行时 `duration_ms`；首 token 延迟来自 `time_to_first_token_ms`，不保证是用户可见文字。逐条回复展示从轮次开始到消息记录时间的间隔、与同轮上一条助手回复的间隔；包含工具执行和等待，不是该消息纯生成耗时。日志没有可靠的逐条生成边界，`generation_ms` 保持 null。重放造成负耗时或消息晚于轮次完成的时间异常也置空。并行轮次耗时不可直接累加为人的总等待时间。

`semantic/batch1.json`、`batch2.json` 是本次主会话语义复核输入：每条用户消息最多 2500 字，并带下一条助手回复的 300 字片段。`review1.json`、`review2.json` 是 Astra medium subagent 阅读上下文后的会话级解释及有依据的消息标记，非自动关键词标签。较长消息可能截断；分类与建议属于可核查的判断，不是已证明的效率损失。每个 flag 保留原消息文件和行号 key、turn_id。子 agent 历史不做人的工作模式推断。

Linear 只核对部分明确提到的票据，`semantic/linear_context.json` 保存核对时间、当前状态与范围摘要；当前 Done 不可倒推会话发生时已完成。新的快照需要重新生成语义输入并复核，旧标记不得宣称覆盖新增数据。

## 正式保存：SQLite + 版本快照

清洗后的数据已保存于 `data/snapshots/<内容哈希>/`。每版包含：

- `sessions.sqlite`：可查询的会话、事件、轮次、逐条回复计时、语义结论/标注和 Linear 上下文。
- `cleaned/`：完整 JSON/JSONL 清洗结果、计时、源文件指纹与质量报告。
- `semantic/`：本版语义复核输入、输出和 Linear 核对结果。
- `pipeline/`：生成与校验脚本副本。
- `receipt.json`：版本号、保存时间、表行数、文件 SHA-256。

`data/current.json` 指向当前正式版本。`web/data.json` 是网页缓存，`output/initial` 是工作产物；正式快照不依赖它们继续存在。SQLite 和快照文件仅限当前用户读写。新版本不覆盖旧版本；数据与脚本不变时复用同一版本，并验证现有文件未改变。保存失败不会更新 current 指针。

```sh
python3 save_dataset.py
# 其他快照须传入与之匹配的语义复核目录：
python3 save_dataset.py --dataset output/your-snapshot --semantic-dir semantic
python3 -m unittest test_save_dataset.py -v
```

仅有未复核的新数据时，不要复用旧语义结果冒充新覆盖。保存器要求必需数据文件齐全，校验语义证据与回复的 session、turn 和来源行号匹配。

主要表：`sessions`、`events`、`turns`、`replies`、`semantic_sessions`、`semantic_flags`、`linear_issues`、`session_issues`、`source_files`。`payload_json` 保留原始清洗对象的附加字段。`reply_analysis` 视图已关联逐条回复、项目、整轮耗时和来源位置。没有记录的时间为 SQL NULL，不用 0 代替。

例如用任意 SQLite 工具打开 current.json 所指数据库后：

```sql
-- 查每条助手回复与耗时
SELECT session_id, turn_id, phase, text,
       elapsed_from_start_ms, gap_since_previous_reply_ms, turn_total_ms
FROM reply_analysis
ORDER BY timestamp;

-- 查看人类主会话中进度消息最多的轮次
SELECT t.request_text, t.total_ms, t.commentary_count, t.final_count,
       s.project, sem.fragmentation_reason
FROM turns t
JOIN sessions s USING(session_id)
LEFT JOIN semantic_sessions sem USING(session_id)
WHERE s.session_kind = 'primary'
  AND (sem.origin IS NULL OR sem.origin NOT IN ('automation','test'))
ORDER BY t.commentary_count DESC
LIMIT 20;
```

这是本地持久化，不是异机灾备。需要备份时复制完整 `data/` 目录；原始 Codex session 不会被复制或修改。SQLite 完整性和外键检查在每次新保存时执行。语义标签是 agent 判断，不作为已证明的效率损失。

## Luna 消息分类试验

`experiments/luna-v1/` 保存分类口径、固定随机种子的100条分层样本、两批 Luna 输出、统计和复核案例。样本来自46个主会话，每个会话最多5条，带目标消息前的8条对话与初始需求。目标最多6000字，上文每条最多1400字，开场需求最多1600字；截断标记随数据保留。抽样桶仅用于覆盖场景，不是分类答案，样本比例不能解释为总体比例。

- `prepare_luna_pilot.py`：从当前只读快照构造样本；已有试验不要在未经归档时重新生成。
- `rubric.md`：阶段、消息作用（多标签）、响应要求、必要性、能否汇总、置信度、原文证据。
- `labels1.json` / `labels2.json`：两位 `gpt-5.6-luna` subagent 逐条分类，medium 推理。不调用 Vercel API，也不需要 key。
- `save_luna_pilot.py`：检查100条完整覆盖、字段枚举、目标及前文引用、来源SQLite正文一致性，然后事务保存至 `data/classification_runs.sqlite`。

分类数据库与正式源快照分离：`runs` 保存模型、规则版本、来源快照及输入指纹；`classifications` 以 `(run_id,event_id)` 为主键，保存标签、理由、原文证据及完整实际输入/输出。event_id仅在其来源快照内有效，不是跨版本全局ID。输入或标签变化生成新run，重跑同一输入不重复插入。

这是模型分类试验，尚未由用户校准。无需回复的进度消息可以延后汇总，但不能据此断言用户已经受到打断；必须决策的消息也不等于无效率。

## 按理想协作节奏复核（v2）

本轮以“共同讨论、关键决策、亲自验收”为中心，另列正常委托、额外介入候选、自动化和待判断。`experiments/rhythm-v2/` 包含固定快照的完整133个主会话、1156条用户角色记录，按会话分为三批，由3个Luna medium独立处理各自批次。不是三个模型对同一数据投票，因此不能声称模型间一致性。

每条输入保留用户原文（最多3000字）、前两条助手消息（各2200字）和下一条助手回应（500字），并标注截断。另存语义正文：移除浏览器环境包装，将批注转为引用及用户批注，并保留其后的真实请求；完整原文仍在源快照中。模型逐会话阅读用户序列；下一条助手回应只帮助解释后续，不是前一时刻已知的授权。验收路径只在相关用户参与记录上标记，不覆盖所有交付事件，也不能推断人实际耗时。

`save_rhythm.py` 校验逐会话完整覆盖、event_id唯一、标签枚举、原文证据、源SQLite正文一致后，将结果事务写入 `data/classification_runs.sqlite` 的 `rhythm_runs`、`rhythm_sessions`、`interventions` 表。source snapshot与run_id共同区分版本，保留实际输入、输出和规则全文；源快照不被覆盖。

```sh
python3 save_rhythm.py
python3 -m unittest test_rhythm.py -v
```

前端 `/rhythm.html` 支持按参与类型、验收路径、项目与正文筛选，每条标签可展开查看助手上下文。额外介入只是复核候选；模型confidence是自评，不等于测得准确率。用户体验产生新要求可以是有效验收，不默认计为返工。准备脚本 `prepare_rhythm.py` 只用于初始化新试验；已复核数据需要先归档后再更新输入。
