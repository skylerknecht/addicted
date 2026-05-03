"""
Refresh Masters+ data — standalone CLI runner.

Usage:
  python refresh_masters.py --api-key RGAPI-... [--count 1000] [--region na1] [--start-date 2025-04-01]

Calls fetch_all_masters from analyzer.py to (re)build the per-role caches in cache/.
This is what the old "Refresh Masters+" button did, just headless.
"""
import argparse
import os
import sys
import time

# Importing analyzer doesn't start the server because the HTTP listener is gated by `if __name__ == "__main__": main()`.
import analyzer


def main():
    parser = argparse.ArgumentParser(
        description="Refresh Masters+ aggregated data per role.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--api-key", "-k", required=True,
                        help="Riot API key (RGAPI-...)")
    parser.add_argument("--count", "-c", type=int, default=1000,
                        help="Target games per role (will fetch matches until each role has this many)")
    parser.add_argument("--region", "-r", default="na1",
                        help="Platform region (na1, euw1, kr, br1, eun1, la1, la2, oc1, tr1, ru, jp1)")
    parser.add_argument("--start-date", "-s", default=None,
                        help="Earliest match start date (YYYY-MM-DD). Skipped if not provided.")
    parser.add_argument("--force", "-f", action="store_true", default=True,
                        help="Delete existing cache files before fetching (default: true)")
    parser.add_argument("--no-force", dest="force", action="store_false",
                        help="Keep existing cache, only fill in missing roles")
    args = parser.parse_args()

    region = args.region.lower()
    roles = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    cache_dir = analyzer.CACHE_DIR

    if args.force:
        for r in roles:
            cf = os.path.join(cache_dir, f"masters_{r}_{region}.json")
            if os.path.exists(cf):
                os.remove(cf)
                print(f"[CLEAR] removed {os.path.basename(cf)}")

    print(f"[START] region={region}  target={args.count} games/role  start_date={args.start_date or 'any'}")
    t0 = time.time()
    err = analyzer.fetch_all_masters(
        args.api_key,
        region=region,
        target_per_role=args.count,
        start_date=args.start_date,
    )
    elapsed = time.time() - t0

    if err == "KEY_EXPIRED":
        print("[ERROR] API key expired or invalid")
        sys.exit(1)
    if err:
        print(f"[ERROR] {err}")
        sys.exit(1)

    rate = analyzer.get_rate_info()
    print(f"[DONE] {elapsed/60:.1f} min  total API calls: {rate.get('total', '?')}")


if __name__ == "__main__":
    main()
