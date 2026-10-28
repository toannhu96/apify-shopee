"""Fetch Shopee Affiliate VN product data by ID using a logged-in Chrome profile.

    python get_product.py 26609048170
    python get_product.py 26609048170 123456789 --out output

First run: sign in manually in the Chrome window opened by the script. This uses
a separate persistent profile, not the Chrome profile open on your desktop.
Raw responses and normalized rows.json are saved to a timestamped output folder.
"""

from __future__ import annotations

import argparse

from shopee_affiliate import probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("product_ids", nargs="+", help="One or more numeric Shopee product IDs")
    parser.add_argument("--profile-dir", default=".auth/shopee-profile")
    parser.add_argument("--out", default="output", help="Parent directory for JSON results")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--headless", action="store_true", help="Use only after signing in once")
    args = parser.parse_args(argv)
    options = [
        "--profile-dir", args.profile_dir,
        "--out", args.out,
        "--channel", args.channel,
        *(["--headless"] if args.headless else []),
        "detail",
        *(value for item_id in args.product_ids for value in ("--item-id", item_id)),
    ]
    return probe.main(options)


if __name__ == "__main__":
    raise SystemExit(main())
