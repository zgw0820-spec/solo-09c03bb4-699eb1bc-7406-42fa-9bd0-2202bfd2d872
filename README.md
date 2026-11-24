# 收包进程换核演练

用 Flask 页面演练“网卡队列换核后，收包进程跟着换绑，但不先停进程腾位置”。

- 2～4 个单线程样例进程，各自只绑一颗逻辑 CPU，彼此不共核；
- 页面列出容器可用逻辑 CPU、每个样例当前绑定（每次请求都从 `/proc` 重读）；
- 值班员一次提交一组目标核，编排器用 `sched_setaffinity` 真正换绑；
- 换环（含最简单的两两互换）借一颗空闲核周转，任何一步都不会让两个样例共核；
- 池里没有空闲核且目标成环时整组拒绝，一个进程都不动；
- 可指定“第 N 步在写入前报错”，用来演练中途失败；
- 中途失败自动按内核当前状态回退到原绑定，回退步骤同样不共核；
- 页面逐步列出每一步之后从内核读回的绑定，以及回退步骤。

## 目录

```
drill/               编排器与页面
  cpulist.py         内核 CPU 列表格式（0-3,6）解析/格式化
  kernel.py          /proc 读取、sched_setaffinity 写入、cgroup cpuset
  planner.py         换核计划：直接换 / 借空闲核 / 走不通则拒绝
  supervisor.py      启动并监督样例进程（独立 OS 进程，JSON 行协议）
  session.py         单组受理、执行、回读、失败回退、历史
  web.py             Flask 路由与 JSON 接口
  templates/index.html, static/drill.css
sample/sample_proc.py  单线程收包样例（忙闲交替，带控制通道）
scripts/dev_run.sh     无 Docker 的本机演练（taskset 限定 CPU 池）
scripts/acceptance.py  通过 HTTP 跑“互换”和“中途失败”两个场景
scripts/test.sh        单元 + 集成测试
tests/                 计划器单元测试、真实进程集成测试
vendor/                预下载的 Flask wheel（离线构建镜像用）
```

## 运行

### Docker Compose

```bash
docker compose up --build
# 打开 http://<宿主机>:8008/
```

`docker-compose.yml` 里 `cpuset: "0-5"` 就是页面上的“容器可用 CPU 池”，样例数由
`DRILL_SAMPLES` 控制（2～4）：

```bash
DRILL_SAMPLES=2 docker compose up --build
```

说明：样例进程由 `drill` 服务的编排器**在容器内以独立 OS 进程**拉起，而不是每个
样例一个 Compose 服务。原因是要对别的进程调用 `sched_setaffinity` 必须处在同一个
PID 命名空间；同容器同 uid 也不需要额外 capability。若以后要拆成多服务，需要
`pid: service:...` 共享 PID 命名空间并补 `SYS_NICE`，见“未完成”。

### 本机（没有 Docker 时）

```bash
./scripts/dev_run.sh                 # CPU 池 0-5，3 个样例，http://127.0.0.1:8008/
POOL=0-3 SAMPLES=2 ./scripts/dev_run.sh
```

脚本用 `taskset -c $POOL` 限定可用核，语义与 Compose 的 cpuset 一致；依赖从
`vendor/*.whl` 解包到 `.deps/`，不需要联网和 pip。

## 页面怎么用

1. “容器可用逻辑 CPU”显示池内每颗核的占用者与负载，空闲核可被借来周转。
2. “样例进程与当前绑定”显示 PID、线程数、`Cpus_allowed_list` 和每个线程的绑定。
   “查询样例”会让样例进程自己再读一次 `/proc/self/status`，作为旁证。
3. 表格里为每个样例选一颗目标核（下拉框标出当前持有者），点“提交这一组”。
   - 目标核不得重复、必须在池内；
   - 上一组没结束时不收新组；
   - 目标成环且没有空闲核 → 整组拒绝，不改动任何进程。
4. “故障注入”填 N，表示计划中的第 N 步在**写入亲和性之前**抛错，用来演练回退。
   故障步号超过本组步数时整组不受理。
5. 提交后页面显示计划、每一步的内核回读、失败原因和回退步骤。

## 换核算法

设当前每个样例独占一颗核。每一步只移动一个样例，且只移到“此刻没有别的样例”的核上：

- 目标核空闲 → 直接换；
- 目标核被占 → 先找一个空闲核把某个待换样例临时停靠（不是停进程，只是改绑），
  腾出它的旧核给链条上的下一个样例；
- 仍无空闲核且目标构成环 → `PlanError`，提交阶段就拒绝，未做任何写入。

回退用同一套算法（`plan_restore`），但允许起点“已经被改坏”（例如外部把两个样例
放到同一颗核上）：先把冲突样例停靠到空闲核，再回到原绑定；即使原池已满，回退也
不会把两个样例放到同一颗核上。

## 测试与实测

```bash
./scripts/test.sh                                   # 单元 + 真实进程集成测试
./scripts/dev_run.sh &                              # 另起页面
python3 scripts/acceptance.py                       # HTTP 层跑互换与中途失败
```

- `tests/test_planner.py`：CPU 列表解析、互换/三环/无空闲核拒绝、随机与穷举的全部
  置换，逐步校验“每进程单核且互不共核”。
- `tests/test_integration.py`：真实拉起样例进程，验证都是单线程单核不共核；真实
  互换；对第 1..N 步逐一注入故障并验证回退到原绑定；页面/JSON 接口回读；无空闲核
  时拒绝且不动；并发提交只受理一组。
- `scripts/acceptance.py`：只走公开页面和 `/api/state`，和值班员点击路径一致。

实测环境（本项目交付时）：

```
本机 CPU 池 0-7，任务用 taskset -c 0-5 限定为 6 颗；
3 个样例 rx-1/rx-2/rx-3 分别绑 0/1/2，均为 1 线程；
互换 rx-1<->rx-2：计划 rx-1 0→3（借空闲核）；rx-2 1→0；rx-1 3→1，最终 1/0/2；
中途失败（第 2 步注入）：第 1 步成功、第 2 步失败、回退 1 步，最终回到 1/0/2；
32 个单元/集成测试全部通过。
```

## 未完成与已知边界

- **没有在真实 Docker 里跑过**：交付环境没有 docker 命令，Compose/Dockerfile 只做了
  静态编写；实测用本机 `taskset` 限核 + 独立 OS 进程等价覆盖了换绑逻辑。
- Docker 镜像固定使用 Python 3.11（`markupsafe` 提供的是 cp311 wheel）；换 Python
  版本需要换 wheel。
- Compose 目前是“一个编排器服务 + 容器内多个样例进程”。要真正“每个样例一个
  Compose 服务”，需要共享 PID 命名空间（`pid: service:x`）、为编排器加 `SYS_NICE`、
  并把控制通道改成挂载的 Unix socket 或 HTTP；这部分未实现。
- cpuset 在部分宿主机/Compose 版本上不生效时，池会退化成全部可见 CPU；页面的池
  展示仍以 `sched_getaffinity` ∩ cpuset 为准。
- 故障注入只覆盖“设置前报错”，不模拟内核部分写入失败；真实写入失败路径（权限、
  ESRCH、回读不一致）已实现并有分支，但没有专门的故障注入去触发。
- 页面是单实例内存态（历史只保留最近 6 组），不做持久化；重启编排器后样例进程
  会重新拉起并回到默认分布。
