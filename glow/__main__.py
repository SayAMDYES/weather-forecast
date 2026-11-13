import argparse
import logging

from glow.config import DEFAULT_CONFIG, load_config


def main():
    parser = argparse.ArgumentParser(prog="python -m glow", description="火烧云（朝霞 / 晚霞）预报")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="YAML 配置文件")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="启动本地网页（不会自动计算，在网页上框选区域后才计算）")
    p_run = sub.add_parser("run", help="计算已保存的区域，然后退出")
    p_run.add_argument("--region", help="只算这个区域 id（默认全部）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.command == "serve":
        from glow.server import serve
        serve(cfg)
    else:
        from glow import regions
        from glow.pipeline import run_region
        todo = [r for r in regions.load_all(cfg) if args.region in (None, r.id)]
        if not todo:
            parser.error("没有已保存的区域（先在网页上框选）" if args.region is None else f"找不到区域 {args.region}")
        for r in todo:
            run_region(cfg, r)


if __name__ == "__main__":
    main()
