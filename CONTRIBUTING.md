# 贡献指南

感谢你愿意花时间。这个项目**零第三方依赖、注释与文档用中文**,请先读三条红线再动手。

## 三条红线

1. **不提交任何隐私信息。** 不要提交 `data/`、真实的 QQ 号/群号、令牌、设备序列号、本机绝对路径
   (`C:\Users\<你的名字>`)、个人邮箱。提交前请跑:
   ```bash
   python3 tools/check_secrets.py --staged
   ```
   建议先装钩子,让它在每次 `git commit` 时自动拦:
   ```bash
   bash tools/install-hooks.sh
   cp tools/secret_denylist.example.txt tools/secret_denylist.txt   # 填入你自己的敏感值(该文件不入仓)
   ```
2. **不引入第三方依赖。** 核心必须只用标准库 —— 手机上的 Python 装包经常失败,"装不上依赖 = 程序不可用"。
   测试里有一项专门断言模块级不得出现第三方导入。确实需要新能力时,做成**可选依赖 + 优雅降级**。
3. **不破坏容错。** 新增的循环、线程、请求处理、插件钩子都要有异常出口(记账 + 日志 + 降级),
   不能让一个坏插件/坏配置/网络抖动把整个进程带走。

## 开发环境

只需要 Python 3.10+;控制台探针需要 Node 18+。无需安装任何包。

```bash
git clone https://github.com/mayuxiao2021/qqops && cd qqops

python3 tests/smoke.py            # 冒烟测试(纯标准库, 不需要网络; 手机与 PC 都应通过)
node tools/console_probe.js       # 桩 DOM 渲染全部控制台页面 + 点一遍关键按钮(能抓到"渲染正常但点了没用")
python3 tools/check_secrets.py    # 隐私扫描
python3 tools/check_secrets.py --tree   # 连"还没 git add 的新文件"一起扫(打包前用这个)
python3 pack.py && python3 tools/verify_package.py   # 打包并校验 包==工作区==已提交
```

Windows 上注意:shell 脚本必须是 LF 换行(`.gitattributes` 已兜底);用 `pwsh` 时别在命令行里塞复杂引号,
把脚本写成文件再执行。

## 文档站

使用文档在 `docs/guide/`,用 **VitePress** 构建成静态站点(这是仓库里唯一的 Node 依赖,只为文档服务;
程序本体仍然是零依赖纯标准库 Python)。

```bash
npm ci                # 只在第一次或依赖变动后需要
npm run docs:dev      # 本地预览 http://localhost:5173/qqops/
npm run docs:build    # 构建到 docs/.vitepress/dist(死链会直接失败)
```

推送 `docs/**` 会自动触发 `.github/workflows/docs.yml`。两点注意:

- **死链检查是开着的**(`ignoreDeadLinks: false`)。写了指向不存在页面的链接,构建就会失败 —— 这是故意的,
  文档站最怕"点进去 404"。
- **发布到 GitHub Pages 需要仓库所有者一次性启用**:Settings → Pages → Build and deployment → Source 选
  `GitHub Actions`。启用前工作流只构建、不发布(并把站点产物传成 `docs-site` 供离线查看),
  不会让仓库一直挂红;启用后重跑一次即可上线。

## 代码风格

- **纯标准库**,Python 3.10+ 语法;模块顶部 `from __future__ import annotations`。
- 中文注释与中文日志/报错信息是这个项目的风格(用户看的就是中文控制台),请保持一致。
- 注释解释**为什么**,不解释"这行在做什么";踩过的坑写进注释(项目里已有大量先例)。
- 函数短、单一职责;跨层调用走 `framework/api.py` 暴露的面,插件不要 import 内部实现。
- 新配置项要同时给:默认值、`schema` 描述、中文标签(`static/app.js` 的 `CFG_CN`)、必要的 `hint`。
- 新接口要有:入参校验、失败时的明确错误、`docs/` 里的说明。

## 提交与 PR

- 提交信息:`<type>(<scope>): <做了什么>`。type 用 `feat|fix|docs|refactor|test|chore|perf|security`。
  一次提交讲一件事;修 bug 请写清**根因**(而不只是现象)。
- PR 前请确认:
  - [ ] `python3 tests/smoke.py` 全绿(PC;有条件的话真机也跑一遍)
  - [ ] `node tools/console_probe.js` 全绿(改了前端必跑)
  - [ ] `python3 tools/check_secrets.py` 干净
  - [ ] 改了行为就更新 `docs/` 与 `CHANGELOG.md`
  - [ ] 新增功能带测试;修 bug 带**能复现该 bug 的**测试
- PR 描述里写:问题、方案、验证方式(命令 + 输出)、影响面。截图/日志片段很有帮助。
- 大改动请先开 issue 讨论,避免白做。

## 目录约定

```
qqops/            程序本体(纯标准库)
  framework/      事件总线 / 管线 / 插件 / 注册表 / 日志 / 错误 / Web 路由(≈ AstrBot 的 core/)
  plugins/        内置插件(≈ builtin_stars):assistant(智能查询) · reminders(截止提醒)
  console/        控制台页面 API(≈ dashboard)
  static/         前端(原生 JS + CSS, 无构建)
tests/smoke.py    冒烟测试(单文件, unittest)
tools/            开发工具:隐私扫描 / 控制台探针 / 打包校验 / 保活脚本
docs/             文档:guide/ 是使用文档(文档站), 其余是设计与运维文档
  .vitepress/     文档站配置(VitePress, 仅文档用)
data/             运行时数据(不入仓):config.json / messages.db / logs / reports / backups
```

## 行为准则

参与本项目即表示同意 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
