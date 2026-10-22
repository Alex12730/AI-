# 本地文案生成与版本编辑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking. 推荐当前会话直接实施，最后进行一次独立代码审查；待用户审阅计划并选择执行方式后开始产品代码。

**Goal:** 在现有工作台内从一句话实际生成中文文案与分镜草稿，支持手改、定向改写、候选比较、版本保存和确认后继续制作。

**Architecture:** 使用独立的本地 Qwen 写作权重与便携 llama.cpp，在现有 Task/Worker 的全局串行队列中执行。写作版本不可变，生成结果仅添加候选，不覆盖编辑稿；确认版本后创建对应 Storyboard/CreationPlan。现有三种视频模型、旧项目与模板入口保留。

**Tech Stack:** 现有 Python/FastAPI/SQLAlchemy/Alembic/Jinja；stdlib urllib/subprocess；Qwen3-8B GGUF Q4_K_M；Windows x64 llama.cpp CPU 便携包。首版固定 CPU，避免额外 CUDA 部署与和 ComfyUI 常驻显存冲突；实际速度以本机测量为准，不宣称最快或最佳模型。

**Spec:** `docs/superpowers/specs/2026-10-22-local-creative-pipeline-design.md` 第 1、3（想法/文案/分镜）、4、5、6.1 节。本计划只覆盖第一阶段，声音、角色图、图生视频及最终全链断网验收继续另列计划。

## Global Constraints

- 默认“每步先预览确认，再继续下一步”。生成文案不触发图片、配音或视频任务。
- “文案编辑区可随时保存”；“新结果单独展示，由用户选择替换；不能覆盖用户已手动修改的版本”。
- “继续复用当前队列和单 GPU 串行调度，不为文字、绘图或声音另建相互竞争的执行器”。CPU 写作仍走同一任务队列。
- “准备完成后的日常创作不得请求公网、自动补下载或失败转云端”。推理只使用清单中的绝对本地路径；禁用代理、重定向、远程模型参数和外部 URL。
- “项目专用 Python、写作服务、绘图、配音、视频、字体和帮助须全部脱离 Codex 缓存”。本批写作引擎采用独立 EXE；业务 Python 迁移仍是全链独立部署的后续验收项，不可据此宣称整站已独立。
- 只修改本人作品，所有表单保留 CSRF；原团队审批、管理员权限与视频十次准入规则不放宽。
- 安装下载和推理严格分开；不在用户打开首页时下载模型或启动生成。保留许可证、原始版本和 SHA-256。
- 既有五秒分镜合同继续约束第一版发布；不按字数宣称精确配音时长，不保证角色一致性或精确对白。

## Review Focus

1. 生成期间用户保存了新手稿：任务 2/3 检查任务只新增候选，当前选择和手稿不改变。
2. 双标签页同时修改/确认：任务 2 的版本冲突、唯一任务键与幂等确认测试。
3. 长中文、空 JSON、字段缺失、输出截断与模型未准备：任务 3 明确失败，不填假文案，不破坏其他任务。
4. 服务端口占用、超时和 Worker 租约失效：任务 3 只结束自己启动的进程，过期结果不得落库。
5. 改写后的确认影响正在制作的版本：任务 2/4 新建分镜版本和计划，不覆盖旧计划的镜头、素材或任务 ID。

## 文件与数据边界

新增 `writing_contracts.py` 定义输入/输出；`services/writing.py` 管版本和发布；`adapters/local_writer.py` 只负责固定本地引擎生命周期及结构化生成；`writing_web.py` 管页面表单。`worker.py` 增加 `write_script` 分支并复用心跳机制，不把写作塞进视频 GenerationRequest。

新增表：

