# 安全策略 / Security Policy

## 报告漏洞

请勿用公开 Issue 报告安全问题（凭据泄露、注入、鉴权绕过等）。请优先使用
GitHub 的私密漏洞上报（本仓库 Settings → Security → Private vulnerability
reporting），或通过仓库主页的联系方式私下联系维护者。我们会在 7 天内响应。

## 凭据卫生（重要）

- 本插件的运行期密钥**只**存放在本地、且被 Git 忽略的目录中：
  - `var/config.json` —— 面板里填写的各服务商 API Key（SenseNova、MiMo、Agnes 等）
  - `state/credentials.json` —— 云端 OAuth 账号令牌
- 这两个目录**永不**提交、打包或分享。`tools/build_release_zip.py` 会自动排除
  它们，并在打包前扫描凭据特征，命中即拒绝出包。
- 如果你曾经把含有 `var/` 或 `state/` 的目录打包发出，请将其中所有 Key 与
  令牌视为已泄露：**立即到对应服务商后台作废并更换**，OAuth 账号请登出以
  使 refresh token 失效。
- 提交前请确认 `git status` 中没有 `var/`、`state/`、`*.log` 或任何形似密钥
  的文件。

## 支持版本

安全修复只应用于最新发布线（版本号见 `plugin.json`）；旧版本不再维护。
