# DX hook — CRITICAL step removed in five minutes

Shows the first-five-minutes story without cryptography:

```text
REGRESSION / CRITICAL step removed: verify_account_status
```

## Fastest path

From a clean directory:

```bash
pip install dprovenancekit
dpk init my-agent && cd my-agent
python agent_stub.py
```

## From this checkout

```bash
pip install -e .
python -m examples.e2e_decision_path
# or the scaffold:
dpk init /tmp/dpk-dx --force && python /tmp/dpk-dx/agent_stub.py
```

Cryptographic attestation is a later chapter (`dpk attest` / Swift SE). The hook
is the regression headline above.
