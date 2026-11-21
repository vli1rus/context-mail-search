import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter_ns
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

from .data import DEMO_QUERIES
from .evaluate import percentile_nearest_rank, read_queries


def main():
    parser = argparse.ArgumentParser(description='Measure server-side search latency of a running instance')
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--queries', type=Path, default=DEMO_QUERIES)
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--mode', choices=['lexical', 'semantic', 'smart'], default='smart')
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or args.repeat < 1 or args.workers < 1:
        parser.error('Limit must be 1–100, repeat and workers must be positive')
    try:
        queries = list(read_queries(args.queries))
        if not queries:
            raise ValueError('No queries available')

        def request(row):
            _, query, mailbox, day = row
            params = urlencode({'q': query, 'synthetic_user': mailbox, 'query_date': day, 'mode': args.mode, 'limit': args.limit})
            started = perf_counter_ns()
            client = build_opener(ProxyHandler({}))
            with client.open(f'{args.base_url.rstrip("/")}/api/search?{params}', timeout=20) as response:
                timing = response.headers['Server-Timing']
                data = json.load(response)
                if timing is None or not timing.startswith('backend;dur=') or not isinstance(data.get('items'), list):
                    raise ValueError('Expected search JSON and the backend Server-Timing header')
                return float(timing.partition('=')[2]), (perf_counter_ns() - started) / 1e6

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            backend, roundtrip = zip(*pool.map(request, queries * args.repeat))
        p99_ms = percentile_nearest_rank(backend, .99)
        print(json.dumps({'requests': len(backend), 'workers': args.workers, 'limit': args.limit,
                          'backend_p50_ms': percentile_nearest_rank(backend, .50),
                          'backend_p95_ms': percentile_nearest_rank(backend, .95),
                          'backend_p99_ms': p99_ms,
                          'client_p99_ms': round(percentile_nearest_rank(roundtrip, .99), 3),
                          'under_one_second': p99_ms < 1000}, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