- `writing_documents`：id、project_id（唯一）、created_by_id、idea、recipe、target_seconds、aspect_ratio、latest_version（默认 0）、selected_version（可空）、created_at。
- `writing_revisions`：id、document_id、version、parent_version（可空）、kind（manual/generated/rewrite）、body_json、created_by_id、task_id（唯一可空）、created_at；唯一 `(document_id, version)`。
- `writing_publications`：id、revision_id（唯一）、creation_plan_id（唯一）、confirmed_by_id、confirmed_at；重复确认同一文案版本返回原计划。

`WritingBody`：schema_version=1，title（1–160 字）、summary（1–2000 字）、script（1–20000 字）、characters（最多 12 项角色描述，不表示已生成角色图）、shots（1–36 项）。镜头字段 title（1–200 字）、visual（1–2000 字）、narration（最多 2000 字）、scene_type（product_ui/broll/character）、duration_seconds（固定 5）。镜头数必须等于目标秒数/5；不符合时返回可理解错误，不自行截断。

生成操作 `operation` 仅允许 generate/shorter/conversational/ending/custom。`custom` instruction 长度 1–2000 字。想法长度 1–4000 字，目标时长沿用 5–180 秒且为 5 的倍数。首版输入总文本 UTF-8 不超过 24000 字节；超限在入队前说明需精简，防止运行时上下文溢出。

## Task 1: 写作合同与持久化版本

**Files:** Create `src/video_workstation/writing_contracts.py`, `migrations/versions/20261022_0006_writing.py`, `tests/test_writing_contracts.py`; Modify `models.py`, `tests/test_database.py`。

**Interfaces:** Produces `WritingRequest` 和 `WritingBody`（Pydantic BaseModel）；`WritingDocument`、`WritingRevision`、`WritingPublication`（SQLAlchemy）。`WritingRequest` 包含 idea、recipe、target_seconds、aspect_ratio、operation、base_body（WritingBody|None）、instruction（str，默认空）。

- [x] 先写合同测试：合法的 30 秒对象包含 6 段五秒镜头；31 秒、空文案、37 段、超长自定义要求、未知操作均被拒绝；body 拒绝多余字段，正文不能包含被当成 HTML 执行的内容。
- [x] 运行 `.venv/Scripts/python.exe -m pytest tests/test_writing_contracts.py -q`。Expected: FAIL，缺少新合同/模型。
- [x] 实现合同、三张表与迁移。将 `creation_plans.project_id` 的唯一约束移除，改为普通索引，保留 storyboard_id 唯一；允许同一作品的多个已冻结计划分别指向不同分镜版本。迁移须兼容初始迁移使用当前 metadata 的空库安装；对旧库用 batch/naming_convention 明确删除旧唯一约束，不重建或清空用户数据。
- [x] 增加旧库迁移测试：以 0005 表结构建库并保留一个用户、旧 CreationPlan 与 Task；升级后旧 ID 均不变，同一项目可有两个不同 storyboard_id 的计划，重复 storyboard_id 仍被唯一约束拒绝。
- [x] 跑合同、迁移及既有 creation 测试。Expected: PASS，迁移 head 为 `20261022_0006`。
- [x] 提交本任务，记录迁移和检查结果。

## Task 2: 草稿、候选、确认发布与版本冲突

**Files:** Create `src/video_workstation/services/writing.py`, `tests/test_writing.py`; Modify `services/creation.py`（提供结构化分镜创建入口，保留旧模板路径）。

**Interfaces:** Consumes Task 1 models/contracts. Produces:

- `create_writing_document(session, actor, request: WritingRequest) -> WritingDocument`。
- `save_writing_revision(session, actor, document, body: WritingBody, *, expected_latest: int) -> WritingRevision`。
- `enqueue_writing(session, actor, document, request: WritingRequest, *, expected_latest: int, request_key: str) -> Task`。
- `store_writing_candidate(session, task, body: WritingBody, *, lease_token: str) -> WritingRevision`（task_id 幂等，不更改 selected_version）。
- `select_writing_revision(session, actor, document, version: int, *, expected_latest: int) -> WritingRevision`。
- `publish_writing_revision(session, actor, document, version: int) -> CreationPlan`。

