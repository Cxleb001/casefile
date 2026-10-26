# Run everything in a GitHub Codespace (no Docker on your laptop)

A Codespace is a Linux computer in the cloud that you use from your browser. It already has Docker, so Zetaris runs there instead of on your laptop. GitHub's free plan includes 120 core-hours a month, which is about 30 hours on the 4-core / 16 GB machine Zetaris needs. Usage simply stops at the limit if you have no card on file, so there are no surprise charges. **Stop the Codespace whenever you take a break** (step 10).

Each step ends with what you should see. If you don't see it, copy the terminal output into the chat.

---

### 1. Create the Codespace (5 minutes)

1. Open <https://github.com/Cxleb001/casefile>.
2. Click the green **Code** button → **Codespaces** tab → the **⋯** menu → **New with options**.
3. Set **Machine type** to **4-core** (16 GB RAM). Leave everything else as it is, then click **Create codespace**.
4. Wait for the editor to load, and for the terminal at the bottom to finish installing packages (2–4 minutes).

**You should see** a VS Code-style editor in your browser, with the file list on the left and a terminal at the bottom. If there's no terminal, use **☰ menu → Terminal → New Terminal**.

### 2. Connect the models through Meterless Nightshift (10 minutes)

Casefile's AI provider is Meterless Nightshift, running on your laptop with your NVIDIA key stored inside it. Follow [NIGHTSHIFT.md](NIGHTSHIFT.md): install Nightshift, add the NVIDIA key in Nightshift, open the tunnel from your laptop, then run:

```bash
python scripts/codespace/setup_env.py --nightshift
python scripts/check_nightshift.py
```

**You should see** `status: ONLINE` and a `[PASS]` line naming the NVIDIA model that answered.

> No models yet? Skip this step: everything up to step 8 works rules-only, and `SAMPLE_MODE=true` replays a recorded run (see [sample/README.md](../sample/README.md)). For development you can also call NVIDIA directly with `python scripts/codespace/setup_env.py --models`.

### 3. Download the real data (10 minutes)

1. On kaggle.com, open your profile → **Settings** → **API** → **Generate New Token**, and copy the token.
2. Run the command below and paste the token when asked. It's hidden as you type and stored only in this Codespace.
3. ```bash
   bash scripts/codespace/get_data.sh
   ```
4. Run the command it prints at the end. It starts with `.venv/bin/python scripts/prepare_data.py ...`.

**You should see** the data prep work through its 7 steps and finish with `Done in ...s` and a list of row counts.

### 4. Check the agent on the real data (1 minute)

```bash
python scripts/check_setup.py
python scripts/run_eval.py --rules --limit 10
```

**You should see** "All checks passed" and a results table. This runs on the built-in stand-in for now. Zetaris comes next.

### 5. Start Zetaris (20–30 minutes, mostly waiting)

1. On the Zetaris **Run locally** page, download **zetaris-platform.zip** to your laptop.
2. Drag it into the Codespace file list, at the top `casefile` level.
3. On the same page, copy the **`docker login zetarishackathon.azurecr.io ...`** command. Paste it into the Codespace terminal and enter the username and password it shows.
4. Run:

```bash
bash scripts/codespace/start_zetaris.sh --browser
```

**You should see** the images download (about 10 GB), then `Bootstrap finished.`, the `verify.sh` checks passing, and finally `Zetaris is running.`

### 6. Load the two banks' data and connect it to Zetaris (5 minutes)

```bash
bash scripts/codespace/connect_sources.sh
python scripts/publish_local.py load --yes
python scripts/codespace/setup_env.py --zetaris
python scripts/zetaris_apply.py
```

When asked, use the Zetaris sign-in from the Run locally page (step 7 there: `admin@zetaris.com` and its password).

**You should see** `organisation id 1 works; saved`, seven registration steps marked `ok`, then six `[PASS]` checks, including **PII withheld** with no `full_name` column.

### 7. Switch Casefile to Zetaris (1 minute)

Open `.env` in the file list and set:

```
CASEFILE_DATA_BACKEND=zetaris
```

Then run:

```bash
python scripts/check_setup.py
```

**You should see** five tables with row counts, the federated query (A's ledger × B's ledger × B's governed view) returning rows, a Nemotron reply, and "All checks passed."

### 8. Open the app (1 minute)

```bash
streamlit run app.py
```

A pop-up offers **Open in Browser**. Click it, or open the **Ports** tab and click the globe next to port 8501. Pick an alert and press **Investigate**.

To show the Zetaris web UI as well (useful for the demo video), open the **Ports** tab and click the globe next to port 3000.

### 9. Measure it

```bash
python scripts/run_eval.py --rules --single-bank
python scripts/run_eval.py --rules
python scripts/run_eval.py
```

### 10. Stop when you take a break

Go to <https://github.com/codespaces>, click **⋯** next to your Codespace, then **Stop codespace**. To come back, open it again from the same page and run:

```bash
bash scripts/codespace/resume.sh
```

Your files, the data and Zetaris's registrations are kept while the Codespace is stopped. Only **deleting** it removes them.

---

**Stuck?** Copy the full terminal output into the chat. For Zetaris install problems, you can also post your OS (`Linux, GitHub Codespace`), the command and the error in Zetaris's HackOS Help desk.
