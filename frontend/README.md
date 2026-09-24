# InvoiceOps UI

Vite, React, TypeScript, Mantine, TanStack Query, and React Router form the
single-page application shell. Its six routed workspaces are added in plan
steps 3.5–3.10. The current shell shows the routes and API liveness.

```sh
cd frontend
npm ci
npm run generate:api
npm run typecheck
npm run dev
```

The Vite server proxies `/v1`, `/healthz`, and `/readyz` to
`http://127.0.0.1:8000` by default. Set `VITE_API_PROXY_TARGET` to a different
server only for local development. Compose runs the UI with
`docker compose --profile ui up --build`, then serves it at
`http://127.0.0.1:5173`.

`npm run generate:api` exports FastAPI's local OpenAPI contract to
`openapi.json` and regenerates `src/generated/api.ts`. Both files are checked
in, and CI verifies that generation produces no diff. The runtime client is
typed from those generated paths and parses every response with the matching
Zod contract before it reaches components.

Select Maria, Dan, Priya, or Platform Engineer in the sidebar. Maria and Dan
can see the operational queue; Priya sees the audit and evaluation routes.
Enter the matching persona token to make protected API reads. Tokens stay in
the current tab's memory and are never bundled into the app or persisted in
browser storage. Switching persona or changing a token clears TanStack Query's
cache, while the API enforces the actual role permissions.
