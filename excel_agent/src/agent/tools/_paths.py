"""项目根目录路径，被多个工具模块共用（input/output/templates/assets/snapshots 都挂在
根目录下）。集中在这一处算，阶段二脚本搬进 skills/*/scripts/ 在沙箱容器里跑成 CLI 时，
只需要改这一处对"运行位置"的判断，不用在每个用到路径的文件里各改一遍。

不对 LLM 暴露任何 @tool。
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
