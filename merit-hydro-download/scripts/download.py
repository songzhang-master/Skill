"""Run the download skill from a standalone copy of this directory."""
import argparse
import json

from merit.merit_basins import MeritBasinsTools
from merit.merit_download import MeritDownloadTools


def main():
    parser = argparse.ArgumentParser(description='Download MERIT river data for a WGS84 bbox.')
    parser.add_argument('--bbox', nargs=4, type=float, required=True, metavar=('WEST','SOUTH','EAST','NORTH'))
    parser.add_argument('--output', required=True)
    parser.add_argument('--source', choices=['basins','hydro_raster'], default='basins')
    parser.add_argument('--cache-dir', help='Reusable vector download cache (default: OUTPUT/cache)')
    parser.add_argument('--threshold-km2', type=float, default=1.0)
    parser.add_argument('--project', default='')
    args = parser.parse_args()
    if args.source == 'basins':
        tool = MeritBasinsTools(args.output, cache_dir=args.cache_dir)
    else:
        tool = MeritDownloadTools(args.output)
    result = tool.acquire(args.bbox, args.threshold_km2, args.project)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
