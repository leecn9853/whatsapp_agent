"""查看 data/ 目录里持久化数据的命令行工具。

data/checkpoints.sqlite 里的 checkpoint 内容是 msgpack 二进制序列化的，而且 messages
字段用了 DeltaChannel（增量存储，见 src/main.py 里 checkpointer 那段注释），大多数
checkpoint 里根本不包含完整的消息列表，只有 LangGraph 自己知道怎么把增量拼回完整历史
——所以看对话历史必须用 agent.get_state()，不能直接读 SQL。
data/memory_store.sqlite 是 JSON 明文，但存的 key 是 "/AGENTS.md"（CompositeBackend
把 "/memories/" 这个路由前缀去掉了，不是 "/memories/AGENTS.md"），直接用 SQL 查也容易
查错 key，所以也封装成函数用 store.get() 查。

用法：
    uv run python scripts/inspect_data.py threads
    uv run python scripts/inspect_data.py conversation "12345@c.us"
    uv run python scripts/inspect_data.py users
    uv run python scripts/inspect_data.py memory "12345@c.us"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.main import agent, store  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CHECKPOINTS_DB = DATA_DIR / "checkpoints.sqlite"
MEMORY_DB = DATA_DIR / "memory_store.sqlite"

MEMORY_KEY = "/AGENTS.md"  # CompositeBackend 里 "/memories/" 前缀被路由后剥掉了


def _require(path: Path) -> None:
    if not path.exists():
        print(f"文件不存在：{path}（还没有任何数据写入，或者路径不对）")
        sys.exit(1)


def cmd_threads(_args: argparse.Namespace) -> None:
    """列出 checkpoints.sqlite 里所有的 thread_id（= WhatsApp chat_id）。"""
    _require(CHECKPOINTS_DB)
    conn = sqlite3.connect(str(CHECKPOINTS_DB))
    rows = conn.execute(
        "SELECT thread_id, COUNT(*) AS n FROM checkpoints GROUP BY thread_id ORDER BY n DESC"
    ).fetchall()
    conn.close()
    if not rows:
        print("目前没有任何对话记录。")
        return
    for thread_id, count in rows:
        print(f"{thread_id}\t{count} 个 checkpoint")


def cmd_conversation(args: argparse.Namespace) -> None:
    """打印某个 thread_id（chat_id）当前的完整消息历史（自动拼回 DeltaChannel 增量）。"""
    snapshot = agent.get_state({"configurable": {"thread_id": args.chat_id}})
    messages = snapshot.values.get("messages", [])
    if not messages:
        print(f"没有找到 chat_id={args.chat_id!r} 的对话记录。")
        return
    for msg in messages:
        role = type(msg).__name__
        content = getattr(msg, "content", msg)
        print(f"[{role}] {content}")


def cmd_users(_args: argparse.Namespace) -> None:
    """列出 memory_store.sqlite 里所有已经有记忆的用户（namespace）。"""
    _require(MEMORY_DB)
    conn = sqlite3.connect(str(MEMORY_DB))
    rows = conn.execute("SELECT DISTINCT namespace FROM items ORDER BY namespace").fetchall()
    conn.close()
    if not rows:
        print("目前没有任何用户记忆。")
        return
    for (namespace,) in rows:
        print(namespace)


def cmd_memory(args: argparse.Namespace) -> None:
    """打印某个用户的 /memories/AGENTS.md 内容。

    chat_id 传原始值即可（比如带句点的 "12345@c.us"），这里按 src/main.py 里
    namespace 工厂用的同一种规则（句点替换成下划线）转换后再查找。
    """
    namespace = (args.chat_id.replace(".", "_"),)
    item = store.get(namespace, MEMORY_KEY)
    if item is None:
        print(f"没有找到 chat_id={args.chat_id!r} 对应的记忆（namespace={namespace!r}）。")
        return
    print(item.value["content"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("threads", help="列出所有对话（thread_id/chat_id）").set_defaults(func=cmd_threads)

    p = subparsers.add_parser("conversation", help="打印某个 chat_id 的完整对话历史")
    p.add_argument("chat_id", help="WhatsApp chat_id，例如 12345@c.us")
    p.set_defaults(func=cmd_conversation)

    subparsers.add_parser("users", help="列出所有已经有记忆的用户").set_defaults(func=cmd_users)

    p = subparsers.add_parser("memory", help="打印某个 chat_id 的 /memories/AGENTS.md 内容")
    p.add_argument("chat_id", help="WhatsApp chat_id，例如 12345@c.us")
    p.set_defaults(func=cmd_memory)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
