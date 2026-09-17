# Deploy to Railway

1. Push this repo to GitHub.
2. Go to https://railway.app and create a new project.
3. Choose **Deploy from GitHub repo** and select `dual-lobe-proxy`.
4. Railway will detect `railway.json` and use the Dockerfile.
5. Add a **PostgreSQL** service to the project (Railway provisions one).
6. In the dual-lobe service Variables tab, add:
   - `DUAL_LOBE_A_API_KEY`
   - `DUAL_LOBE_B_API_KEY`
   - `DUAL_LOBE_A_MODEL` (e.g. `Qwen/Qwen3-Max`)
   - `DUAL_LOBE_B_MODEL` (e.g. `Qwen/Qwen3-Max`)
   - `DUAL_LOBE_A_BASE_URL` = `https://api.deepinfra.com/v1/openai`
   - `DUAL_LOBE_B_BASE_URL` = `https://api.deepinfra.com/v1/openai`
   - `RLS_DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `DUAL_LOBE_ROUTING_MODE` = `off`
   - `DUAL_LOBE_HIERARCHY_ROLES` = `sawii/dl-dialogue1:0,sawii/dl-dialogue2:1,sawii/dl-dialogue3:2`
7. Deploy. Railway will expose a public URL like `https://dual-lobe-proxy.up.railway.app`.
8. Test with `curl https://<your-url>/healthz`.

The service listens on `$PORT` (set by Railway).
