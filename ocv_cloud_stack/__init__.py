"""OCV 全云端免费栈插件（cloud_free_stack）。

本包把三个云端免费适配器挂载到 One-Click VidGen 上：

* MiMo-TTS        —— 替换 Qwen-TTS 槽位，走 ``mimo-v2.5-tts``
* 商汤 SenseNova  —— 新增一个 ``sensenova`` 语言模型 provider（deepseek-v4-pro）
* 商汤 u1.5-lite  —— 通过本地 RunningHub 协议 shim 接入分镜生图

**OCV 源码不需要任何改动。** 挂载方式是在 ``PYTHONPATH`` 上提供一个
``sitecustomize.py``，让解释器（含 OCV 拉起的每一个子进程）启动时自动安装
一层轻量导入钩子，等目标模块导入完成后就地打补丁。
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
