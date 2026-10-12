/**
 * VitePress 站点配置(QQOps 文档站)。
 *
 * 为什么用 VitePress: 与 AstrBot / NapCat 等同类项目一致, 中文排版好, 静态构建可直接发 GitHub Pages。
 * 底座路径 base 必须与仓库名一致(项目站点是 https://<user>.github.io/qqops/)。
 */
import { defineConfig } from 'vitepress'

export default defineConfig({
  lang: 'zh-CN',
  title: 'QQOps',
  description: '在手机上把 QQ 群消息管起来:采集 · 总结 · 智能查询 · 运维控制台',
  base: '/qqops/',
  cleanUrls: true,
  lastUpdated: true,
  ignoreDeadLinks: false,

  head: [
    ['meta', { name: 'theme-color', content: '#3c8772' }],
    ['meta', { property: 'og:title', content: 'QQOps 文档' }],
    ['meta', { property: 'og:description', content: '纯标准库的 QQ 群消息管家:采集、总结、AI 查询、运维控制台' }],
  ],

  themeConfig: {
    nav: [
      { text: '使用指南', link: '/guide/quickstart', activeMatch: '/guide/' },
      { text: '部署', link: '/DEPLOY' },
      { text: '设计文档', link: '/ARCHITECTURE', activeMatch: '/(ARCHITECTURE|CONSOLE|INGEST|POWER|CHAT)/' },
      { text: 'FAQ', link: '/FAQ' },
      {
        text: '仓库',
        items: [
          { text: 'GitHub', link: 'https://github.com/mayuxiao2021/qqops' },
          { text: '更新日志', link: 'https://github.com/mayuxiao2021/qqops/blob/master/CHANGELOG.md' },
          { text: '问题反馈', link: 'https://github.com/mayuxiao2021/qqops/issues' },
        ],
      },
    ],

    sidebar: {
      '/guide/': [
        {
          text: '入门',
          items: [
            { text: '快速开始', link: '/guide/quickstart' },
            { text: '安装与部署', link: '/DEPLOY' },
            { text: '命令行手册', link: '/guide/cli' },
          ],
        },
        {
          text: '使用',
          items: [
            { text: '控制台', link: '/guide/console' },
            { text: '消息与全文检索', link: '/guide/messages' },
        { text: '会不会漏消息', link: '/guide/no-loss' },
            { text: '总结与报告', link: '/guide/reports' },
            { text: 'AI 智能查询', link: '/guide/chat' },
            { text: '在 QQ 里直接问', link: '/guide/qq-chat' },
            { text: '提醒与待办', link: '/guide/reminders' },
          ],
        },
        {
          text: '配置与运维',
          items: [
            { text: '配置详解', link: '/guide/config' },
            { text: '功耗与资源', link: '/POWER' },
            { text: '保活(Android)', link: '/guide/keepalive' },
            { text: '备份与升级', link: '/guide/backup' },
            { text: '安全与隐私', link: '/guide/security' },
            { text: '排错', link: '/guide/troubleshooting' },
          ],
        },
        {
          text: '开发',
          items: [
            { text: '插件开发', link: '/guide/plugins' },
            { text: '架构总览', link: '/ARCHITECTURE' },
            { text: '参与贡献', link: 'https://github.com/mayuxiao2021/qqops/blob/master/CONTRIBUTING.md' },
          ],
        },
      ],
      '/': [
        {
          text: '文档',
          items: [
            { text: '使用指南', link: '/guide/quickstart' },
            { text: '部署指南', link: '/DEPLOY' },
            { text: '常见问题', link: '/FAQ' },
            { text: '架构设计', link: '/ARCHITECTURE' },
            { text: '控制台规划', link: '/CONSOLE' },
            { text: '消息接收规范', link: '/INGEST' },
            { text: '功耗与进程', link: '/POWER' },
            { text: '卡片与聊天问答', link: '/CHAT' },
          ],
        },
      ],
    },

    outline: { level: [2, 3], label: '本页目录' },
    docFooter: { prev: '上一篇', next: '下一篇' },
    returnToTopLabel: '回到顶部',
    sidebarMenuLabel: '目录',
    darkModeSwitchLabel: '深色模式',
    lastUpdatedText: '最后更新',

    search: {
      provider: 'local',
      options: {
        translations: {
          button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
          modal: {
            noResultsText: '没有找到结果',
            resetButtonTitle: '清除',
            footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' },
          },
        },
      },
    },

    socialLinks: [{ icon: 'github', link: 'https://github.com/mayuxiao2021/qqops' }],

    footer: {
      message: '以 AGPL-3.0 许可发布 · 与腾讯无关, 请自行遵守 QQ / NapCat / 模型服务商条款',
      copyright: 'QQOps contributors',
    },
  },
})
