# Changelog

本插件版本号以 `plugin.json` 为准（当前 v1.14.2）。

- 完整开发台账（设计决策、不变量、故障档案、逐版本登记）为内部文档
  `docs/DEVLOG.md`，由维护者本地保留，不随本仓库与发行包分发。
- 1.8.x 之前的版本要点也汇总在 `plugin.json` 的 `description` 字段中。

## 发布约定

- Tag 格式 `v<major>.<minor>.<patch>`，与 `plugin.json` 版本一致。
- Release 附件用 `tools/build_release_zip.py` 生成：只含源码，自动排除
  `var/`、`state/`、缓存与日志，并带凭据特征扫描。

## 近期要点（节选）

- **1.14.x** —— 当前发布线。
- **1.12.0** —— 集成「云端 OAuth」（jethub）：9 家免费额度 LLM 提供商、
  面板化凭据管理、多账号支持；上游为 MIT 的 deepseek-harness-codearts。
- **1.10.0** —— MiMo TTS 作为并列独立配音引擎接入。
- **1.8.x** —— 动态视频方案韧性修复族（reference_beat 夹紧、参考画面草案
  修复顺序、同族字段一次修完）。
- **1.7.x** —— Agnes AI 云端视频协议翻译层（可选、按秒计费、默认关闭）。
- **1.5.0** —— 商汤模型清单校正；卸载改为按键定向还原。
- **1.3.0** —— JSON 模式守卫（自定义 Agent 1 提示词导致的 HTTP 400）。
