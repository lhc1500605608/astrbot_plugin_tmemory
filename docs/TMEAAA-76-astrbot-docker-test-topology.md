# TMEAAA-76 AstrBot Docker Test Topology And Verification

## Scope

This note defines the minimum reproducible Docker topology for validating `astrbot_plugin_tmemory` against a real AstrBot runtime, and separates checks that are already executable now from checks that still depend on later integration setup.

## Minimum Test Topology

### Components

1. Host workspace: `/Users/tango/Documents/paperclip/astrbot_plugin_tmemory`
2. Docker container: `astrbot_tmemory_test`
3. Container image: `soulter/astrbot:nightly-latest`
4. Host test data root: `/tmp/astrbot_test_data`
5. Container data root: `/AstrBot/data`
6. Plugin install path in container: `/AstrBot/data/plugins/astrbot_plugin_tmemory`

### Network Relation

1. Local AstrBot default port remains reserved on host `6185`
2. Docker test container publishes host `6186` to container `6185`
3. Validation target for WebUI readiness is `http://localhost:6186/`

This topology is sufficient to verify that the Dockerized AstrBot instance is isolated from an existing local AstrBot process while still exposing the real application surface needed for plugin loading and pre-integration checks.

## Topology Source Of Truth

The current topology is implemented by `tools/docker_test_env.sh`.

Key behavior in that script:

1. Copies the current repo into `/tmp/astrbot_test_data/plugins/astrbot_plugin_tmemory`
2. Starts container `astrbot_tmemory_test`
3. Maps host `6186` to container `6185`
4. Mounts `/tmp/astrbot_test_data` into `/AstrBot/data`
5. Waits for HTTP readiness on `http://localhost:6186/`

## Verified Current State

The following checks were executed during this QA pass.

| Check | Result | Evidence |
|---|---|---|
| Real-AstrBot import/initialization tests | PASS | `pytest -q tests/test_real_astrbot_integration.py` returned `2 passed in 4.62s` |
| Docker test container online | PASS | `./tools/docker_test_env.sh status` showed `astrbot_tmemory_test` as `Up` |
| Port isolation active | PASS | `docker ps` showed `0.0.0.0:6186->6185/tcp` |
| WebUI reachable from host | PASS | `curl -I http://localhost:6186/` returned `HTTP/1.1 200` |
| Plugin directory present in container | PASS | `docker exec astrbot_tmemory_test ls -la /AstrBot/data/plugins` listed `astrbot_plugin_tmemory` |
| Plugin files mounted into container | PASS | `docker exec astrbot_tmemory_test ls -la /AstrBot/data/plugins/astrbot_plugin_tmemory` showed repo contents including `main.py`, `metadata.yaml`, `tests`, and `tools` |

## Step-By-Step Verification Checklist

## Standard Full E2E Procedure After TMEAAA-166

Use this procedure for every future functional test that must validate real
AstrBot chat behavior, LLM-backed distillation, or command/capture boundaries.
This is the verified replacement for unit-test-only validation when the issue
requires a live AstrBot runtime.

### Prerequisites

1. Keep Docker test files in the workspace: `docker-compose.yml`,
   `docker/astrbot_init.sh`, and `docker/e2e_verify.sh`.
2. Store the provider key in a local ignored `.env` file or runtime environment,
   never in tracked files or issue comments.
3. Confirm `.env` is ignored before use:

```bash
git check-ignore .env
```

4. Use this `.env` shape:

```bash
DEEPSEEK_API_KEY=<runtime key>
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
TZ=Asia/Shanghai
```

### Start And Smoke Test

Run Docker with the gitignored `.env` value. If the heartbeat environment has a
placeholder or empty key, explicitly unset it so Compose reads `.env`:

```bash
env -u DEEPSEEK_API_KEY docker compose up -d --force-recreate
./docker/e2e_verify.sh
```

Expected `e2e_verify.sh` result:

1. AstrBot container is running
2. AstrBot WebUI is reachable at `http://localhost:6186/`
3. `tmemory` initializes
4. DeepSeek LLM call succeeds

### Required AstrBot API Scenario

Use AstrBot OpenAPI, not a direct Python-only fixture, for final E2E evidence.
The verified route is:

1. Authenticate or create an API key with chat scope
2. Send chat requests through `POST /api/v1/chat`
3. Use a unique `session_id` per test run

Minimum scenario:

```text
/style_distill off
<normal style-rich chat containing an OFF marker>
/style_distill on
<normal style-rich chat containing an ON marker>
```

Expected command responses:

1. `/style_distill off` returns `风格蒸馏采集已关闭（不影响普通记忆整理）。`
2. `/style_distill on` returns `风格蒸馏采集已开启（不影响普通记忆整理）。`
3. Normal chat returns non-empty multi-sentence assistant content, not the
   old usage/status-only response.

### Required Database Evidence

Inspect `/AstrBot/data/plugin_data/astrbot_plugin_tmemory/tmemory.db` inside the
container before teardown.

Required checks:

1. `conversation_cache` contains the normal user/assistant chat rows for both
   off/on markers.
2. `/style_distill` command text does not appear in `conversation_cache`.
3. QA marker text and command text do not appear in long-term `memories`.
4. `distill_history`, `style_temp_profiles`, and `style_profiles` are queried
   and recorded, even when their expected count is zero for the scenario.
5. Final config state reflects the last command, e.g. `enable_style_distill=true`
   after the final `/style_distill on`.

### Teardown

