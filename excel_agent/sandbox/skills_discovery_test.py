"""阶段二审查缺口 #3 的最小验证 —— 不是正式测试套件，不接入 CI。

不自己手写一个"看起来差不多"的 ls+read 测试，而是直接调用 deepagents 内部真正被
SkillsMiddleware 使用的同一个函数 `_list_skills(backend, source_path)`（内部实现是
backend.ls(source_path) 找目录，再 backend.download_files(...) 批量取 SKILL.md 内容，解析
YAML frontmatter）——这是目前能做到的最贴近生产路径的验证，只是调用方从
SkillsMiddleware.__init__（起真实 agent）换成了这个独立脚本，避免为了验证去动 main.py。

用法：
    uv run python sandbox/skills_discovery_test.py

前置条件：
    1. docker-compose.yml 的 sandbox service 已经加了 `./src/agent/skills:/workspace/skills:ro`
       挂载。
    2. `docker compose up -d sandbox` 已经跑起来，且挂载已生效（compose 检测到 volumes 变化会
       自动重建容器；没生效的话手动 `docker compose up -d --force-recreate sandbox`）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from deepagents.middleware.skills import _list_skills

from src.agent.backends.docker_sandbox import DockerSandbox
from src.agent.tools._paths import PROJECT_ROOT

SKILLS_SOURCE = "/workspace/skills/"
HOST_SKILLS_DIR = PROJECT_ROOT / "src" / "agent" / "skills"


def main() -> int:
    sandbox = DockerSandbox()
    ok = True

    expected_names = {
        p.name for p in HOST_SKILLS_DIR.iterdir() if p.is_dir() and (p / "SKILL.md").exists()
    }
    print(f"宿主机上实际的技能目录：{sorted(expected_names)}")

    print(f"\n调用 _list_skills(DockerSandbox(), {SKILLS_SOURCE!r}) ...")
    skills = _list_skills(sandbox, SKILLS_SOURCE)
    found_names = {s["name"] for s in skills}
    print(f"容器里发现的技能：{sorted(found_names)}")

    if found_names != expected_names:
        ok = False
        missing = expected_names - found_names
        extra = found_names - expected_names
        print(f"   ✗ 集合不一致：缺失={sorted(missing)} 多余={sorted(extra)}")
    else:
        print("   ✓ 发现的技能名跟宿主机目录完全一致")

    print("\n逐个比对 SKILL.md 内容（容器 bind mount 读到的 vs 宿主机原文件字节级对比）：")
    for skill in skills:
        name = skill["name"]
        host_skill_md = HOST_SKILLS_DIR / name / "SKILL.md"
        host_content = host_skill_md.read_text(encoding="utf-8")

        # skill["path"] 就是 _list_skills 内部实际拿去调 download_files 的那个容器路径字符串
        # （backend.ls 返回的 path，再拼上 "SKILL.md"）——这里直接复用它重新下载一次做字节级
        # 对比，而不是自己另外拼一条"看起来差不多"的路径。
        download_result = sandbox.download_files([skill["path"]])[0]
        if download_result.error is not None:
            ok = False
            print(f"   ✗ {name}: 重新下载 {skill['path']!r} 失败：{download_result.error}")
            continue

        container_content = (download_result.content or b"").decode("utf-8")
        if container_content == host_content:
            print(f"   ✓ {name}: 内容一致（path={skill['path']!r}）")
        else:
            ok = False
            print(
                f"   ✗ {name}: 内容不一致（容器 {len(container_content)} 字符 vs "
                f"宿主机 {len(host_content)} 字符，path={skill['path']!r}）"
            )

    print("\n" + ("全部通过 ✓" if ok else "未通过 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
