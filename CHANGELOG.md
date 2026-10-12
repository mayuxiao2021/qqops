# CHANGELOG

## [3.4.7] — 2026-10-12

**"消息收不到"的根因与防护**(接 3.4.5 继续排):

- **`qqops napcat restart` 会把 NapCat 杀掉、然后报"未知动作: restart"**(`core.py`):
  老代码里 `restart` 只杀掉进程就往下走, 而启动分支要求 `action == "start"`, 于是既不启动、
  又落到末尾返回"未知动作" —— 命令说"不认识这个动作", 服务其实已经被杀了。后果很严重:
  用户以为只是命令写错了, 实际上消息从此全断(**本机排障时就真把它杀了一次**)。
  现在 `restart` = 停掉再启动。
- **看门狗只看进程、不看接口**(`core.py` / `runner.py` / `watchdog.py`):QQ 掉线时 NapCat 的
  **进程还活着**, 只是 OneBot 接口不再响应 —— 看门狗于是 24 小时都报"正常", 消息静默断流,
  直到用户说"收不到消息"。现在新增 `core.extras_check()`:按**真实接口健康**判, 连续 3 次不健康
  才重启(网络抖动不误杀), 重启后进入 10 轮冷却(给 QQ 登录时间);可选旁路(如 AstrBot)没在跑不算故障。
- **单进程模式下没人查旁路服务**:`watchdog` 在 single 模式不是独立线程, 那段 extras 检查压根不执行。
  现在主管循环(`runner.supervise_once`)自己也查。
- `watchdog.manage_extras` 默认由 `False` 改为 **`True`**:宁可自动重启一次, 也不要静默丢一天消息。
- **测试与 CI 绝不动真实旁路服务**:`QQOPS_NO_EXTRAS=1` 让体检只告警不动手。这是上面那条默认值改动的
  第一版踩的坑 —— 它让"跑一遍测试"就去启动真实的 NapCat/AstrBot(CI 上有 `screen`, 于是拉起空会话、
  每轮还等 24 秒), 把 runner 的时序测试搞崩, CI 与发布一起红。另外只有启动命令**真的存在**时才动手。
- 测试 186 → 187 项。

> 注:同一批改动曾以 v3.4.6 打过 tag,但那次 CI 与 Release 因上面那个"测试里去动真实服务"的问题失败、
> **没有产出 Release**;修好后作为 3.4.7 发布, 孤立的 v3.4.6 tag 已删除。

## [3.4.5] — 2026-10-12

**修三个"控制台按钮点了没反应 / 图片打不开"的真 bug**(都是用户先发现的):

- **控制台里所有图片都打不开**(`webapp.py` 的取图接口):`fh.read()` 被写在了 `with open(...)` **外面**,
  文件已经关闭才开始读 → 每个图片请求都是 HTTP 500 `read of closed file`。
  「数据 → 媒体」页、带图消息里的图全部打不开,日志里是一秒几十条 500。
  改成在 `with` 里读完再发送。新增测试:真起一个控制台、取一张图,断言 200 且内容与磁盘一致。
- **运维页「切换功耗档位 / 进程模式」按钮直接 500**(`console/pages.py`):`_spawn_restart()`
  用了 `os` 却没 `import os` → `NameError`。只有点那个按钮才会触发,所以一直没暴露。
- **单进程模式下「启动」按钮报"端口已被占用"**(`core.py`):单进程时服务是 runner 里的线程,
  `pgrep` 找不到独立进程,于是直接走到端口预检,报"端口 8080 已被占用(手机上无法定位占用者)" ——
  明明一切正常却像坏了。现在先用 `runtime_state()` + 真实健康检查确认"它已经在干活", 直接回答"已在运行"。
- **插件状态偶发保存失败**(`framework/manager.py`):插件开关保存自己拼 `plugins.json.tmp`,
  两个保存者抢同一个临时文件时,后一个 `os.replace` 报 `FileNotFoundError`(异常账本里抓到过)。
  改用 `core.atomic_write`,并把它的临时文件名加上 pid + 线程号, 从根上消除互删。
- 新增 **`tools/check_names.py`**:静态扫"用了但没定义的名字"(NameError 的静态版)—— 上面那个
  `os` 漏导入正是这类问题。已接进测试套件(CI 与手机上都跑);写它的时候它还顺手抓到了我自己新引入的
  `threading` 漏导入。
- 测试 184 → 186 项。

## [3.4.4] — 2026-10-11

**真机验收文档时抓到的一个真 bug:Linux 用户根本装不完。**

- `install.sh` / `uninstall.sh` 里写了 `"$PREFIX/bin"`,而 `$PREFIX` 是 **Termux 专有**的;
  脚本又是 `set -u` —— 在普通 Linux 上 `$PREFIX` 未定义,于是**第 5/7 步"注册命令"当场退出**:
  程序复制了、配置也生成了,但**服务没启动、结束提示也没打印**,用户看到的是半截输出加一个报错。
  (Termux 上一直正常,所以本机与手机都没暴露;是照文档在真机上跑一遍才发现的。)
  现在用 `${PREFIX:-}` 兜住,并在真的没注册上命令时明确提示可直接用 `<安装目录>/bin/qqops`。
- **新增测试** `test_installer_survives_a_non_termux_environment`:在"没有 PREFIX、HOME 指向临时目录"
  的环境里**真跑一遍安装器**,断言退出码 0、配置已生成、打印了"安装完成"。
  Windows 上跳过(那里用 `python -m qqops.svc`,不需要 bash)。这条测试在 ubuntu CI 与 Termux 上都会跑。
  - 这条测试第一版就把 CI 搞红了一次:它没清掉 `QQOPS_DATA`,子进程于是继承了**测试套件的共享数据目录**,
    安装器的 `init` 把配置写到那儿 —— 断言找不到配置,还污染了后面几个 webapp/采集器测试。
    现在显式清掉 `QQOPS_DATA`(与套件里其它起子进程的测试一致),并在断言里写明这个坑。
- 安装器输出小修:依赖检查里用 `awk` 取 `screen --version` 的版本号,在 Termux 上会把时间戳当成版本号
  打印成 `screen 02:12:35)`,现在只报"已安装"。
- 3.4.2 的文档改动已在真机上按**原样四行命令**验收:下载 375418 字节 → 解压顶层目录正确 →
  安装器跑完并打印控制台地址。

> 注:同一批改动曾以 v3.4.3 打过 tag,但那次 CI 因上面那条测试的隔离问题失败、**没有产出 Release**;
> 修好后作为 3.4.4 发布,孤立的 v3.4.3 tag 已删除。

## [3.4.2] — 2026-10-11

**安装教程改成"整段复制粘贴"。** 以前命令里带 `<版本号>`
(如 `tar xzf qqops-<版本>.tar.gz -C ~ && bash ~/qqops-<版本>/install.sh`):每升一次级都得改命令,
对新手也不友好。现在:

- **发布包多一个固定名字**:每次发布除了 `qqops-3.4.2.tar.gz`, 还产出 `qqops-latest.tar.gz`,
  于是文档里的下载网址可以**写死**:
  `https://github.com/mayuxiao2021/qqops/releases/latest/download/qqops-latest.tar.gz` ——
  永远指向最新版, 以后升级也是同一行;`SHA256SUMS.txt` 同时覆盖两个包。
- **解压不再需要版本号**:用 `--strip-components=1` 解到固定的 `~/qqops-src/`
  (Termux 的 GNU tar 1.35、Windows 自带 bsdtar 3.8 都支持, 两边都在真机上验过),
  于是"装 + 跑"四行命令全程不用改一个字, 重跑也安全(先 `rm -rf` 再解)。
- **控制台地址不再让人自己拼**:`python -m qqops.tools init` 与安装器结束语都直接打印
  `http://127.0.0.1:8091/?token=…`, 文档里不再出现 `?token=<令牌>`;随时 `qqops url` 可以再看一次。
- **NapCat 接线示例给具体值**:`token` 用 `change-me-3000` 并说明"自己定一串、两边填一致";
  找不到配置文件时给出 `ls ~/*/opt/QQ/…/config/onebot11_*.json` 这条命令。
- **Linux 的 systemd 自启**写成一条可复制的 heredoc(`$USER`/`$HOME` 由 shell 展开, 不用手改);
  **Windows** 补上完全不需要 bash 的流程(下载 → 解压 → `python -m qqops.tools init` → `svc start`)。
