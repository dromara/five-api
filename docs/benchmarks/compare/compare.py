"""Same load generator as scripts.benchmark against other gateways: python compare.py NAME URL KEYS_FILE."""
import json
import multiprocessing
import sys

from scripts.benchmark import measure


def main():
    name, url, keys_file = sys.argv[1:4]
    auth = [{"Authorization": "Bearer " + line.strip()} for line in open(keys_file) if line.strip()]
    body = {"model": "bench-model", "max_tokens": 16, "messages": [{"role": "user", "content": "synthetic benchmark"}]}
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        measure(pool, 4, url, body, auth, 300, 64)
        runs = [measure(pool, 4, url, body, auth, 4000, 64) for _ in range(3)]
    print(json.dumps({"gateway": name, "keys": len(auth), "runs": runs}))


if __name__ == "__main__":
    main()
