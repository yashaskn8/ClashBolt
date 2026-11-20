"""Child process for the shipped synthetic fixture, not an OS security sandbox."""

import argparse
import asyncio
import json
import os

from clashbolt.experiment import execute, execute_postgres


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a fixed synthetic inventory fixture")
    parser.add_argument("variant", choices=("racy", "locked"))
    parser.add_argument("schedule", choices=("overlap", "serial"))
    args = parser.parse_args()
    database_url = os.getenv("CLASHBOLT_TARGET_DATABASE_URL")
    if database_url:
        result = asyncio.run(execute_postgres(args.variant, args.schedule, database_url))
    else:
        result = asyncio.run(execute(args.variant, args.schedule))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