Always stop the Docker environment after evidence collection to avoid leaving
the default local AstrBot WebUI exposed:

```bash
docker compose down
```

### Acceptance Gate

A future test using this procedure passes only when all of these are true:

1. `./docker/e2e_verify.sh` passes.
2. `/style_distill on/off` command responses are correct.
3. Normal chat produces non-empty assistant content.
4. Ordinary chat material is collected into `conversation_cache`.
5. Control commands do not enter `conversation_cache` or `memories`.
6. No provider secret is printed, committed, or written into tracked files.

If command replies are correct but DB command pollution reappears, treat that as
a failing regression. If DB pollution is fixed but assistant content is empty,
the live-provider path is not valid and the test is incomplete.

### A. Start Or Reconfirm The Docker Test Environment

Run:

```bash
./tools/docker_test_env.sh start
```

Expected result:

1. Script prints `Port mapping: host 6186 -> container 6185`
2. Script ends with `AstrBot WebUI ready at http://localhost:6186`
3. `docker ps` shows container name `astrbot_tmemory_test`

Fail criteria:

1. Container is not created
2. Readiness loop expires without HTTP success
3. Port is not `6186->6185`

### B. Confirm Port Isolation

Run:

```bash
./tools/docker_test_env.sh status
curl -I http://localhost:6186/
```

Expected result:

1. Container port exposure is `6186->6185`
2. HTTP response is `200`
3. Validation traffic does not require occupying local port `6185`

Pass interpretation:

The Docker AstrBot test surface is isolated enough to run in parallel with a host-side AstrBot instance using the default port.

### C. Confirm Plugin Payload Is Available To AstrBot

Run:

```bash
docker exec astrbot_tmemory_test ls -la /AstrBot/data/plugins
docker exec astrbot_tmemory_test ls -la /AstrBot/data/plugins/astrbot_plugin_tmemory
```

Expected result:

1. `astrbot_plugin_tmemory` exists under `/AstrBot/data/plugins`
2. Plugin directory contains `main.py` and `metadata.yaml`
3. Plugin content matches the working repo copy rather than a stale image-layer artifact

### D. Verify Real AstrBot Compatibility At Python Level

Run:

```bash
pytest -q tests/test_real_astrbot_integration.py
```

Expected result:

1. `test_plugin_initializes_under_real_astrbot` passes
2. `test_plugin_is_discoverable_from_real_astrbot_plugin_directory` passes

What this proves:

1. `TMemoryPlugin.initialize()` and `terminate()` complete under a real AstrBot dependency set
2. AstrBot plugin manager can discover the plugin from the real plugin directory layout
3. `metadata.yaml` is valid enough for AstrBot loading and naming

### E. Pre-Integration Gate Before Full Functional Testing

A Docker topology is considered ready for later integration scenarios only if all checks above pass together.

Decision rule:

1. If A through D all pass, the environment is ready for plugin integration testing
2. If A or B fails, environment isolation is not trustworthy
3. If C fails, AstrBot is not testing the intended plugin payload
4. If D fails, later in-container interaction testing is not meaningful yet

## What Can Be Validated Right Now

These validations are executable and already supported by repository assets:

1. Docker container startup and teardown behavior
2. Host-to-container port isolation on `6186 -> 6185`
3. Host HTTP readiness of AstrBot WebUI
4. Correct plugin directory placement under AstrBot data volume
5. Real-AstrBot plugin discovery and plugin lifecycle initialization tests

## What Still Depends On Later Integration Work

These checks are not fully covered by the current topology alone:

1. End-to-end message ingestion through a live AstrBot adapter session
2. End-to-end memory write, recall, and injection behavior observed through real chat responses
3. Distillation behavior that requires enough live traffic to cross the default batch threshold
4. Vector search path validation with `enable_vector_search=true` and a working embedding provider
5. Security-focused checks around WebUI authentication, authorization, and IP whitelist behavior

## Sufficiency Assessment

### Sufficient For

This topology is sufficient for the following scope:

1. Verifying Docker runtime isolation for AstrBot
2. Verifying that the repo can be mounted/copied into AstrBot's plugin directory layout
3. Verifying that the plugin is structurally loadable by real AstrBot dependencies
4. Establishing a stable precondition for later plugin integration tests

### Not Sufficient For

This topology alone is not sufficient to declare full plugin integration complete, because it does not yet prove:

1. Live adapter-triggered read/write memory behavior
2. Production-like LLM-backed distillation and memory injection
3. Security and load behavior under concurrent traffic

## Precise Gaps To Fill Next

1. Add a scripted in-container validation that captures AstrBot logs and asserts `astrbot_plugin_tmemory` load success after container startup
2. Add one reproducible adapter-level scenario that sends messages through AstrBot and verifies `conversation_cache` or `memories` changes in the mounted database
3. Add a dedicated security checklist for WebUI auth-related settings before declaring WebUI production-ready
4. Add a vector-search integration scenario only when embedding provider credentials/test doubles are available

## Conclusion

The current Docker topology is valid as a minimum integration gate for `astrbot_plugin_tmemory`.

It is strong enough to support subsequent real-plugin integration work because it already proves:

1. port isolation is correct
2. AstrBot is reachable inside the Docker test path
3. the plugin payload is present at the expected AstrBot plugin location
4. real AstrBot dependencies can discover and initialize the plugin

It should be treated as a verified pre-integration environment, not as evidence that all runtime memory features have already been fully validated end to end.
