#!/usr/bin/env python3
"""Run isolated unittest cases in parallel and report the slowest cases."""
from concurrent.futures import ProcessPoolExecutor, as_completed
import io
import os
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))


def cases(suite):
    for case in suite:
        if isinstance(case, unittest.TestSuite):
            yield from cases(case)
        else:
            yield case


def run_case(name):
    output = io.StringIO()
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromName(name))
    return (name, time.monotonic() - started, result.testsRun,
            result.wasSuccessful() and not result.skipped, output.getvalue())


def main():
    try:
        jobs = int(os.environ.get('TEST_JOBS', min(2, os.cpu_count() or 1)))
        if jobs < 1:
            raise ValueError
    except ValueError:
        sys.exit('TEST_JOBS must be a positive integer.')
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent), pattern='test_*.py')
    names = [case.id() for case in cases(suite)]
    if not names:
        sys.exit('No tests found.')
    started = time.monotonic()
    results = []
    print(f'Running {len(names)} tests with {jobs} workers; skips count as failures.', flush=True)
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_case, name) for name in names]
        for future in as_completed(futures):
            result = future.result()
            name, duration, _, passed, output = result
            results.append(result)
            print(f'{"ok" if passed else "FAIL"} {name} ({duration:.2f}s)', flush=True)
            if not passed:
                print(output, flush=True)
    print(f'\nRan {sum(r[2] for r in results)} tests in {time.monotonic() - started:.2f}s.')
    print('Slowest tests:')
    for name, duration, *_ in sorted(results, key=lambda r: r[1], reverse=True)[:5]:
        print(f'  {duration:.2f}s {name}')
    passed = all(r[3] for r in results)
    print('OK' if passed else 'FAILED (including any skipped tests).')
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
