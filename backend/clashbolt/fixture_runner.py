"""Child process for the shipped synthetic fixture, not an OS security sandbox."""

import argparse
import asyncio
import json
import os

from clashbolt.experiment import execute, execute_postgres


def _apply_fixed_resource_limits() -> None:
    """Best-effort POSIX limits for the trusted fixed runner, not a sandbox."""
    if os.name != "posix":
        return
    import resource

    bounds = (
        (resource.RLIMIT_CPU, 3),
        (resource.RLIMIT_AS, 768 * 1024 * 1024),
        (resource.RLIMIT_FSIZE, 1024 * 1024),
        (resource.RLIMIT_NOFILE, 64),
    )
    for kind, limit in bounds:
        soft, hard = resource.getrlimit(kind)
        adjusted = min(
            limit,
            soft if soft != resource.RLIM_INFINITY else limit,
            hard if hard != resource.RLIM_INFINITY else limit,
        )
        resource.setrlimit(kind, (adjusted, adjusted))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a fixed synthetic inventory fixture")
    parser.add_argument("variant", choices=("racy", "locked"))
    parser.add_argument("schedule", choices=("overlap", "serial"))
    args = parser.parse_args()
    _apply_fixed_resource_limits()
    database_url = os.getenv("CLASHBOLT_TARGET_DATABASE_URL")
    if database_url:
        result = asyncio.run(execute_postgres(args.variant, args.schedule, database_url))
    else:
        result = asyncio.run(execute(args.variant, args.schedule))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
