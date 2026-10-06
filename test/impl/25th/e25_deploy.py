"""Epic 25 스택 시험 — 배포 고도화: 카나리 가중 라우팅·승격·standby 롤백·오류율 자동 롤백·드리프트·블루그린·평가 게이트·
배포 정책(digest·레지스트리·ed25519 서명·승인)·환경 승격(승인함)·스케일·k8s 매니페스트·토폴로지·이벤트·감사.
대상 에이전트 = 가짜 A2A 에이전트(fake-upstream /agent/<name>/<ok|fail>/<version>) — 카드 이름·버전을 시험이 정한다.
실행: cd agents/runtime && uv run --frozen python ../../test/impl/25th/e25_deploy.py <local|prod>"""
import base64
import json
import os
import secrets
import subprocess
import sys
import time

import httpx
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

MODE = sys.argv[1] if len(sys.argv) > 1 else "local"
M = os.environ.get("ATLAS_MANAGER_HOST_URL", "http://127.0.0.1:8080")
MH = {"Authorization": f"Bearer {os.environ['ATLAS_TEST_TOKEN']}"} if os.environ.get("ATLAS_TEST_TOKEN") else {}
SVC = {"Authorization": f"Bearer {os.environ['ATLAS_MANAGER_BOOTSTRAP_KEY']}"} if MODE == "prod" else {}   # 승격 요청자 ≠ 승인자
N = secrets.token_hex(3)
NAME, PROJ = f"canary-{N}", "rollout-e25"
F = "http://fake-upstream:8000/agent"
c = httpx.Client(timeout=120, headers=MH)
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


def wait(fn, timeout=60, every=1.5):
    end = time.time() + timeout
    while True:
        v = fn()
        if v or time.time() > end:
            return v
        time.sleep(every)


def dep(did):
    return c.get(f"{M}/api/v1/deployments/{did}").json()


def manifest(version, image=None, sig=None):
    m = {"apiVersion": "agents.prodai.io/v1alpha1", "kind": "AtlasAgent", "metadata": {"name": NAME, "unitId": "AL-PLAN"},
         "spec": {"version": version, "interfaces": [{"binding": "JSONRPC", "port": 8000, "path": "/a2a"}],
                  "agentCard": {"skills": ["loop.plan"]}, "drivers": {"supported": ["compose", "kubernetes"]}}}
    if image:
        m["spec"]["artifacts"] = {"image": image, **({"signature": sig} if sig else {})}
    return m


def subjects():
    out = subprocess.run(["docker", "run", "--rm", "--network", f"prodai-atlas-{MODE}_default", "natsio/nats-box:0.18.1", "nats", "-s", "nats://nats:4222",
                          "stream", "subjects", "PLT", "--json"], capture_output=True, text=True, timeout=90).stdout
    return json.loads(out or "{}")


