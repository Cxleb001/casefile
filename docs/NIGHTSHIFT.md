# Meterless Nightshift as Casefile's AI provider

The hackathon requires **Meterless Nightshift as the primary AI provider**, with NVIDIA models connected through
Nightshift. Casefile talks to Nightshift's Meterless Runtime as an OpenAI-compatible provider, following the
[Meterless provider contract](https://github.com/meterless/meterless/blob/main/docs/agent-ready/add-meterless-provider.md):
Bearer token, `meterless-app: casefile` on every request, `meterless-scope: user` on chat, routing-mode model ids
(`meterless/text.fast`, `meterless/text.deep`, ...), a 180 s timeout, and status from `GET /v1/models`.

Nightshift runs on your own computer (Windows, Mac or DGX edition). Casefile and Zetaris run in the Codespace.
A one-command SSH tunnel from your computer makes Nightshift appear inside the Codespace at `127.0.0.1:8088`.

## 1. Nightshift on your computer

1. Install Nightshift (<https://www.meterless.ai/windows.html> on Windows) and open it.
2. Add your **NVIDIA key** in Nightshift's model settings, so its cloud lanes can use NVIDIA's models
   (Nemotron). The key stays in Nightshift; Casefile never sees it.
3. Copy Nightshift's **connect link** (hotspot / connect panel, `http://<host>:<port>/connect#k=<key>`), or its
   access token from the `proxy-token` file in its state folder.

## 2. Open the tunnel (on your computer, keep the window open)

In PowerShell:

```powershell
winget install --id GitHub.cli          # once; then open a new PowerShell window
gh auth login                           # once; choose GitHub.com and sign in with the browser
gh auth refresh -h github.com -s codespace
gh codespace list                       # copy your Codespace's NAME
gh codespace ssh -c NAME -- -N -R 8088:127.0.0.1:8088
```

The last command prints nothing and keeps running: that is the tunnel. If Nightshift uses another port, use it on
both sides (`-R 8090:127.0.0.1:8090`).

## 3. Point Casefile at it (in the Codespace)

```bash
python scripts/codespace/setup_env.py --nightshift   # paste the connect link (or the address and token)
python scripts/check_nightshift.py                   # ONLINE, the routing modes, and one test reply
```

`check_nightshift.py` reports `UNAUTHORIZED` for a wrong token (not "offline"), auto-detects ports 8088, 8090,
8787 and 11434, and shows which model actually served the test request.

Casefile's steps map to routing modes by default: the investigation loop on `meterless/text.fast`, the report and
the claim verifier on `meterless/text.deep`. Override any of them with `CASEFILE_MODEL_PLAN`, `CASEFILE_MODEL_REPORT`
or `CASEFILE_MODEL_VERIFY` in `.env`, using an id from the check's list.

## Without Nightshift

`CASEFILE_LLM_PROVIDER=nvidia` with `NVIDIA_API_KEY` calls NVIDIA's API directly (development only), and
`--rules` / sample mode run with no model at all.
