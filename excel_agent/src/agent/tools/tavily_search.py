import json
import os
from typing import Literal
from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv()


@tool
def web_search(
    query: str,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    include_raw_content: bool = False,
) -> str:
    """运行网络搜索获取最新信息。

    参数:
        query: 搜索关键词
        max_results: 返回的最大结果数量
        topic: 搜索主题 (general, news, finance)
        include_raw_content: 是否包含原始页面文本
    """
    try:
        from tavily import TavilyClient

        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            return json.dumps({"error": "TAVILY_API_KEY not set"}, ensure_ascii=False)

        client = TavilyClient(api_key=api_key)
        result = client.search(
            query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            topic=topic,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Search failed: {e}"}, ensure_ascii=False)