- 新增两条测试:安装类文档里不得出现"要人替换"的占位符或带版本号的解压命令;
  发布流程必须产出固定名字的包(否则文档里写死的网址会 404)。测试 179 → 181 项。

## [3.4.1] — 2026-10-11

**发布流程收尾**:3.4.0 打 tag 后暴露的问题, 一个个修掉。

- **文档站工作流不再挂红。** 启用 GitHub Pages 属于仓库管理设置, `GITHUB_TOKEN` 没有这个权限
  (`configure-pages` 的 `enablement` 报 `Resource not accessible by integration`), 于是整个 Docs 工作流失败。
  现在工作流**先探测 Pages 是否已启用**:已启用 → 照常发布;未启用 → **只构建不发布**
  (构建与死链检查照跑, 站点产物传成 `docs-site` 供离线查看), 并把"去哪里点一下"写进 job 摘要与
  warning, 不让仓库一直红着。
  一次性启用(仓库所有者, 约 30 秒):Settings → Pages → Build and deployment → Source 选 `GitHub Actions`。
- **发布说明取自 CHANGELOG。** `gh release create --generate-notes` 只给一句 compare 链接,
  对"能不能用起来"没有帮助。新增 `tools/changelog_notes.py`:Release 正文用 CHANGELOG 里该版本的段落,
  并附上发布包大小与 SHA256;取不到该版本才回退到自动 notes。顺带把发布步骤做成**可重跑**
  (同一个 tag 重推时更新既有 Release, 而不是失败在"Release 已存在")。
- **打包前的隐私门禁补上一个缺口。** 扫描一直用 `git ls-files`(只扫**已跟踪**文件),而 `pack.py`
  打的是**工作区** —— 一个还没 `git add` 的新文件会被装进发布包却躲过扫描。现在新增 `--tree`
  (已跟踪 + 未跟踪但未被 gitignore 的文件, 用 `git ls-files -co --exclude-standard`),
  `pack.py` 打包前改用它;被 gitignore 的开发草稿/运行时数据不扫(否则满屏假警报)。
  新增测试: 往工作区放一个未跟踪的假密钥文件, 扫描必须拦下(退出码 1)。
- `CONTRIBUTING.md` 新增「文档站」一节:本地预览/构建命令、死链策略为什么开着、Pages 的一次性启用。

## [3.4.0] — 2026-10-11

**文档站 + 自动发布**:用开源文档框架 VitePress 补全**使用文档**并发布到 GitHub Pages;
打 tag 自动创建 GitHub Release 并附上发布包与校验和。

### 新增:文档站 <https://mayuxiao2021.github.io/qqops/>
- `docs/.vitepress/`:VitePress 配置(中文、本地搜索、导航/侧边栏、**死链检查开启**);底座 `/qqops/`。
- 新写的**使用文档** `docs/guide/`(14 篇):快速开始、控制台、消息与检索、总结与报告、
  AI 智能查询、在 QQ 里直接问、提醒与待办、配置详解、命令行手册、保活、备份与升级、
  安全与隐私、排错、插件开发。
- 首页 `docs/index.md`:hero + 特性卡 + "你是哪种人 → 看哪篇"入口表;
  原有设计文档纳入导航(使用指南 / 部署 / 设计文档 / FAQ)。

### 新增:自动发布
- `.github/workflows/docs.yml`:构建文档站 → 发布 GitHub Pages(含 `enablement` 自动启用 Pages)。
- `.github/workflows/release.yml`:打 `v*` tag 自动跑**隐私扫描(含全历史)+ 全量测试 → 打包 →
  校验「包 == 工作区 == 已提交 == tag」→ SHA256 → 创建 Release 并附发布包**;
  用 runner 自带 `gh` 与 `GITHUB_TOKEN`, 无需额外凭据。

### 工程
- `package.json`/`package-lock.json`(仅文档站用;程序本体仍是零依赖纯标准库 Python);
- `.gitignore` 排除 `node_modules/` 与 `docs/.vitepress/{dist,cache}`;`pack.py` 排除 `.vitepress`;
- `verify_package.py` 关键文件加入文档站首页与使用文档;
- 测试 176 → **177 项**:新增「文档站导航不得有死链」。

## [3.3.12] — 2026-10-10

CI/工具链收尾(这几版都是 CI 一跑就暴露的问题, 逐个修掉):
- **3.3.7** 进程归属判定:占用者只要出现在"我们自己的候选集"(pid 文件 / 环境变量一致)里就算自己人,
  不再只比对命令行 —— macOS 的 `lsof` 只给命令名(`Python`), 原来必然判错;
  单进程模式下主管心跳检查改为**等到超时**(慢机器上会误判"没有心跳")。
- **3.3.8** 修一个真 bug:多进程/多线程同时**读改写 `state.json`** → Windows 上 `os.replace` 撞共享冲突
  (直接启动失败), 其它平台**丢更新**(心跳/计数忽然回退)。改用**心跳独立文件**(`data/run/beat_<服务>`)
  + `state_set` 跨进程文件锁 + `atomic_replace` 重试;runner 在线程启动后立刻写心跳。
- **3.3.9 / 3.3.10** 加 `QQOPS_DEBUG_START=<秒>`:启动卡死时自动 dump 全部线程栈(只 dump 不退出);
  测试的失败信息带**退出码 + 子进程输出 + 日志尾巴**, 并把诊断以 `::error::` 发成 CI annotation
  (job 日志要登录才能看, annotation 是公开的)。
- **3.3.11** 修**打包路径的隐私漏洞**:`pack.py` 把整个 `tools/` 打进发布包, 连**本机禁词表**
  (QQ 号/群号/令牌)一起带了出去 —— 真机测试当场拦下 12 条命中。现在排除私有文件, 并在打包后
  **再校验一遍包内容**(命中就删包报错)+ 新增发布包隐私测试;仓库侧一直干净。
- **3.3.12** CI 的打包校验任务取全历史与标签(`fetch-depth: 0` + `fetch-tags`),
  `verify_package.py` 的标签检查改为默认宽容(`--require-tag` 时严格)。
- 平台现状:Linux(Termux)与 Windows 全绿;macOS 代码已补 `ps`/`lsof` 兜底, 但 CI 上两个
  "独立启动 collector/webapp"的用例仍失败(子进程无输出, 原因未查明), 该 job 已标为实验性。

## [3.3.6] — 2026-10-10

接着修跨平台问题:上一版"读不到环境变量就不认领进程"虽然更安全,但**在 macOS 上把自己人也否掉了**
(回收不了僵死进程)。这一版给"是不是我们自己人"补上**全平台可靠的凭据**。

### 修复:进程归属以 pid 文件为主凭据
- 新增 `_pids_from_pidfiles()`:读 `data/run/<服务>.pid` —— 这是本机安装**自己写下的记录**,
  不需要任何权限(macOS 没有 `/proc/<pid>/environ`,环境变量那条路走不通);
- `_own_pids()` 现在 = pid 文件里的活进程 ∪ (pgrep 命中且环境变量一致的进程),
  因此 macOS 上也能正确回收自己的僵死进程, 同时仍不会误认别人的进程。

### 修复:细节与可测性
- `_ancestor_pids_ps()` 从 `_ancestor_pids()` 里抽出来(ps 兜底可单独测);
- `ci_annotate.py` 的失败详情改为取**异常行之后**的多行内容(之前被 unittest 的 `----` 分隔线
  截断, "子进程输出/日志尾巴"这类关键上下文看不到), 并容忍日志里的 BOM。

### 测试
- 归属相关用例改为**贴近现实**:给自己的僵死进程写 pid 文件(真实服务就会写),
  并按"能否定位到占用者"分支(桌面 Linux 走 `/proc/net/tcp`, macOS 走 `lsof`), 而不是按
  "有没有 /proc/net/tcp";
- 单进程端到端里四处裸 `assertTrue` 补上说明, 失败时能自证是哪一条;
- 新增断言:env 读不到时, **pid 文件仍能证明归属**;
- 174 项在本地(Windows/utf-8 与 cp1252)全绿。

## [3.3.5] — 2026-10-10

**修 CI 自己**:上一版把"失败整理"逻辑以 heredoc 内嵌在 workflow 的 `run:` 里,YAML 缩进一错,
bash 就把它当语法错误 → **exit 2,全平台一起挂**,而且看不到任何失败详情。