def call(key):
    r = c.post(f"{M}/api/v1/agents/{AID}/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}}, headers={"x-atlas-assign-key": key})
    return r.status_code, r.headers.get("x-atlas-deployment-slot")


c.post(f"{M}/api/v1/orgs/default/projects", json={"slug": PROJ, "name": PROJ})
AID = c.post(f"{M}/api/v1/agents", params={"project": PROJ}, json=manifest("0.1.0")).json()["id"]
d0 = c.post(f"{M}/api/v1/deployments", json={"agentId": AID, "endpoint": f"{F}/{NAME}/ok/0.1.0"}).json()
wait(lambda: dep(d0["id"])["status"] == "ready")

# E25-01 카나리 시작 · 가중 라우팅(고정 배정) (DEP-05)
ro = c.post(f"{M}/api/v1/agents/{AID}/rollouts", json={"endpoint": f"{F}/{NAME}/ok/0.1.0/", "steps": [30, 60, 100]}).json()
dup = c.post(f"{M}/api/v1/agents/{AID}/rollouts", json={"endpoint": f"{F}/{NAME}/ok/0.1.0"}).status_code
wait(lambda: dep(ro["toDeploymentId"])["status"] == "ready")
split = [call(f"user-{i}")[1] for i in range(120)]
sticky = {call("vip-1")[1] for _ in range(5)}
check("E25-01", (ro["status"], ro["weight"], dup, 15 <= split.count("canary") <= 60, split.count("primary") >= 50, len(sticky)),
      ("running", 30, 409, True, True, 1))

# E25-02 단계 전진(60%) → 승격 → 이전 primary standby · standby 즉시 롤백 (DEP-05·07)
mid = c.post(f"{M}/api/v1/rollouts/{ro['id']}/advance").json()
pr = c.post(f"{M}/api/v1/rollouts/{ro['id']}/advance").json()
after = {d["slot"]: d["id"] for d in c.get(f"{M}/api/v1/deployments", params={"agentId": AID}).json()["items"] if d["status"] != "stopped"}
rb = c.post(f"{M}/api/v1/deployments/{after.get('primary')}/rollback", json={}).json()
check("E25-02", (mid["weight"], pr["status"], after.get("primary") == ro["toDeploymentId"], after.get("standby") == d0["id"], rb.get("mode"), rb.get("primary", {}).get("id") == d0["id"]),
      (60, "promoted", True, True, "standby_swap", True))

# E25-03 오류 canary → 자동 롤백 · 이후 primary 로만 (DEP-05)
r2 = c.post(f"{M}/api/v1/agents/{AID}/rollouts", json={"endpoint": f"{F}/{NAME}/fail/0.1.0", "steps": [100], "gate": {"minCalls": 3, "maxErrorRate": 0.5}}).json()
wait(lambda: dep(r2["toDeploymentId"])["status"] == "ready")
codes = [call(f"k{i}")[0] for i in range(4)]
rolled = wait(lambda: c.get(f"{M}/api/v1/rollouts/{r2['id']}").json()["status"] == "rolled_back", 40)
reason = c.get(f"{M}/api/v1/rollouts/{r2['id']}").json().get("reason", "")
check("E25-03", (502 in codes or 500 in codes, bool(rolled), "오류율" in reason, {call(f"z{i}")[1] for i in range(5)}), (True, True, True, {"primary"}))

# E25-04 드리프트 — 카드 version ≠ 매니페스트 (DEP-09)
r3 = c.post(f"{M}/api/v1/agents/{AID}/rollouts", json={"endpoint": f"{F}/{NAME}/ok/9.9.9", "steps": [10, 100]}).json()
drift = wait(lambda: dep(r3["toDeploymentId"]).get("drift"), 30) or ""
c.post(f"{M}/api/v1/rollouts/{r3['id']}/abort", json={"reason": "드리프트"})
check("E25-04", ("9.9.9" in drift, c.get(f"{M}/api/v1/rollouts/{r3['id']}").json()["status"]), (True, "rolled_back"))

# E25-05 블루그린 — green(0%) 준비 → 전환 (DEP-06)
bg = c.post(f"{M}/api/v1/agents/{AID}/rollouts", json={"strategy": "bluegreen", "endpoint": f"{F}/{NAME}/ok/0.1.0/bg"}).json()
wait(lambda: dep(bg["toDeploymentId"])["status"] == "ready")
pre = {call(f"b{i}")[1] for i in range(10)}
sw = c.post(f"{M}/api/v1/rollouts/{bg['id']}/advance").json()
post = {call(f"b{i}")[1] for i in range(10)}
check("E25-05", (bg["weight"], pre, sw["status"], post), (0, {"primary"}, "promoted", {"primary"}))

# E25-06 평가 게이트 — 통과 스위트 · 실패 스위트 → 롤백 (DEP-05 · EV-REG)
subprocess.run(["bash", "test/impl/14th/dc.sh", MODE, "exec", "-T", "agent-ev", "python", "-m", "units.ev.cli", "seed", "--dataset", "planner-golden", "--file",
                "evals/planner-golden.jsonl", "--approve", "--snapshot", "--input-schema", '{"type":"object","required":["goal"],"properties":{"goal":{"type":"string"}}}'],
               cwd=os.path.join(os.path.dirname(__file__), "../../.."), capture_output=True, timeout=300)


def suite(pattern):
    return {"name": f"canary-gate-{N}", "dataset": {"name": "planner-golden"}, "epochs": 1, "thresholds": {"pass_rate": 1.0},
            "targets": [{"id": "canary", "type": "prompt", "model": "chat-default", "template": "{{input.goal}} @ {{canary.endpoint}}"}],
            "scorers": [{"type": "regex", "pattern": pattern}]}


g1 = c.post(f"{M}/api/v1/agents/{AID}/rollouts", json={"endpoint": f"{F}/{NAME}/ok/0.1.0/g1", "steps": [20, 100], "gate": {"suite": suite(".")}}).json()
wait(lambda: dep(g1["toDeploymentId"])["status"] == "ready")
a1 = c.post(f"{M}/api/v1/rollouts/{g1['id']}/advance").json()
g2 = c.post(f"{M}/api/v1/agents/{AID}/rollouts", json={"endpoint": f"{F}/{NAME}/ok/0.1.0/g2", "steps": [20, 100], "gate": {"suite": suite("NEVER-^$")}}).json()
wait(lambda: dep(g2["toDeploymentId"])["status"] == "ready")
a2 = c.post(f"{M}/api/v1/rollouts/{g2['id']}/advance")
check("E25-06", ([h["event"] for h in a1["history"]][-2:], a1["weight"], a2.status_code, "평가 게이트" in a2.json().get("message", ""),
                 c.get(f"{M}/api/v1/rollouts/{g2['id']}").json()["status"]), (["eval_passed", "promoted"], 100, 409, True, "rolled_back"))

# E25-07 배포 정책 — digest·레지스트리·서명·승인 (DEP-10)
key = Ed25519PrivateKey.generate()
pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
c.put(f"{M}/api/v1/orgs/default/deploy-policies/staging", json={"requireDigest": True, "allowedRegistries": ["ghcr.io/prodai"], "requireSignature": True,
                                                                 "trustedKeys": {"k-e25": pem}, "requireApproval": True}).raise_for_status()
deny = c.post(f"{M}/api/v1/agents/{AID}/promote", json={"from": "dev", "to": "staging"}, headers=SVC or MH)
img = "ghcr.io/prodai/al-plan@sha256:" + "b" * 64
dg = c.post(f"{M}/api/v1/manifests/digest", json=manifest("0.1.0", img)).json()
sig = {"kid": "k-e25", "sig": base64.b64encode(key.sign(dg["message"].encode())).decode()}
c.patch(f"{M}/api/v1/agents/{AID}", json=manifest("0.1.0", img, sig)).raise_for_status()
cur = c.post(f"{M}/api/v1/deployments", json={"agentId": AID, "endpoint": f"{F}/{NAME}/ok/0.1.0"}).json()
wait(lambda: dep(cur["id"])["status"] == "ready" and dep(cur["id"])["rev"] == 2)
check("E25-07", (deny.status_code, sorted(deny.json()["details"]["violations"])[:1] != [], len(deny.json()["details"]["violations"]), dg["message"].startswith("atlas-manifest/v1|")),
      (422, True, 3, True))

# E25-08 환경 승격 — 승인함(source=deploy) → 승인 → staging primary = rev 2 (DEP-08 · X-HITL)
pm = c.post(f"{M}/api/v1/agents/{AID}/promote", json={"from": "dev", "to": "staging", "reason": "릴리스 0.1.0"}, headers=SVC or MH).json()
ap = c.get(f"{M}/api/v1/approvals/{pm.get('approvalId')}").json().get("approval", {})
dec = c.post(f"{M}/api/v1/approvals/{pm.get('approvalId')}/decision", json={"decision": "approve", "reason": "검토 완료"}).json()
stg = next((d for d in c.get(f"{M}/api/v1/deployments", params={"agentId": AID}).json()["items"] if d["env"] == "staging" and d["status"] != "stopped"), {})
skip = c.post(f"{M}/api/v1/agents/{AID}/promote", json={"from": "dev", "to": "prod"}).status_code
check("E25-08", (pm.get("status"), ap.get("action"), ap.get("source"), str(dec.get("callbackResult", "")).startswith("promoted:"), stg.get("rev"), skip),
      ("pending_approval", "deploy.promote", "deploy", True, 2, 422))

# E25-09 스케일 · k8s 매니페스트 렌더 (MGR-07 · DEP-03)
sc = c.put(f"{M}/api/v1/deployments/{stg.get('id')}/scale", json={"replicas": 3}).json()
docs = list(yaml.safe_load_all(c.get(f"{M}/api/v1/deployments/{stg.get('id')}/manifests").text))
kd = next(x for x in docs if x["kind"] == "Deployment")
ctr = kd["spec"]["template"]["spec"]["containers"][0]
check("E25-09", (sc["desired"], sc["applied"], [x["kind"] for x in docs], kd["spec"]["replicas"], kd["metadata"]["namespace"], ctr["image"] == img,
                 ctr["securityContext"]["readOnlyRootFilesystem"], kd["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"]),
      (3, False, ["Deployment", "Service"], 3, "atlas-staging", True, True, True))

# E25-10 토폴로지 — 배포 노드·관찰 간선 (MGR-09)
topo = c.get(f"{M}/api/v1/topology", params={"env": "dev"}).json()
node = next((n for n in topo["nodes"] if n["id"] == AID), {})
edge = [e for e in topo["edges"] if e["callee"] == AID]
check("E25-10", ({d["slot"] for d in node.get("deployments", [])} >= {"primary"}, bool(edge), sum(e["calls"] for e in edge) >= 150, sum(e["errors"] for e in edge) >= 3),
      (True, True, True, True))

# E25-11 이벤트 · 감사
subj = subjects()
want = ["plt.deploy.rollout.started", "plt.deploy.rollout.advanced", "plt.deploy.rollout.promoted", "plt.deploy.rollout.rolled_back", "plt.deploy.rolled_back",
        "plt.deploy.drift", "plt.deploy.promoted", "plt.deploy.scaled", "plt.deploy.policy_denied"]
aud = {a["action"] for a in c.get(f"{M}/api/v1/orgs/default/audit", params={"limit": 300}).json().get("items", [])}
check("E25-11", ([s for s in want if s not in subj], sorted(a for a in ("deploy.rollout.start", "deploy.rollout.promote", "deploy.rollout.rollback", "deploy.rollback",
                                                                         "deploy.promote", "deploy.scale", "deploy.policy_denied", "deploy.policy.update") if a not in aud)),
      ([], []))

for _ in range(5):   # 정리 — 남으면 다음 실행의 E2-07(AL-PLAN 에이전트 목록)이 깨진다 (prod DB 볼륨은 실행 간 유지)
    dr = c.delete(f"{M}/api/v1/agents/{AID}")
    if dr.status_code in (204, 404):
        break
    print(f"cleanup retry: {dr.status_code} {dr.text[:200]}", flush=True)
    time.sleep(3)
print(f"== {MODE}: failed={failed}")
sys.exit(1 if failed else 0)
