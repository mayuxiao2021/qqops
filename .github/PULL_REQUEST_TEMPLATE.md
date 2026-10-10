## 这个 PR 做了什么

<!-- 一句话说清"解决什么问题", 而不是"改了哪些文件" -->

## 关联 issue

<!-- Closes #123 -->

## 改动类型

- [ ] 修 bug(请写清**根因**,不只是现象)
- [ ] 新功能
- [ ] 重构 / 性能
- [ ] 文档 / 工程
- [ ] 安全相关

## 验证方式

<!-- 贴命令与输出;改了前端请贴探针输出;真机验证更好 -->

```bash
python3 tests/smoke.py
node tools/console_probe.js
python3 tools/check_secrets.py
```

## 自查清单

- [ ] 没有引入第三方依赖(核心只用标准库)
- [ ] 没有提交任何隐私信息:`data/`、真实 QQ 号/群号、令牌、设备号、本机绝对路径、个人邮箱
- [ ] `python3 tools/check_secrets.py` 干净(必要时配了 `tools/secret_denylist.txt`)
- [ ] 新增/修改行为有测试;修 bug 附带**能复现该 bug** 的测试
- [ ] 循环/线程/请求/插件钩子都有异常出口(记账 + 日志 + 降级),不会把进程带走
- [ ] 改了行为就同步更新 `docs/` 与 `CHANGELOG.md`
- [ ] 新配置项给了默认值、schema 描述与中文标签(`static/app.js` 的 `CFG_CN`)

## 影响面 / 风险

<!-- 兼容性、数据迁移、性能、成本、需要用户做什么 -->
