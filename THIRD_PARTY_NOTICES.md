# 第三方组件与在线服务说明

本插件自研代码使用 `AGPL-3.0-only`（见 [LICENSE](./LICENSE) 与 [NOTICE](./NOTICE)）。
下列第三方组件与在线服务不因本项目许可证而改变其原有权利和许可条件。

## Vendored 代码（`vendor/` 目录）

`vendor/` 是自带的依赖闭包：由开发机上的 `vendor_tool.py` 再生并**提交进仓库**，
使插件在没有 DSH、npm 或网络的机器上也能独立运行。每个包都保留其自带的
LICENSE 文件，正文以上游为准。

| 组件 | 上游 | 许可 |
| --- | --- | --- |
| dsh-codearts-auth | https://gitee.com/iJetLi/deepseek-harness-codearts | MIT（Copyright (c) 2026 Jet） |
| @deepseek-ai/cordis | https://github.com/cordjs/cordis | MIT（Copyright (c) 2021-present Shigma） |
| @deepseek-ai/cosmokit | https://github.com/cordjs/cosmokit | MIT |
| @deepseek-ai/schemastery | https://github.com/cordjs/schemastery | MIT |
| @deepseek-ai/dsh-* 各包 | DeepSeek Harness（DSH） | 以各包内 LICENSE 为准 |
| jose | https://github.com/panva/jose | MIT |
| zod | https://github.com/colinhacks/zod | MIT |

## 宿主项目

本插件的运行宿主是 One-Click VidGen（OCV / 一键成片，AGPL-3.0-only，
Copyright (C) 2026 Zhou Ruoyu 周若雨 / He Yun 何允）：

- 官方仓库：https://github.com/IFRIT-Zhou/One-Click-VidGen

插件仅在本地挂接 OCV 运行时，不修改、不再分发 OCV 源码。OCV 品牌标识不随
本许可证授权，使用边界见 [TRADEMARKS.md](./TRADEMARKS.md)。

## 第三方在线服务

本插件适配以下独立第三方在线服务。收录不代表隶属、合作、授权或背书；
可用性、计费与免费额度政策完全由各服务商决定，并可能随时调整或终止：

- 小米 MiMo（TTS 配音）
- 商汤 SenseNova（语言模型与生图）
- Agnes AI（云端视频生成，可选组件，按秒计费，默认关闭）
- 云端 OAuth 提供商：CodeBuddy（腾讯）、华为 CodeArts、WorkBuddy、LobsterAI、
  Qoder、Trae、Cline、loomy、atomcode（GitCode）

用户须自行遵守各服务商的服务条款、可接受使用政策、内容政策与配额规则。
**特别提示：** 在官方产品之外以自动化方式或账号池方式使用编程助手类服务的
额度，可能违反相应服务商条款，并可能导致账号被限制或封禁；「云端 OAuth」
为可选功能，是否启用由用户自行判断，风险与后果自负。

## 遗漏反馈

如发现遗漏的版权或许可证信息，请在本仓库提交 Issue。
