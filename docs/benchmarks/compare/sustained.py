"""Sustained run: N requests, then wait until every one is in the gateway's own spend log.

python sustained.py NAME URL KEYS_FILE REQUESTS
"""
import json
import multiprocessing
import subprocess
import sys
import time

from scripts.benchmark import measure

COUNT = {
    "newapi": ["docker", "exec", "cmp-mysql", "sh", "-c",
               'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" newapi -e "SELECT count(*) FROM logs WHERE type=2" 2>/dev/null'],
    "litellm": ["docker", "exec", "cmp-pg", "psql", "-U", "postgres", "-d", "litellm", "-tAc",
                'SELECT count(*) FROM "LiteLLM_SpendLogs"'],
}


def logged(name):
    return int(subprocess.run(COUNT[name], capture_output=True, text=True).stdout.strip())


def main():
    name, url, keys_file, requests = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    auth = [{"Authorization": "Bearer " + line.strip()} for line in open(keys_file) if line.strip()]
    body = {"model": "bench-model", "max_tokens": 16, "messages": [{"role": "user", "content": "synthetic benchmark"}]}
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        before = logged(name)
        result = measure(pool, 4, url, body, auth, requests, 64)
    done = time.perf_counter()
    target = before + requests - result["errors"]
    while logged(name) < target and time.perf_counter() - done < 300:
        time.sleep(0.2)
    drain = round(time.perf_counter() - done, 2)
    result.update(gateway=name, drain_seconds=drain, logged=logged(name) - before,
                  sustained_per_second=round(requests / (result["elapsed_seconds"] + drain), 1))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
