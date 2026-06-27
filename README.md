# Bookkeeping Agent

Standalone Frappe app for the ERPNext Bookkeeping Agent.

This app provides:

- Desk page at `/app/bookkeeping-agent`
- Server-side OpenAI Responses API orchestration
- ERPNext bookkeeping read/write tools
- Pending-action confirmation flow for accounting writes
- Bookkeeping Agent memory DocTypes

ERPNext must already be installed on the site.

## Install In A Bench

```bash
cd /workspace/development/frappe-bench
bench get-app git@github.com:jacksmies/bookkeeping-agent.git --branch main
bench --site development.localhost install-app bookkeeping_agent
bench --site development.localhost migrate
bench build --app bookkeeping_agent
```

## Docker Build Apps File

For `frappe_docker`, install official ERPNext first, then this app:

```json
[
  {
    "url": "https://github.com/frappe/erpnext.git",
    "branch": "develop"
  },
  {
    "url": "git@github.com:jacksmies/bookkeeping-agent.git",
    "branch": "main"
  }
]
```

Use matching Frappe and ERPNext branches for your target version.

## License

GNU General Public License v3 or later.
