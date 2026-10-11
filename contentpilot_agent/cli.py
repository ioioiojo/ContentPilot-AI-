from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv

from .models import ContentRequest
from .workflow import run_workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ContentPilot 多平台内容运营 Agent")
    parser.add_argument("--product", required=True, help="商品名称")
    parser.add_argument("--description", required=True, help="商品核心卖点")
    parser.add_argument("--audience", required=True, help="目标用户")
    parser.add_argument("--goal", default="提升点击和转化")
    parser.add_argument(
        "--platforms",
        default="xiaohongshu,douyin",
        help="逗号分隔：xiaohongshu,douyin,wechat",
    )
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--max-iterations", type=int, default=3)
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    args = build_parser().parse_args()
    request = ContentRequest(
        product_name=args.product,
        product_description=args.description,
        audience=args.audience,
        goal=args.goal,
        platforms=[item.strip() for item in args.platforms.split(",") if item.strip()],
        content_count=args.count,
    )
    result = run_workflow(request, max_iterations=args.max_iterations)
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    if os.getenv("CONTENTPILOT_PROVIDER", "mock") == "mock":
        print("\n当前使用 Mock 模式；配置 CONTENTPILOT_PROVIDER=compatible 和模型凭据后切换。")


if __name__ == "__main__":
    main()
