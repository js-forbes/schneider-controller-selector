# schneider-controller-selector

Schneider Electric MP-C Controller Selector. Select point requirements, buffers and eligible controller models. The selector finds a controller combination using built-in I/O only.

Selections are stored in separate SQLite workspaces. Create a workspace to generate a random 8-character code, or enter a code to reconnect to an existing workspace. To choose a code yourself, enter exactly 8 printable Unicode characters without spaces and select **Create custom code**. Codes are case-insensitive and remembered in the current browser. Anyone with a code can read, change, or delete selections in that workspace, so share it only with people you trust. The server stores a hash of each code and limits repeated invalid-code attempts. This provides lightweight shared access, not account-level security.

The first workspace created copies selections from the earlier unpartitioned SQLite table. When creating a new workspace, existing browser-local selections are also copied into it. Use **Export database** and **Import database** to back up or transfer selections as a portable JSON file.

Run the app and SQLite API locally with Python 3:

```sh
python3 controller_selector_server.py
```

Then open `http://localhost:8000/`. The workspace data is kept in the local `saved_selections.sqlite3` file. That file is ignored by Git because the repository is public; do not commit it. Use the JSON export/import buttons when you need to move saved selections.

## Railway API and GitHub Pages

The Pages workflow publishes the static selector. The workspace API must run separately because Pages cannot run Python or SQLite. To host the full app:

1. Deploy this repository as a Python service on Railway. The included `railway.toml` starts the API and configures its health check.
2. Attach a persistent Railway volume at `/data`. Set `DATABASE_PATH=/data/saved_selections.sqlite3` and `ALLOWED_ORIGINS=https://js-forbes.github.io`. Optionally set `BOOTSTRAP_CODES` to a comma-separated list of workspaces to create, for example `TESTPAGE,ICIX2026,ICI20261,ICI20262,ICI20263,ICI20264,ICI20265,ICIJAKE1`. `PORT` is supplied by Railway. The service stores only code hashes in its private volume.
3. Generate a Railway public domain and add a repository Actions variable named `API_BASE_URL` with that HTTPS origin (for example, `https://your-service.up.railway.app`).
4. Set **Settings → Pages → Build and deployment → Source** to **GitHub Actions**. Push to `main` to deploy the site; the workflow embeds `API_BASE_URL`. You can also start a deployment from **Actions → Deploy to GitHub Pages → Run workflow**.

Do not expose the built-in HTTP server directly to an untrusted network without HTTPS. Workspace codes are bearer credentials: anyone who knows one can read, change, or delete data in that workspace. `TESTPAGE` is a public, predictable default and must only be used for non-sensitive test data. Export and import selections to migrate data from the local database; do not commit the SQLite file to this public repository.

## GitHub Pages

The deployment workflow runs on every push to `main` and can be run manually. The Pages site and Railway API require separate deployments.