### 修复
- 抽出 `tools/ci_annotate.py`:解析冒烟测试日志 → 输出 `::error::` annotation(失败测试名 +
  堆栈行号 + 原因)并追加到 job summary。workflow 里只留一行调用,不再内嵌 heredoc。
- CI 失败信息现在**公开可读**(annotation 与 summary),不必登录下载日志 —— 上一轮 macOS 的问题
  就是靠这个才定位到的。

### 测试
- 173 → **174 项**:新增 `test_ci_annotate_extracts_failures`(工具本身也要有测试:
  它坏了会让"看不到失败原因"雪上加霜)。

## [3.3.4] — 2026-10-10

**跨平台修复**:CI 一跑, macOS 与 Linux/py3.10 上各暴露一批问题(本地 Windows + 中文编码全看不出来)。

### 修复:macOS / BSD 没有 `/proc` 时的能力缺失
- `proc_rss()` / `_rss_and_etime()` 加 `ps -o rss=` / `ps -o etime=` 兜底(macOS 上内存与运行时长
  不再是 0 / 空);
- `_cmdline()` 加 `ps -o command=` 兜底;`_ancestor_pids()` 加 `ps -o ppid=` 兜底(否则父进程集合为空);
- `port_owner()` 在没有 `/proc/net/tcp` 时退回 **`lsof -iTCP:<port> -sTCP:LISTEN`**(macOS 自带),
  端口占用者能定位到 pid 与命令;
- 新增 `_is_windows()` 把"平台判断"抽成可 mock 的 helper —— macOS 分支因此能在任何平台上被测试覆盖。

### 修复:进程归属判断的安全语义(顺带修 macOS 误判)
- `_own_pids()` 原先是"**读不到**对方 `QQOPS_DATA` 环境变量就当成自己人";macOS 上没有
  `/proc/<pid>/environ`,于是把别人的进程认成自己的 → 端口预检误报、还可能**误杀别人的进程**。
  现在改成:读不到环境变量就**不认领**(宁可报"占用者不是我们"),安全性优先。

### 修复:单进程健康检查的时序
- `_health_single()` 里 collector/web 的端口检查只查一次就下结论;冷启动慢的机器(CI 的 py3.10)
  会误判成不健康。现在和多进程分支一样**等到超时**再判定。

### 测试与 CI
- 172 → **173 项**:新增「非 Linux 兜底与归属安全」——mock `ps`/`lsof` 输出验证解析、
  并锁住"读不到环境变量不能认领进程"这条安全语义。
- 启动超时的失败信息现在自带**退出码 + 子进程输出 + 日志尾巴**(CI 上日志要登录才能看,
  失败信息必须自解释 —— macOS 那次就是靠它定位的)。
- CI 失败时把**失败测试名 + 堆栈行号 + 原因**发成 check annotation(公开可读),
  并在 job summary 里留一份摘要。

## [3.3.3] — 2026-10-10

**修一个只在"西文 Windows"上才暴露的 bug**(CI 的 `windows-latest` 上抓到):非 UTF-8 控制台下
打印中文会抛 `UnicodeEncodeError`,把服务启动日志、`qqops doctor`、测试输出一起打崩。

### 修复:非 UTF-8 控制台打印中文崩溃
- `core.ensure_utf8_console()`:把 `stdout/stderr` 切到 UTF-8 + `errors="replace"` ——
  能显示就正常显示, 显示不了只丢个别字符, **绝不让程序因为"打印"而死**。
- 七个进程入口(`collector/webapp/scheduler/watchdog/runner/svc/tools`)在 `main()` 第一行调用它;
  独立工具(`pack.py`、`tools/check_secrets.py`、`tools/verify_package.py`)自带同样实现
  (它们不 import qqops);测试进程同样先切。
- 测试里所有 `text=True` 的子进程显式 `encoding="utf-8", errors="replace"`,并给子进程环境加
  `PYTHONIOENCODING=utf-8`(子进程读写的管道也不能是 cp437)。
- CI 顶层加 `PYTHONIOENCODING: utf-8` 兜底。

### 测试
- 171 → **172 项**:新增「非 UTF-8 控制台打印中文不能崩」——断言标准流是 UTF-8,
  且**每个入口都调用了** `ensure_utf8_console()`(以后新增入口忘了调用会被挡住)。
- 复现方式(本地中文 Windows 看不出这个问题):`PYTHONIOENCODING=cp1252 python tests/smoke.py`
  —— 修前 `FAILED (failures=2, errors=1)`,修后全绿。

## [3.3.2] — 2026-10-10

**开源发布准备**: 文档体系、隐私门禁、CI 与工程规范。功能代码不变。