- [x] 先写测试：手工保存 v1→排队改写→另存手稿 v2→改写结束只新增 v3，selected_version 仍为用户选定版；错误 expected_latest 返回冲突；其他成员或管理员不可代替所有者修改个人稿。
- [x] 写确认测试：同一 revision 两次确认得到同一个 plan；改写后确认得到新 storyboard.version 与新 plan，旧 plan.snapshot_json/Task/Shot 均不改变；未确认不会调用 start_creation、排视频或修改 approved 状态。
- [x] 运行 `.venv/Scripts/python.exe -m pytest tests/test_writing.py -q`。Expected: FAIL，缺少上述服务。
- [x] 实现服务。创建文案容器时保留一个合法 draft Storyboard，以兼容既有项目序列化；发布时创建新的结构化 Storyboard 和 draft CreationPlan，镜头数和描述取自用户选择的 WritingBody，不再重新按标点拆句。用户后续仍需确认制作，不在文案确认时开跑视频。
- [x] 并发写入先取得 document 所属 Project 的写锁，再读取 version counter；更新/确认必须重读，避免依赖过期 ORM 实例。任务 request_key 在同文档/用户/相同 payload 内幂等；同 key 不同 payload 返回冲突，不吞掉用户修改。
- [x] 在写作 Task 中冻结 request、base_revision、模型配置指纹、模板版本、deployment manifest SHA；候选结果落库与 task.succeed 同一事务完成，拒绝失效 lease_token。
- [x] 跑版本、发布与旧 creation 测试。Expected: PASS；提交本任务。

## Task 3: 真正的本地写作引擎与 Worker 分支

**Files:** Create `src/video_workstation/adapters/local_writer.py`, `tests/test_local_writer.py`, `tests/test_writing_worker.py`; Modify `worker.py`, `services/models.py`, `offline.py`（仅在需要时增加固定 manifest 校验，不削弱旧策略）。

**Interfaces:** Consumes `WritingRequest`/`WritingBody`、Task 2 candidate service. Produces `LocalWriterError(ValueError)` 与 `LocalWriter(manifest_path: Path).generate(request: WritingRequest, *, work_dir: Path, timeout_seconds: int=900) -> WritingBody`；`validate_writer_deployment(manifest_path: Path) -> dict`。

- [x] 先写 loopback 测试服务器和进程测试：响应为合法 body 时结构化解析；空结果/缺字段/截断/finish_reason=length/超过 1 MiB 响应/重定向/外部 URL/非本人进程占端口必须失败。中文、双引号、换行及脚本状输入不能变成 Shell 指令。
- [x] 写 Worker 测试：write_script 和视频任务走同一 QueueService；推理过程保持心跳；成功只新增候选；重复恢复只生成一个 revision；过期租约或模型指纹变化拒绝写回；缺文件不下载、不调用云端、其他任务可继续。
- [x] 运行新测试。Expected: FAIL，缺少 adapter 与 Worker 分支。
- [x] 实现 adapter：由 Worker 启动其专有子进程，固定 `127.0.0.1:8091`、`--model <绝对 GGUF 路径>`、`--offline`、`--jinja`、`--n-gpu-layers 0`、`--ctx-size 16384`、单并发。端口已占用即阻塞，不连接未知服务。只使用 urllib `ProxyHandler({})`，禁止跟随重定向，健康检查超时 120 秒，整个生成超时 900 秒。
- [x] 在向 8091 发送健康请求前，通过 Windows TCP 端口所有者信息核对监听 PID 等于本次启动的 EXE PID，同时检查子进程未退出；端口被其他进程抢占也要失败，不能凭 HTTP 200 认领未知服务。测试模拟检查与启动之间的端口竞争。
- [x] 请求 `/v1/chat/completions` 使用 WritingBody JSON schema、`chat_template_kwargs.enable_thinking=false`、temperature=0.7、top_p=0.8、top_k=20、min_p=0、presence_penalty=1.5、max_tokens=4096；不发送 tools、图片 URL 或任何远程引用。模型输出仅作为候选数据，不执行其指令或代码。
- [x] 子进程在任务结束/异常的 finally 中先 terminate、等待最多 10 秒、必要时 kill 并 wait，只操作本次启动且仍持有的进程对象。日志写本地忽略目录，默认不在错误提示中公开完整文案。
- [x] 为崩溃恢复保存引擎 PID、创建时间和 task_id 的本地所有权记录；重新启动只清理标识和可执行绝对路径都匹配的旧引擎。无法核实时阻塞该写作任务，不能用 taskkill 全杀 Python 或未知 8091 服务。完成后删除本任务所有权记录。
- [x] `qwen-local` 增加 `text_ready` 独立能力状态及真实准备测试指纹；不使用视频 validated_presets 伪造文案准入。现有 model_readiness 的视频语义保持兼容，界面明确用途。
- [x] 跑新测试及完整 pytest。Expected: PASS；提交本任务。

