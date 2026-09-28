"""OCV 全云端免费栈插件 —— 解释器级自动注入点。

只要本文件所在目录出现在 ``PYTHONPATH`` 上，CPython 在每次启动时
（``site`` 模块初始化阶段）都会自动导入 ``sitecustomize``，
因此 **OCV 拉起的每一个子进程**都会走到这里：

* ``backend/app/main.py``（uvicorn 主进程）
* ``module1_agent_director.py``（配音子进程）
* ``module2_scene_director.py``（识别子进程）
* ``module4_video_render.py``（生图子进程）
* ``module5_video_render.py``（成片子进程）

这就是本插件不需要修改 OCV 任何一行源码的物理基础。

设计约束：**这里绝不允许抛异常**。哪怕插件配置全错、依赖缺失，
OCV 也必须能照常启动。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from ocv_cloud_stack import bootstrap as _bootstrap

    _bootstrap.install()
except Exception as _exc:  # noqa: BLE001 - 兜底：插件故障不得影响 OCV
    try:
        sys.stderr.write(
            "[cloud_free_stack] 插件未能注入，OCV 将以原生模式继续运行："
            f"{type(_exc).__name__}: {_exc}\n"
        )
    except Exception:
        pass