### 文档(对齐 AstrBot 等成熟开源仓库)
- `README.md` 重写:徽章、功能表、安装、界面、架构、文档索引、安全、开发;
  新增 `README_EN.md`(英文简版)、`SECURITY.md`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`。
- 新增 `docs/DEPLOY.md`(Termux/Android、Linux、Windows 的部署、NapCat 接线、保活、升级、排错)、
  `docs/FAQ.md`(隐私、成本、性能、卡片等常见问题)。
- `LICENSE` 由 MIT 改为 **AGPL-3.0**。

### 隐私与密钥门禁(硬性, 不靠自觉)
- 新增 `tools/check_secrets.py`:通用规则(私钥块、`sk-`/`ghp_`/`AKIA`/`xox*`/JWT/Telegram 令牌、
  硬编码的 password/token/api_key、个人绝对路径、非 noreply 邮箱、中国大陆手机号、超长十六进制串)
  **+ 本机禁词表** `tools/secret_denylist.txt`(已 gitignore, 永不入仓;格式见
  `tools/secret_denylist.example.txt`)。支持 `--staged`(提交前)、`--history`(全历史)、`--strict`。
- `pack.py` 打包前自动跑一遍扫描;新增 `.githooks/pre-commit` 与 `tools/install-hooks.sh`;
  CI 有独立的 `privacy` job 扫工作区与全历史。
- `.gitignore` 增补 `.env`/`*.pem`/`*.key`/禁词表;`config.example.json` 重新生成, 密钥一律留空。
- 测试新增「仓库不得含隐私/密钥」:既断言仓库干净, 也断言扫描器**确实能抓到**假密钥/个人路径/禁词
  (测试里的假值用拼接构造, 免得测试文件自己变成"泄漏")。

### 工程与 CI
- `.github/workflows/ci.yml`:隐私扫描 + 三平台 × 多 Python 版本测试 + 控制台探针 + 打包校验;
  另有 `ISSUE_TEMPLATE`(bug/feature/config)、`PULL_REQUEST_TEMPLATE.md`、`dependabot.yml`。
- `.editorconfig`、`.gitattributes`(强制 LF, 二进制与生成物标记)。
- 开发工具搬进仓库并去掉私有内容:`tools/console_probe.js`(桩 DOM 渲染 + 交互断言)、
  `tools/verify_package.py`(断言 包 == 工作区 == 已提交 == tag, 且包内权限位正确)。
- `pack.py` 清单纳入全部文档与工具;测试 169 → **171 项**。

## [3.3.1] — 2026-10-10

接上一版: 把 **Markdown 卡片**这条线摸到底并接进产品(它的真实身份是 **PacketBackend 的
protobuf 通道**, 不是"带签名的 JSON 卡片")。

### 新增:Markdown 卡片(可用则用, 不可用自动退文本)
- `NapCat.send_markdown(content, …, fallback_text=…)`: 发 OneBot 扩展段
  `{"type":"markdown","data":{"content":…}}`; 失败**一定**退回纯文本并把原始报错写日志。
- 回答形态 `reply.reply_mode` 与推送形态 `push.format` 新增 `markdown`
  (推送还新增 `forward` = 合并转发卡片: 各版本 QQ 都能渲染、内容任意)。
- 控制台「测试卡片渲染」新增 markdown 项, 一次看清当前设备能不能用。

### 查明:Markdown 卡片为什么在这台机器上发不出去(写进 docs/CHAT.md)
- 源码证据: `napcat-onebot/types/message.ts` 定义 markdown 段 →
  `napcat-core/packet/message/converter.ts:120` 转 `PacketMsgMarkDownElement` →
  `napcat-core/packet/message/element.ts:691` 组 protobuf `MarkdownData`。
  **即 markdown 与签名小程序卡片同源, 都依赖 PacketBackend**(官方支持矩阵: Linux arm64 仅覆盖
  QQ 构建号 28498–36580; 本机 QQ 3.2.32-52194 超范围)。
- 实测报错很阴: `Timeout: NTEvent … NodeIKernelMsgService/sendMsg … onMsgInfoListUpdate`,
  内核 `result: 0` 看着像成功, 但**消息实际没发出**(用"机器人自己发的消息有没有被上报"
  验证: 库里 `self=1` 没有这条)。`miniapp` 段则报 `消息体无法解析`。
- 因此当前可渲染的富文本仍是 **合并转发卡片** 与 **ArkSharePeer 官方分享卡片**;
  markdown 做成"能发就发、不能发就退文本"的开关, 换 QQ/NapCat 版本后自动生效。

### 测试
- 169 项保持全绿; 新增断言: markdown 段结构(带 `content`)、发送失败必退文本。

## [3.3.0] — 2026-10-10

三件事:**在 QQ 聊天里直接问记录**、**卡片消息的能力摸清并落地**、**私聊事件通路**。
新增 `docs/CHAT.md`(含卡片实测结论与"为什么现在发不出签名卡片")。

### 新增:在 QQ 里直接问(私聊默认开, 群聊要显式放行)
- 私聊机器人发一句话即可查记录, 答案回在聊天里; 群里必须带前缀(默认 `问`/`ai`/`/ai`/`?`)。
  实现为管线阶段 `assistant_reply`(order 45), 沿用阶段级 guard —— 一条消息处理失败只记账,
  不影响采集与其它插件。
- **安全默认**: 白名单默认 = 「推送」里的主人 QQ; 既没配白名单也没配主人时**一律不响应**,
  免得陌生人烧模型额度。群聊默认关闭(回答是公开的), 且必须同时满足"允许群问答 + 群号在白名单"。
- 每人/每群一个会话(`… 的 QQ 提问`), 在 QQ 里能接着追问; 与会话存储、每日额度、冷却共用一套;
  机器人自己的消息(`message_sent`)不回应, 免得自己跟自己聊。
- 回答形态可选: `text` 纯文本(可引用提问) / `forward` 合并转发卡片 / `card` 签名卡片+文本;
  附来源编号(可关), 失败时回一句原因(如额度用完)。
- 统计进 `/api/assistant/stats` 的 `reply` 字段(answered/skipped/failed/最后错误)。

### 新增:卡片消息(含"为什么发不出签名卡片"的实测结论)
- `NapCat` 客户端补齐: `send_message` / `send_text(reply_to=…)` / `send_forward(合并转发)` /
  `send_card(裸 JSON, 自动退文本)` / `miniapp_ark` + `send_miniapp_card(官方签名接口)` /
  `ark_share_peer` + `send_share_card(官方分享卡片)`;`core.forward_node()` / `core.qq_card()`
  / `core.cq_plain()`。
- **实测结论(写进文档, 免得反复踩)**: 裸 JSON 卡片(`miniapp_01`/`structmsg`)在现在的 QQ 上
  会显示**"卡片版本过低"**(客户端要求签名); 官方签名接口 `get_mini_app_ark` 依赖 PacketBackend,
  而官方支持矩阵里 Linux arm64 只覆盖 QQ 构建号 **28498–36580**, 本机 QQ 是 **52194** →
  `sendPacket 超时`。因此: **合并转发卡片**(各版本可渲染、内容任意)与 **ArkSharePeer 官方分享卡片**
  是当下可用的两条路, 签名卡片做成特性探测(能签名就用, 不能就退文本并记录原因)。
- 控制台「对话」页新增 **「测试卡片渲染」**: 一次把四种卡片发到你的 QQ 并列出成功/失败原因。

### 新增:私聊事件通路(聊天问答的前提)
- 采集器新配置 `command_private`(默认开): 私聊消息**不入库**(隐私), 但打上 `_not_stored`
  标记后交给插件 —— 否则"在 QQ 里问 AI"根本收不到消息。关掉它私聊提问即不再响应。
- 采集器把"落库后发事件"抽成 `_publish_message(ev, ptype, stored=…)`, 未入库的事件带标记,
  下游不会重复入库。

### 框架
- `framework/schema.py`: select 选项支持 `{"value":…, "label":…}`(校验按 value, 标签给人看);
  `flatten()` 统一输出该形状, 前端渲染兼容字符串与对象两种写法。

### 测试
- 166 → **169 项**: 聊天内问答全链路(白名单/非白名单/自己发的/冷却/群前缀/别群不理/转发形态/统计)、
  私聊"发事件但不入库 + 关闭开关后不发"、卡片与转发载荷(节点结构/裸卡片失败退文本/签名不可用的
  原因/官方分享 ark/CQ 码清洗)。

## [3.2.1] — 2026-10-10

两个用出来的 bug:**对话页发送按钮点了没反应**、**来源引用(尤其长期记忆)点开说取不到原文**。

### 修复:对话页发送按钮无效
- `VIEWS.chat` 的 `send()` 里写了 `const q = ...`,**遮蔽了全局 `q(root, sel)` 帮助函数**,
  于是函数内下一行 `q(v, '#chat-q')` 抛 `Cannot access 'q' before initialization` ——
  点「发送」和按 Enter 都在这里静默失败(异步函数里抛错, 页面看不出任何异常)。
  改用 `question` 变量名; 顺手把群组页同样遮蔽 `q` 的局部变量改名。
- **补上防回归的交互测试**:`_probe/console_probe.js` 现在会真的点一遍按钮 ——
  桩 DOM 支持按 标签/类/属性 找元素并触发 `onclick`, 断言"点了确实发出请求"。
  把遮蔽写法注入回去跑一遍, 探针报 `Cannot access 'q' before initialization`, 能抓住。

### 修复:来源引用取不到原文(长期记忆必然失败)
- **`[Y1]`(长期记忆)压根没有处理分支**:`api_source` 只认 `M/E/P/L`, 点记忆来源直接返回
  `未知来源类型: Y` → 前端 toast「取不到原文」。现在补上 `Y`, 并给出可核对的原文:
  记忆内容 + 范围/键/更新时间/命中次数 + **它是从该群哪些消息沉淀出来的**。
- **来源定位从"序号"改成稳定 `sid`**:原先前端把 `M1/Y1` 里的序号当数据库 id 去查,
  序号只在一份上下文里唯一 —— 点 `[M1]` 可能打开**编号恰好相同的另一条**消息(静默错),
  跨会话更是直接查不到。现在每条来源带 `sid`(`m:101` / `e:3` / `y:group:6666|digest` /
  `l:<url>` / `p:<key>`), 引用与来源芯片都按 sid 打开; 回答正文里的 `[M3]` 用该轮的
  `sources` 表还原成 sid 再请求。老会话(存库时还没有 sid)仍走原路径。
- 原文弹窗按类型给出可核对字段:消息(时间/群/昵称/正文/是否撤回/机器人标记)、
  事件(标题/类型/状态/截止/提出人/置信度)、链接(标题/域名/正文摘要/打开链接)、
  媒体(图片预览/大小/识别说明), 不再是裸 JSON。
- **「数据 → 记忆」页每条记忆加「看原文」按钮**, 长期记忆也有能点开的原始数据入口。

### 测试
- 165 → **166 项**:新增「来源必须能用稳定 sid 定位」——记忆 sid 可取原文、序号张冠李戴时
  以 sid 为准(必须返回被引用的那条)、事件/链接 sid 可用、无 sid 的历史路径如实报错不装死。
- 探针新增交互段(发送按钮、来源引用、真机记忆原文接口), 离线打桩与真机实时都跑。

## [3.2.0] — 2026-10-10

三件事:**四个服务进程合并成一个**、**加功耗档位(低功耗模式)**、**按需加载**。
真机实测:4 进程 104.6 MB → **1 进程 27.5 MB(省 74%)**,功能与自愈能力一条没少。

### 新增:单进程形态 `runner.py`(进程合并)
- `python -m qqops.runner`:主线程当**主管**,collector(`:8080`)/web(`:8091`)/scheduler 起线程,
  四个服务共享**一个解释器 + 一份 SQLite 缓存**;`runtime.mode` 新增 `single|multi|auto`,
  `auto` 在 Android 上自动选单进程(桌面仍是多进程,进程隔离更抗单点故障)。
- **自愈照旧**:主管每轮检查线程 `is_alive()`,不在就按 `Backoff`(10s→30s→90s)重启,
  反复崩 → `crash_loop` 熔断 + 崩溃账本;调度滴答与体检**同拍**跑(少一次唤醒源)。
- **心跳不说谎**:主管只给"真的活着"的线程写 `beat_<服务>`,线程死了心跳即过期,
  控制台立刻显示异常 —— 不会出现"进程在、调度早停了"。
- 停机只 SIGTERM 一个 pid,没有孤儿;多进程模式原有 `pgrep` + 端口判定保持不变。
- 实测(真机 Termux):多进程 4 进程 / **104.6 MB**,单进程 1 进程 / **27.5 MB**。

### 新增:功耗档位(低功耗模式)
- `runtime.power_mode = performance|balanced|saver`,展开为四个节拍 + 富化窗口 + SQLite 缓存:

  | 参数 | 性能 | 均衡(默认) | 省电 |
  |---|---|---|---|
  | 调度滴答 | 5s | 15s | 60s |
  | 主管体检 | 30s | 60s | 180s |
  | 控制台刷新 | 10s | 15s | 60s |
  | 富化(抓媒体/链接) | 立即 | 立即 | 只跑 `02:00-06:00` |
  | SQLite 缓存 | 8000KB | 4000KB | 1000KB |
  | 闲聊日志 | 关 | 关 | 开(少写盘) |

- 档位是**节拍**不是功能开关:消息入库始终事件驱动(秒级),变慢的只是"多久看一眼要不要做
  定时总结/体检"和非紧急富化;省电档实测四服务健康、定时任务、端到端均正常。
- 命令行 `qqops power performance|balanced|saver`(别名 `qqops lowpower`)、`qqops mode single|multi|auto`;
  切换即时生效(自动重启实例并等端口释放)。

### 新增:按需加载(懒加载)
- `webapp.py` 不再顶层导入 summary/render/media/linkfetch,改成调用点内导入 —— 控制台进程
  启动只加载 HTTP + 路由;`scheduler.py` 的 summarizer 同理(没配模型就从不加载总结链路)。
- `Store.conn()` 按档位设 `PRAGMA cache_size`,省电档不再让 SQLite 在内存里留大页缓存。

### 新增:控制台「运维 → 资源」页
- `GET /api/ops/resources`:运行形态、自身 RSS、当前档位与生效参数、进程列表与合计、
  **合并效果估算**(合并前后进程数/内存对比)。
- `POST /api/ops/power`:一键切形态/档位;后端**脱离请求进程**重启实例,页面提示刷新即可回显。

### 修复
- **单进程模式下调度被误判"心跳超时"**:`runner` 写的是 `beat_scheduler`,
  而健康检查读的是多进程模块写的 `scheduler_beat` —— 新增 `core.last_beat(name)`
  统一读新老两个键取较新,`scheduler.py`/`watchdog.py` 改走 `core.beat()`;
  多进程模式的调度心跳窗口也按档位放大(省电档 60s 一跳不再擦边误判)。
- 健康检查在单进程分支下不再依赖 `pgrep`(四个服务同一个 pid,`pgrep` 区分不出来)。
- **控制台"数据没拿到就留白/崩"**:消息/待办/群组/任务/报告/日志/配置/概览/运维-服务
  九个页面在接口返回空(令牌过期 401、服务刚重启、接口报错)时会返回空页面,
  消息页更是直接抛 `Cannot read properties of null`。现在统一走 `failCard()`
  → 复用 `errState` 错误卡片 + 一键重试 + 常见原因提示,**失败只降级不白屏**。
  用真机接口实测三种情形(离线打桩 / 带令牌 / 故意不带令牌)全部通过。

### 文档与测试
- 新增 `docs/POWER.md`(进程合并 / 档位 / 懒加载 / 实测数字 / 已知边界);
  `docs/ARCHITECTURE.md` 增 5.6 节(两种形态的自愈与心跳对照)、层级图补 `runner.py`;
  `docs/CONSOLE.md` 增 3.11 节(资源页规格)。
- 测试 154 → **165 项**:运行形态与档位 9 项(档位参数差异/覆盖与回退/`resolve_mode`/
  富化窗口/SQLite 缓存/实例状态/懒加载/主管重启决策/单进程端到端)。
- PC 与真机各跑一遍全绿;真机单进程 + 省电档下 `qqops doctor` 全部通过。

## [3.1.0] — 2026-10-10

两件事:**给控制台加上"用一句话问记录"的 AI 智能查询**,以及**按 OneBot v11 逐条审查消息接收处理**。

### 新增:AI 智能查询(对齐 AstrBot 的 `core/provider`)
- `framework/llm.py`:**LLM 供应商抽象** —— 统一超时/重试/用量/成本统计/失败记账/密钥脱敏;
  `llm` 配置段留空时自动回落到 `summarizer`(base_url/api_key/model 只维护一份);
  失败**不抛异常**, 返回 `ok=False` 让调用方降级。
- `plugins/assistant`(**智能查询插件**):
  * 中文提问**先分词再检索**(剪虚词 + 2 字滑窗, 不需要分词库):"昨天测试群里谁提到了报告"
    → 时间范围 48h + 群 6666 + 关键词 [测试/试群/提到/报告];
  * 检索失败会**逐词检索再合并**(按命中数排序), 命中不足才退回"范围内最近消息";
  * 把消息/待办/记忆/链接/媒体排成**编号上下文**(`[M1] 10-10 12:03 群/昵称: 内容`),
    要求模型**只依据记录回答**且每条结论标注来源编号;
  * 模型可以要求**再检索一轮**(最多 `max_rounds`), 但绝不允许把那行 JSON 当回答;
  * **会话与轮次落库**(`chat_sessions`/`chat_turns`):支持历史回看、token/成本统计、每日额度;
  * **失败降级**:模型不可用时直接列出最相关的记录并说明原因 —— 不留白页。
- 控制台新增**「对话」页**(一级导航第 2 项, 手机底部标签栏也有):
  会话列表 + 气泡对话 + **内联来源编号可点开原文** + 来源芯片(💬📌🧠🔗🖼)+
  模型/token/轮次/耗时/降级标记 + 时间范围过滤 + **从真实记录生成的推荐问题**(不花额度)。
- 新增接口: `/api/assistant/{ask,sessions,session,delete,suggest,stats,source/<kind>/<ref>}`,
  另有命令 `assistant.ask`(命令行/控制台直问)。

### 新增:消息接收处理规范(docs/INGEST.md)
按 OneBot v11 逐条审计 23 项, 修掉下列缺口(每条都有测试):
- **请求体上限 8MB**:超限直接 413 且**不读请求体**(原先无上限, 畸形上报可能吃光内存);
- **`post_type=message_sent`**(机器人自己发的消息)现在也落库并标记 `self=1` —— 对话上下文需要;
- **`post_type=notice`**:`group_recall` 标记消息 `recalled=1` 并发 `message.recalled` 事件;
  其余通知记 DEBUG(原先整体忽略);
- **`post_type=meta_event`**:心跳写 `state.napcat_beat`/`napcat_status`, 连接状态变化记日志
  —— 这是"NapCat 到底连没连上"唯一可靠信号(原先忽略);
- **消息去重**:(`group_id`,`message_id`)唯一索引 + `INSERT OR IGNORE`, 断线重连补发不再重复入库;
  建索引前自动清理历史重复;计数新增 `duplicate`;
- **空消息**(无 `raw_message` 也无 `message`)不入库, 计入 `skipped`;
- **时钟回拨保护**:事件时间落在未来 >1 天时改用本地时间(否则时间线错乱、总结范围错);
- **类型归一化**:`mface/marketface→face`、`node→forward`、`flash→image`(原先只映射 `record`);
- **忙锁重试**:`busy_timeout=5000` + 落库遇忙锁退避重试 3 次;
- 落库失败写入**崩溃账本**;未知 `post_type` 计数 + WARN(原先静默吞掉);
- 采集计数新增 `duplicate/notice/meta/sent` 四项, 控制台可见。

### 控制台
- 消息页显示「已撤回」「机器人」标记;`/api/messages` 支持 `exclude_recalled`、`include_self`、`order`;
- 导航调整为 10 页(新增「对话」), 文档 `docs/CONSOLE.md` 同步更新。

### 测试
- 135 → **154 项**:接收规范 8 项(去重/空消息/self/撤回/心跳/畸形/裸 socket 分块/类型归一)、
  智能查询 11 项(分词与范围解析/检索与编号/带来源作答与会话落库/模型不可用降级/无记录如实回答/
  第二轮补检索/每日额度/关闭开关/接口契约/LLM 配置回落/LLM 失败不抛)、存储 1 项(对话表与去重索引)。

## [3.0.0] — 2026-10-10

架构升级:引入**事件总线 + 阶段化管线 + 插件体系 + 容错内核 + 可扩展控制台**,
结构上对齐 AstrBot,运行形态仍是"纯标准库、手机可跑、多进程或单进程皆可"。
规划与设计见 `docs/ARCHITECTURE.md`、`docs/CONSOLE.md`。

### 新增:框架层 `qqops/framework/`(对齐 AstrBot 的 core/)
- `event.py` / `bus.py`:事件模型 + **线程安全事件总线**(有界队列、工作线程、队列满如实记账);
  每个事件处理器独立隔离 —— **一个插件抛异常, 其它插件照常收到事件**。
- `pipeline.py`:**阶段化管线**(10 过滤 → 20 落库 → 30 归档 → 40 富化 → 50 总结 → 60 推送),
  阶段可插拔/可开关/可短路;`critical` 阶段失败会丢弃事件并记账,普通阶段失败只记账;
  每阶段耗时写入 `Event.meta["stages"]`,可还原"这条消息卡在哪一步"。
- `plugin.py` / `manager.py` / `registry.py`:**插件体系** —— `@register` + `Plugin.initialize/terminate`
  + `@on_event/@on_stage/@task/@command/@route/@page`;支持发现、按角色注册、启用停用、**热重载**;
  坏插件只影响它自己(导入/初始化失败都记账并发事件)。
- `schema.py`:**配置 schema 即默认值**(与 AstrBot `_conf_schema.json` 同构),
  类型转换/枚举/范围校验,**非法值回退默认值并给出人话提示**,同时供控制台自动生成表单。
- `errors.py`:**容错内核** —— 崩溃账本(`data/crashes.jsonl`,去重限流+脱敏)、
  `@guard` 装饰器/上下文管理器、指数退避 `retry`、**退避+熔断 `Backoff`**、
  `install_global_hooks()`(主线程/子线程未捕获异常也进账本)、密钥脱敏 `redact`。
- `log.py`:**日志广播** —— 内存环形缓冲 + 游标增量拉取, 控制台"运维→实时日志"据此实现;
  `core.log()` 自动双写(文件 + 广播),老代码零改动。
- `context.py` / `web.py` / `api.py`:插件上下文(配置/存储/状态/事件/HTTP/发消息)、
  路由注册表(插件与框架页面共用一个表)、对插件稳定的公开面 `qqops.framework.api`。
- `app.py`:`QQOpsApp` 装配总线/管线/插件/上下文,按角色运行(`ingest` 采集 / `tasks` 调度 / `console` 控制台)。

### 新增:内置插件 `qqops/plugins/`(等价 AstrBot 的 builtin_stars)
- **截止提醒(reminders)**:从群消息里识别"下周三交报告"这类带时间点的待办(复用 `parse_due`),
  到点提醒;用"append-only 收件箱 + 单一写者状态文件"避免多进程写冲突;
  同时是插件 API 的完整示例(阶段 + 任务 + 命令 + 接口 + 页面 + schema 配置)。

### 新增:控制台 v3(`qqops/console/` + 前端重构)
- **9 个一级页面**:仪表盘 / 消息 / 总结 / 任务 / 插件 / 数据 / 运维 / 设置 / 帮助;手机底部标签栏。
- **仪表盘**:整体健康、服务卡、24 小时趋势、下次总结、最近报告、异常汇总、快捷操作(立即总结/重载插件)。
- **插件页**:卡片列表(版本/作者/状态/处理器数)、启用停用、**schema 驱动的配置表单**(含密钥掩码)、
  重载、插件命令与定时任务一键执行、失败插件红色显示原因。
- **运维页签**:服务控制 / **异常账本**(按组件与异常聚合、看堆栈、清空)/ **实时日志**(模块+级别过滤、暂停、自动滚动)/
  **一键自检**(环境·数据库·插件·账本)/ 备份与维护。
- **数据页签**:群组开关 / 事件待办 / 记忆 / 链接 / 媒体网格。
- **易用性**:加载骨架、空态、错误态与"重试"、操作 toast、危险操作二次确认、
  请求级 `request-id`(报错可对号入座)、手机端优先(底部标签栏 + 44px 触控目标 + 无横向滚动)。

### 加固(出现 bug / 崩溃时的行为)
- **控制台每个请求独立守卫**:异常返回 500 JSON + `request-id` 并记账, 单个接口异常不再白屏或影响其它卡片;
  超过 2 秒的请求记慢请求日志。
- **采集器**:落库失败记账;端口绑定失败明确报错并退出(不再"看起来在跑");落库后把事件交给总线。
- **调度器**:总结失败/备份失败/循环异常全部记账;每次循环跑插件的到点任务。
- **看门狗**:按 10s→30s→90s… **退避重启**;窗口内崩溃达阈值则**熔断**停止自动重启并告警(避免"崩-拉-崩"雪崩)。
- **数据层**:`busy_timeout=5000`,落库遇忙锁**退避重试 3 次**,新增 `check_integrity()`(接入自检页);
  配置读取容忍 BOM(`utf-8-sig`)并保留损坏现场。
- **打包**:`pack.py` 清单加入 `docs/`,包内权限位依旧显式写入。
- **数据目录不再依赖 `$HOME`**:改为按代码位置解析(`QQOPS_HOME` > 仓库根目录 > `~/qqops`),
  `bin/qqops` 同时固化 `QQOPS_HOME` —— 从任意目录(甚至 cron)调用都指向同一份数据。
  (真机验证时踩到: 不 cd 直接调用 CLI, `$HOME` 不同 → 在别处另建了一套空数据 → 控制台显示"消息 0 条"。)

### 测试
- 110 → **135 项**:框架(总线隔离/管线短路与计时/schema 校验与表单/路由匹配/账本去重脱敏/守卫/重试/退避熔断/日志游标)、
  插件体系(加载·隔离坏插件·启停·热重载·配置校验与保存·角色过滤·任务与命令·路由与页面)、
  真机接口契约(框架状态/插件/配置/日志流/账本/自检/插件命令/**故意报错必须 500 且进账本**/停用插件路由即失效)、
  数据库 WAL 与完整性自检。

## [2.2.1] — 2026-10-10

定位到 2.2.0 之后手机上一手复现的真凶:**上一份僵死进程仍占着 :8080**,
新 collector 一启动就 `OSError: [Errno 98] Address already in use` 秒退, 而界面还会显示"已启动"。

### 修复
- **端口占用预检 + 自动回收**(`core.check_port_free` / `core.port_owner` / `core._reclaim_port`):
  启动前先看端口是否已被占:
  * 占用者是**我们自己的同一个模块** → 判定为僵死进程, 先 TERM 再 KILL 回收, 再启动(消息里注明"已回收");
  * 占用者是**别的程序** → 直接拒绝并报出 `pid + 命令行`, 不再拉起一个必然秒退的进程。
- **Android 现实约束下的占用者识别**: 手机上 `/proc/net/tcp` 是 Permission denied
  (`lsof`/`fuser`/`ss`/`netstat` 读的也是它, 同样不可用), 因此增加不依赖端口反查的判据
  `_own_pids()`: 命令行命中 `-m <模块>` **且** `QQOPS_DATA` 与本实例一致 → 认定为自己人。
  这也是"在真机上跑测试不会把生产实例当僵死进程杀掉"的防线。
- `stop_service` 增加**端口残留回收**: 进程杀完后端口仍被自己模块占着就一并清掉。
- `start_service` 认领已在运行的实例时**补写 PID 文件**, 否则那个进程之后 `qqops stop` 停不掉。
- **启动竞态**: 手机解释器启动/退出都慢, `pgrep` 命中时进程可能"正在退出",
  于是"秒退"被误报成"起来了但端口没应答" → 新增 `_exit_code_soon()` 二次确认退出码,
  现在这类情况一律如实报"启动失败(进程已退出, 退出码 N)"。
- **看门狗首轮心跳**: 原来首轮体检跑完才写心跳, 启动瞬间 `service_health` 判成"没心跳",
  `qqops start` 会报假的"watchdog 启动异常" → 现在**启动即落一次心跳**, 且健康检查会等到超时。
- `_pid_alive_and_matches` 先 `kill(pid, 0)` 确认存活再比对 cmdline。
- **doctor 保活段不再瞎报**: Termux 无权读 `settings`/`dumpsys`, 原来把"读不到"写成
  `[!!] 未加入白名单`, 现在如实标 `[??] 本机测不到` 并给出 PC 端 adb 核验命令;
  唤醒锁也不再无条件 `[OK]`(装了 `termux-api` 脚本≠装了 Termux:API 应用)。
- **打包不再丢执行位**: Windows 没有 exec 位, 用 `tar` 打的包解出来 `bin/qqops` 是 0644,
  直接执行会 Permission denied → 新增跨平台打包器 `pack.py`(显式写入 0755/0644),
  `pack.sh` 改为调用它; `install.sh` 里的 `chmod +x` 仍在, 双保险。

### 测试
- 109 项(新增 6 项): 端口冲突可发现且能识别"自己人"、外部占用必须拒绝启动、
  自己的僵死进程必须被自动回收、另一套实例(不同 QQOPS_DATA)绝不能被回收、
  秒退进程必须能确认退出码、看门狗启动即可健康、打包器权限位与清单完整性。

### 验证记录
- PC: 109 项通过(7 项按平台跳过)。
- 真机(Termux, Android 17 / Python 3.14.6): 108 项通过、0 跳过;
  collector 8080 / web 8091 端口应答, scheduler+watchdog 心跳正常;
  模拟 NapCat 上报 → 落库 → 清理 全链路通过; NapCat 上线(`user_id 1127487015`)。

## [2.2.0] — 2026-10-10

针对「进程老是被杀 / 启动不完全」的两类问题。

### 新增
- **看门狗**(`qqops/watchdog.py`, 随服务一起启动, 默认每 60 秒体检):
  进程不在 → 立刻拉起; 进程在但**端口不响应 / 心跳停了**(假活) → 先清掉再拉起;
  心跳写入 `state.json`, 控制台「运维」可见; `qqops watchdog` 可手动体检一轮; 新增配置段 `watchdog`。
- **开机自启**: `install.sh` 写入 `~/.termux/boot/qqops.sh`(装 Termux:Boot 后生效), 并获取唤醒锁。
- **设备级保活脚本** `tools/android-keepalive.sh`(在电脑上跑): 关闭幽灵进程查杀、加电池优化白名单、
  放开后台权限、待机分桶设为 active。
- `doctor` 新增「手机保活」体检段: 逐项检查并打印该执行的 adb 命令。
- `qqops status` 用 `●` 正常 / `◐` 进程在但没响应 / `○` 未运行 三态显示, 并附健康原因。

### 修复
- **启动判定不可靠(即"启动不完全")**: `start_service` 原来只要 `pgrep` 到进程就报"已启动",
  进程随后崩掉也算成功 → 现在必须**端口真的应答 / 心跳真的刷新**才算成功; 进程秒退会附上日志尾部。
- **`pgrep` 误判**: 当 `pgrep` 命令不存在时, 把"(执行失败: ...)"这行当成了"匹配到了进程",
  于是所有"是否运行"的判断都变成假真 → 现在只认真正的 PID 行。
- **PID 文件可能杀错进程**: 手机重启后 PID 会被复用, 旧的 `.pid` 可能指向别人的进程 →
  kill 之前先校验 `/proc/<pid>/cmdline` 是否是我们的服务。
- `qqops stop/restart` 现在**先停看门狗**再停其它服务(否则看门狗会把刚停掉的服务又拉起来),
  启动时最后起看门狗。

### 测试
- 99 项(新增 5 项): 进程秒退必须报失败、PID 校验、看门狗体检结构、看门狗 CLI、未知服务健康检查。

## [2.1.0] — 2026-10-10

### 新增
- **灵活的总结时间**(不再只能按天), 三种模式, 在控制台「任务」页顶部直接改:
  - `每天定点` — 可填多个时间点(如 `09:30, 22:00`);
  - `每隔一段时间` — 例如每 360 分钟(6 小时)一次;
  - `每周几天` — 周一~周日任选 + 当天时刻。
  面板同时显示**下次运行时间与倒计时**、上次运行时间、距上次新增了多少条消息。
- **无新消息就跳过**: `skip_if_no_new` + `min_new_messages` —— 自上次运行以来新消息不足就跳过,
  不产生空报告、不花模型钱;跳过会如实记一条"已跳过"的任务记录。
- 新增 `GET/POST /api/schedule`(带校验: 时间格式、间隔范围、星期取值), 以及状态快照里的
  调度模式/标签/上次运行/新增消息数。

### 改进(配置去重)
- `schedule.hours` 与 `summarizer.window_hours` 是同一个"时间窗" → **合并为后者**,
  启动时自动迁移并写日志(`Config.migrate()`);旧版本的遗留 `schedule.*` 键一并清理。
- 配置页隐藏重复入口, 只保留唯一维护点并给出提示:
  - 「总结哪些群」只看**群组页**勾选(实时群列表), 不再重复显示 `summarizer.groups`;
  - 定时细节只在**任务页**改, 配置页的「定时」只留总开关;
  - 采集过滤(入库)与总结过滤(分析)的中文名区分开, 并说明二者关系;
  - 补料的子项标签按分组区分(图片: 单张最大字节 / 链接: 单个网页最大字节 / 文件: 单个文件上限)。

### 修复
- `next_run` 的"每隔"模式: 已超过间隔时返回"再等一个周期", 与调度器"立刻执行"的判定不一致 →
  现在返回**立即到期**(倒计时 0)。

### 测试
- 94 项(新增 10 项): 三种模式的下次时间计算、超时滚动、每周只选某天、配置去重迁移、
  未知键清理、调度设置校验(非法模式/时间/间隔/空星期)、新消息计数。

## [2.0.1] — 2026-10-10

### 改进
- **界面全面中文化**(专有名词 QQ / NapCat / AstrBot 保留):
  - 服务名: collector/scheduler/web/napcat → **采集器 / 调度器 / 控制台 / NapCat**;
  - 服务详情: PID/RSS/etime → **进程号 / 内存 / 已运行**;端口写作"端口 8080";
  - 消息类型: text/image/reply/at/face/json/file → **文本 / 图片 / 回复 / @某人 / 表情 / 卡片 / 文件**,
    并新增语音/转发(筛选下拉与列表标签同时中文);
  - 任务: `token` 列 → **用量(输入/输出)**;`manual`/`schedule:22:00` → **手动 / 定时 22:00**;
    `ok`/`partial`/`failed` → **成功 / 部分成功 / 失败**;
  - `dry-run` → **试运行(不花钱)**;`导出 XLSX/DOCX` → **导出表格(Excel) / 导出文档(Word)**;
    `FTS5 / LIKE` → **全文索引 / 模糊匹配**;采集指标由英文 JSON 改为中文四项;
  - 日志页标签中文(浏览器上报/采集器/核心/旁路服务/调度器/总结器/控制台),并显示对应文件名;
  - **配置页**: 每个配置项都有中文名 + 悬停说明(如 `window_hours` → "时间窗(小时)"),
    嵌套的补料设置(图片识别/文件解析/链接抓取)有中文分组标题;登录页说明改为中文。
- `Config.load()` 现在会把配置里的 `version` 同步成当前版本(此前控制台会显示建库时的旧版本号)。

## [2.0.0] — 2026-10-10

从"每日群聊总结"升级为**个人计划与信息整理**系统。

### 新增
- **结构化抽取**(`extract.py`): 模型改为输出 JSON(概览/话题/事件/待办/链接/事实/记忆更新),
  经 `parse_json` 容错解析 + `normalize` 逐字段裁剪(数量上限、类型白名单、日期归一、置信度过滤)。
- **事件与待办长期留存**(`events` 表 + 控制台「计划」页):
  - 带**去重合并**(同 kind+标题+群 → 合并, 保留更重要的优先级、更高的置信度、更长的详情);
  - 状态流转(进行中/已完成/已忽略)、截止日、优先级、来源消息、首次/最近出现;
  - 控制台可勾完成、忽略、编辑、删除、手工新增, 支持按状态/类型/群/关键词/仅 7 天内筛选。
- **记忆提炼**(`memories` 表): 每群滚动维护"当前进展/进行中的事/已知事实", 下次总结自动带上,
  跨天上下文连续; 控制台可直接编辑或删除。
- **个人档案**(`data/profile.md`): 我关注的群、长期目标、输出偏好 —— 每次抽取都会作为背景注入,
  用于判断"与我相关"。
- **报告输出**(`render.py`): Markdown + **自包含 HTML 报告页**(`/r/<name>` 可直接打开, 也可从推送里点开)
  + **XLSX/DOCX 导出**(标准库手写 OOXML, 零依赖); 推送改为"与我有关"的摘要(有截止的、重要事件、待办、链接)。
- **补料 enrich**(`media.py` / `fileread.py` / `linkfetch.py`):
  - **图片识别**: 走多模态接口(实测该 DeepSeek 端点支持 `image_url`), 图片不再是 `[图片]` 占位符;
    按 URL 缓存, 同一张图只识别一次;
  - **文件解析**: txt/md/csv/json/代码 + docx/xlsx/pptx(zip+XML); PDF 走可选 `pypdf`;
  - **链接正文**: 只抓文章类链接, 带 **SSRF 防护**(禁本机/内网/非 http(s), DNS 解析后再校验真实 IP,
    限制重定向次数、响应体大小与超时)。
- 采集器落库时保存原始消息段, 供补料取媒体直链; NapCat `get_image`/`get_group_file_url` 作为兜底。

### 修复(由新增的 80 项测试抓出)
- `event_upsert` 里 `now = now()` 遮蔽模块函数 → `UnboundLocalError`。
- 事件合并优先级用了 `max`, 但 1=最重要 → 应取 `min`(否则重要事件会被降级)。
- `parse_due`: 中文日期预处理把 `月` 换成 `-`, 导致 **`月底` 永远匹配不上**; 日期类截止统一为当天 23:59。
- `linkfetch` 的 HTML 解析把 `head` 列入跳过标签 → **`<title>` 永远取不到**。
- `media.safe_name` 把中文文件名全部替换成 `_` → 现在保留 Unicode。

### 测试
- 由 49 项扩展到 **80 项**: 新增结构化抽取容错、日期解析、OOXML 读写往返(自己产的 xlsx/docx
  反过来解析)、SSRF 黑名单、HTML 正文与标题、文件类型识别、事件去重合并与状态流转、记忆与链接与媒体存储。

## [1.1.0] — 2026-10-09

### 新增
- **全文检索(FTS5)**: 消息页支持关键词检索, 中文按字匹配、拉丁按前缀匹配, 命中词高亮,
  并显示当前检索方式(FTS5 / LIKE / 按时间); 运维页新增「重建全文索引」与 `POST /api/reindex`。
  中文需逐字索引(unicode61 会把整串中文当一个 token), 故索引侧插入空格、查询侧用短语。
- **旁路服务管理**: NapCat / AstrBot 可在控制台「运维」页启停, 也可用 CLI:
  `qqops extras`、`qqops napcat {start|stop|restart}`、`qqops astrbot {start|stop|restart}`;
  `POST /api/service` 同样支持这两个名字。AstrBot 停止时会清理 `astrbot.lock`。
- 状态快照新增 `extras` / `fts` 字段; `qqops status` 显示旁路服务与索引状态。

### 修复
- **collector**: `signal.SIGHUP` 在 Windows 上不存在, 直接引用会让进程**启动即崩**(改为按平台取信号)。
- **webapp**: 未鉴权访问 `/api/*` 现在返回 **401 JSON**(此前 302 到登录页, 客户端会把登录页 HTML 当 JSON 解析);
  页面路径仍 302 跳登录。
- **FTS**: 群改名时同步更新索引内的群名副本, 否则按群名会搜到旧名。

### 测试
- 测试由 24 项扩展到 **47 项**: 新增中文/拉丁检索、改名同步、索引重建、旁路服务规格、
  版本号一致性、**纯标准库校验**(解析 AST 断言无第三方依赖)、以及**真实子进程集成测试**
  (采集器收消息落库 + SIGTERM 必须能退出; 控制台 HTTP 契约: 鉴权、302/401、HEAD、缓存头唯一)。
  上述前两个 bug 正是被这套集成测试抓出来的。

## [1.0.1] — 2026-10-09

修复「页面渲染不全 / 状态不全」。

### 修复
- **浏览器缓存旧页面**: HTML 与 JSON 接口原来没有任何 `Cache-Control`, 浏览器会启发式缓存,
  换版本后仍显示旧页面(表现为"渲染不全")。现在 HTML/接口一律 `Cache-Control: no-store`,
  静态资源仍走版本化 URL(`?v=<version>`)。
- **状态不全**: 概览页只有「运行中/已停止」。
  - 服务状态现在带 **PID / 常驻内存 / 运行时长 / 端口**;
  - 新增 **生态端口探测**: NapCat WebUI(6099)、反向 WS(6199)、AstrBot(6185)、存档仪表盘(8090)、NapCat API;
  - 新增 **下次总结时间**(由 `schedule.times` 计算)与剩余分钟数;
  - 新增存储面板: 库大小 / 数据目录 / 报告数 / 备份数 / 磁盘剩余 / 启用群数 / 采集计数器;
  - 顶部新增**常驻状态条**(采集·调度·控制台·NapCat 四灯 + 近24h 条数 + 启用群 + 下次总结),
    每 10 秒独立刷新, 任何页面都能看到。

### 新增
- `GET /api/status`: 轻量完整状态快照(服务/生态端口/采集/调度/存储/消息), 供状态条与自动化使用。
- `core.status_snapshot()` / `core.proc_info()` / `core.next_run_time()` / `core.ecosystem_ports()`。

## [1.0.0] — 2026-10-09

首个可部署版本。把此前散落的脚本(listen.py / summarize.py / panel.py / _sched.py)
重构为一个有安装器、控制台、CLI 与测试的完整程序。

### 新增
- **安装器** `install.sh`:依赖检查、程序复制、配置与令牌生成、旧数据迁移、命令注册、
  服务启动、自动自检;支持 `--no-start` / `--import-db` / `--import-config`。
- **Web Console**(`qqops/webapp.py` + `static/`):SPA 外壳 + JSON API,暗色主题、响应式、
  自动刷新;八个页面(概览/消息/群组/任务/报告/日志/配置/运维)。
- **群组页**:群列表改为调用 NapCat `get_group_list` 的**实时数据**(含从未发言的群),
  支持搜索、单个/批量启用总结。
- **任务页**:每次运行入库(`runs` 表),记录状态、耗时、token 与费用。
- **运维页**:服务启停、立即备份、CSV 导出、NapCat 信息与发送测试、采集指标。
- **CLI** `bin/qqops`:start/stop/restart/status/url/logs/summary/backup/prune/doctor。
- **Scheduler**:多时间点、当天去重、配置热加载、每日自动备份与保留策略清理、心跳。
- **Tools**:`init / doctor / import / backup / prune`。
- **测试** `tests/smoke.py`:24 项冒烟测试(配置/存储/总结/过滤/会话),不依赖网络。
- **打包** `pack.sh` → `dist/qqops-<version>.tar.gz`。

### 工程规范
- 配置:默认值深合并、校验、密钥掩码、损坏自动留证;**原子写**(临时文件 + fsync + rename)。
- 存储:WAL 模式、索引、`runs`/`meta` 表;导出 CSV 带 BOM(Excel 直接打开不乱码)。
- 日志:分级 + 按大小滚动;采集器/调度器/控制台/总结各自独立日志。
- 安全:仅监听 loopback、令牌 + HttpOnly 会话、密钥只回传掩码、可选 HMAC 上报校验。

### 修复(来自历史踩坑)
- **采集器信号死锁**: 原在信号处理器里调用 `srv.shutdown()`, 而 `serve_forever()` 跑在同一线程 →
  进程无法退出、一直占着端口, 重启后新实例绑定失败即退出。改为落指标后 `os._exit(0)`。
- 服务判活模式与启动方式不一致(`collector.py` vs `-m qqops.collector`)→ 会造成「状态显示未运行、按重启却重复启动」。
- 进程管理改为 `nohup` + PID 文件 + TERM/KILL 两段式停止, 不再依赖 screen 会话名(避免 "Session name already in use")。
- 旧配置迁移只写 `summarizer.groups`、未同步群勾选 → 现在同时写 `groups.enabled`, 控制台是唯一事实来源。
- NapCat 用 `Transfer-Encoding: chunked` 上报导致连接错位 → 显式消费请求体 + HTTP/1.0 关闭连接。
- 容器时区为 UTC 导致定时偏移 8 小时 → 全程序强制 `TZ=Asia/Shanghai`。
- 插件配置 BOM 导致写入失败 → 统一 `utf-8-sig` 读取。