## Task 4: 工作台内写作、比较和修改

**Files:** Create `src/video_workstation/writing_web.py`, `templates/writing.html`, `static/writing.js`, `tests/test_writing_api.py`; Modify `main.py`（注册路由）、`creation_web.py`、`templates/create.html`、`templates/works.html`、`static/app.css`。

**Interfaces:** Consumes Tasks 1–3. Produces CSRF 保护的 POST `/writing`、`/writing/{id}/generate`、`/save`、`/select`、`/publish`；GET `/writing/{id}`。每个写入都需要本人权限及版本标识。不要在 GET 中触发生成。

- [x] 先写 HTTP 测试：一句话新建→生成任务→候选显示→手改保存→选择版本→确认文案进入该版本的分镜；越权/无 CSRF 返回 403，失效版本返回 409 并保留用户提交内容；无引擎时清楚说明缺少能力，原模板入口仍能用。
- [x] 写重复提交和旧计划测试：双击发布返回同一计划；已有视频执行中时确认新的文案，不修改或取消旧 Task；“我的作品”将文案及各版制作计划按同一项目归组，显示版本和阶段，避免同名重复行让人误认为多个无关作品。
- [x] 运行 API 测试。Expected: FAIL，缺少写作路由/页面。
- [x] 修改开始页：提供“让工作台帮我写”和“我已有完整文案”两种入口，前者是默认；视频类型说明直接跟随下拉选择变化，移除底部重复的说明长列表。移除“配音请先自行录制”的误导，准确显示独立录音导入尚未接通。
- [x] 写作页有可编辑标题/概要/完整文案、折叠分镜建议，主按钮“保存我的修改”和“确认文案，查看分镜”；改写按钮“更简短”“更口语”“改结尾”“按要求改写”“另写一版”。未保存修改必须先显式保存或提示；收到候选绝不自动替换编辑区。
- [x] 版本列表显示来源（手工/生成/改写）、时间及所基于版本；选中历史版本可复制为新手稿。模型名仅放高级说明；没有角色图/声音能力的步骤不可显示已完成或可生成。
- [x] 浏览器验证 1280 与 390 视口、键盘、生成中仍可编辑、错误后正文保留；网络检查页面资源均为本机。执行 node `--check static/writing.js` 及完整 pytest。Expected: PASS；提交本任务。

## Task 5: 准备离线模型、真实生成和交付

**Files:** Create `scripts/prepare_local_writer.py`, `scripts/verify_local_writer.py`, `config/writer-deployment.schema.json`, `docs/operations/local-writing-guide.md`, `tests/test_writer_deployment.py`; Modify 本机忽略的 `data/writer-deployment.json` 及正式用户运行配置（成功验证后再启用）。

**Interfaces:** Consumes Task 3 deployment manifest contract. Manifest schema=1，包含 engine 的 version、exe_path、文件列表/哈希/字节数，model 的 repo、immutable revision、filename、sha256、bytes、license_path，context_size=16384、gpu_layers=0、template_version、verification（时间、用例和结果）。所有路径来自部署根的相对路径，拒绝绝对外部路径、越界、缺失 DLL 和指纹不一致。

