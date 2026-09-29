"""Measure cold readiness and end-to-end worker latency without changing the catalog."""
import argparse
import json
import os
from pathlib import Path
import select
import statistics
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--configuration', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    config = json.loads(args.configuration.read_text())
    command = [sys.executable, '-B', str(args.worker), '--index', config['index'], '--catalog', str(args.catalog)]
    if config.get('speech'):
        command += ['--speech', config['speech']]
    with args.output.open('x') as output, args.output.with_suffix('.stderr.log').open('x') as errors:
        started = time.monotonic()
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                   env={**os.environ, 'HF_HUB_OFFLINE': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
        def read():
            if not select.select([process.stdout], [], [], 240)[0]:
                raise TimeoutError('Search worker did not respond in 240 seconds')
            result = json.loads(process.stdout.readline())
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result
        try:
            ready = read()
            report = dict(ready=ready, cold_seconds=time.monotonic() - started, requests=[])
            for round_number in range(args.rounds):
                for mode in ('both', 'speech'):
                    for query in ('go', 'goats', 'milking goats', 'dun', 'dunare', 'copii'):
                        started = time.monotonic()
                        process.stdin.write((json.dumps(dict(query=query, mode=mode)) + '\n').encode())
                        process.stdin.flush()
                        result = read()
                        deadline = time.monotonic() + 30
                        while result.get('visual_pending'):
                            if time.monotonic() > deadline:
                                raise TimeoutError('Visual warmup did not complete')
                            time.sleep(.05)
                            process.stdin.write((json.dumps(dict(query=query, mode=mode)) + '\n').encode())
                            process.stdin.flush()
                            result = read()
                        row = dict(round=round_number, mode=mode, query=query,
                                   wall_ms=round((time.monotonic() - started) * 1000, 2),
                                   worker_ms=round(result['elapsed'] * 1000, 2),
                                   hits=len(result['hits']), samples=result['indexed_samples'])
                        report['requests'].append(row)
                        print(json.dumps(row), flush=True)
                        time.sleep(.15)
            for mode in ('both', 'speech'):
                timings = sorted(row['wall_ms'] for row in report['requests'] if row['mode'] == mode)
                report[mode] = dict(median_ms=statistics.median(timings),
                                    p95_ms=timings[min(len(timings) - 1, int(len(timings) * .95))],
                                    max_ms=max(timings))
            json.dump(report, output, indent=2)
            print(json.dumps({k: v for k, v in report.items() if k != 'requests'}), flush=True)
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
