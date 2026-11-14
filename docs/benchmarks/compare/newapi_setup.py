"""Initialise a throwaway new-api: admin, one OpenAI channel to the synthetic upstream, priced model, 16 users with tokens."""
import json
import os
import secrets

import httpx

BASE = "http://127.0.0.1:18003"
ROOT_FILE = "/tmp/cmp/newapi-root"
c = httpx.Client(base_url=BASE, trust_env=False, timeout=30)


def ok(response):
    try:
        body = response.json()
    except ValueError:
        raise SystemExit(f"{response.request.method} {response.request.url} {response.status_code}: {response.text[:300]}")
    if not body.get("success"):
        raise SystemExit(f"{response.request.url}: {body}")
    return body


def login(username, password, user_id):
    data = ok(c.post("/api/user/login", json={"username": username, "password": password}))["data"]
    return {"Authorization": "Bearer " + data["access_token"], "New-Api-User": str(user_id)}


if os.path.exists(ROOT_FILE):
    password = open(ROOT_FILE).read()
else:
    password = secrets.token_hex(8) + "Aa1!"
    open(ROOT_FILE, "w").write(password)
    ok(c.post("/api/setup", json={"username": "root", "password": password, "confirmPassword": password,
                                  "SelfUseModeEnabled": False, "DemoSiteEnabled": False}))
admin = login("root", password, 1)
ok(c.post("/api/channel/", headers=admin, json={"mode": "single", "channel": {
    "type": 1, "name": "bench", "key": "sk-upstream", "base_url": "http://127.0.0.1:18001",
    "models": "bench-model", "group": "default"}}))
# 1 ratio = 2 USD per million input tokens; 2.5 / 10 USD like the Five run.
ok(c.put("/api/option/", headers=admin, json={"key": "ModelRatio", "value": json.dumps({"bench-model": 1.25})}))
ok(c.put("/api/option/", headers=admin, json={"key": "CompletionRatio", "value": json.dumps({"bench-model": 4})}))

tokens = []
for index in range(16):
    name = f"bench{index}"
    user_password = secrets.token_hex(8) + "Aa1!"
    ok(c.post("/api/user/", headers=admin, json={"username": name, "password": user_password, "display_name": name}))
    users = ok(c.get("/api/user/search", headers=admin, params={"keyword": name}))["data"]
    users = users.get("items", users) if isinstance(users, dict) else users
    user = next(u for u in users if u["username"] == name)
    user["quota"] = 500_000_000_000
    user["password"] = ""
    ok(c.put("/api/user/", headers=admin, json=user))
    as_user = login(name, user_password, user["id"])
    ok(c.post("/api/token/", headers=as_user, json={"name": "bench", "remain_quota": 0, "expired_time": -1,
                                                   "unlimited_quota": True}))
    listed = ok(c.get("/api/token/", headers=as_user))["data"]
    listed = listed.get("items", listed) if isinstance(listed, dict) else listed
    key = listed[0]["key"]
    if "*" in key:
        key = ok(c.post(f"/api/token/{listed[0]['id']}/key", headers=as_user))["data"]["key"]
    tokens.append(key if key.startswith("sk-") else "sk-" + key)
open("/tmp/cmp/newapi-keys", "w").write("\n".join(tokens) + "\n")
print("tokens", len(tokens))