- [x] 先写测试：损坏文件、../路径、URL 型路径、缺少 SHA、清单变更后旧验证记录全部不能就绪；准备流程中断只留下 .part，不激活半个包；不运行下载的脚本，不通过模型卡命令自动安装。
- [x] 运行部署测试。Expected: FAIL，缺少准备/清单验证接口。
- [x] 准备脚本只在明确调用时联网：从 Qwen 官方仓库解析并固定 commit，再取得 `Qwen3-8B-Q4_K_M.gguf` 的真实 LFS SHA-256/字节数；从 ggml-org/llama.cpp 官方发布选择 Windows CPU x64 包，固定具体 release/asset ID 和发布 digest（没有官方 digest 则记录来源并独立验证，不伪称官方哈希）。保存 LICENSE、完整引擎 DLL 清单，检查存储后下载到 .part，校验后原子激活到 `models/local-writer` 与 `runtime/local-writer`。不得下载整个含所有量化的模型仓库。
- [x] 首次准备失败可按官方来源重试，但不能偷换模型/量化；资源无法取得时记录具体阻塞证据。日常 adapter 不包含安装、HF/远程模型参数或联网修复路径。
- [x] 用真实模型分别生成故事、产品教程、知识讲解三种稿；再对一个已手改版本执行改写，实测结构、中文可读性、耗时、内存及版本隔离，结果写入 `data/writing-verification`。这不等于画质或商业成片质量保证。
- [ ] 完成断网推理检查（本批未做物理断网，按本条约定保留未验收）：下载准备完成后关闭代理与外部可达网络，在受控验证窗口内实际从一句话生成并保存。记录具体断网手段、引擎进程绝对路径和命令参数；若不能安全执行真实断网，保留“未验收”，不以静态 URL 扫描替代。
- [x] 正式数据迁移前做 SQLite 一致性备份。确认没有活动用户任务后更新本机 Web/Worker；保留账号、素材与旧版本。完整测试及独立代码审查通过后本地提交；不自动推送 GitHub。
- [x] 最后更新操作说明，清楚区分本阶段可用的写稿与仍待接通的图片、配音、角色参考和全链独立部署。

## 官方资料核对与选型依据（2026-10-22）

- [Qwen 官方 Qwen3-8B GGUF 模型卡](https://huggingface.co/Qwen/Qwen3-8B-GGUF)：存在正式对话模型及 Q4_K_M，不使用视频编码器代替写作；[文件列表](https://huggingface.co/Qwen/Qwen3-8B-GGUF/tree/main)显示目标单文件约 5.03 GB。其内容质量和本机速度仍需实测。
- [llama.cpp 官方 server 文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)：支持本地模型、CPU/GPU、JSON schema、关闭思考模式及 `--offline`。部署时固定具体二进制版本并验证这些参数，而非依赖浮动 master 的未来行为。
- 官方 release API 的 latest 可能只提供版本指针文件，不能直接假设有 Windows ZIP；准备脚本须按实际 assets 查询二进制发布并固定，不猜测 URL。

## 自查及后续范围

已按 spec 第 6.1 节收敛为可单独验收的文案阶段。五项 Review Focus 分别有任务 2、3、4 对应测试。后续声音/角色/参考图/剪辑不在本计划混写；所有确认行为只推进至下一阶段，不自动开启整片推理。

用户已批准直接实施与最终独立审查。任务 1–5 已落地并部署；160 项自动化测试通过，真实三类写作与改写、网页至 Worker 闭环已验证。独立审查发现的跨标签页基线、终态孤儿进程及旧报告复用问题均已修复并复审通过。物理断网验收仍未完成，按任务 5 的例外明确记录，不视为已通过；完整独立 Python、角色与声音继续后续阶段。
