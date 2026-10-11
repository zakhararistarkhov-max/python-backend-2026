import argparse
import os
import sys

from isolation_lab.lab import IsolationLab


def main(argv=None):
    parser = argparse.ArgumentParser(description="Transaction isolation lab on H2 via JPype and JDBC.")
    parser.add_argument("--journal", default="logs/run.jsonl")
    parser.add_argument("--jar", default=os.environ.get("H2_JAR", "/opt/h2/h2.jar"))
    parser.add_argument("--step-timeout", type=float, default=10.0)
    parser.add_argument("--lock-timeout-ms", type=int, default=3000)
    args = parser.parse_args(argv)
    lab = IsolationLab(args.journal, args.jar, step_timeout=args.step_timeout, lock_timeout_ms=args.lock_timeout_ms)
    try:
        code = lab.run()
    except Exception as exc:
        print(f"FAIL lab: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        code = 2
    if lab.stuck:
        sys.stdout.flush()
        os._exit(code or 1)
    return code


if __name__ == "__main__":
    sys.exit(main())
