"""Child process for the shipped synthetic fixture, not an OS security sandbox."""

import argparse
import asyncio
import json

from clashbolt.experiment import execute


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a fixed synthetic inventory fixture")
    parser.add_argument("variant", choices=("racy", "locked"))
    parser.add_argument("schedule", choices=("overlap", "serial"))
    args = parser.parse_args()
    result = asyncio.run(execute(args.variant, args.schedule))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
