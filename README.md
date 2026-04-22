# WFC

A web app that runs the [Wave Function Collapse](https://github.com/mxgmn/WaveFunctionCollapse)
algorithm against a user-supplied seed image and returns a larger, procedurally-generated
tileable image.

## Architecture

```
 Browser
    |
    | upload image, poll status
    v
 [Frontend on App Engine] --write job doc--> [Firestore: wfc-db]
    |                                               ^
    | publish work order                            | status updates
    v                                               |
 [Pub/Sub: wfc-work-queue] --push (OIDC)--> [Worker on Cloud Run]
                                                    |
                                                    | read seed / write result
                                                    v
                                      [GCS: wfc-inputs, wfc-outputs]
```

- **Frontend** ([frontend/](frontend/)) — Flask on App Engine. Handles uploads,
  writes a job document to Firestore, publishes a Pub/Sub message, and serves a
  polling dashboard.
- **Worker** ([worker/](worker/)) — Flask on Cloud Run (8 GiB / 4 vCPU / 1 h timeout).
  Receives Pub/Sub push messages, runs the solver in a subprocess with a 5-minute
  circuit breaker, and writes the rendered PNG back to GCS.
- **Infrastructure** ([infrastructure/main.tf](infrastructure/main.tf)) — Terraform
  for buckets, Firestore, Pub/Sub, the Cloud Run invoker SA, and GitHub Actions
  Workload Identity Federation.

## The solver

The solver lives in [worker/main.py](worker/main.py) and is Numba-JIT-compiled.
Key pieces:

- **Pattern extraction** — extracts every N×N patch from the seed with toroidal
  wrap via `sliding_window_view`, dedups with `np.unique`, and builds the 4-direction
  adjacency rule table in a parallel `@njit` kernel (only dirs 0/1 computed;
  2/3 filled by symmetry).
- **Propagation** — uses per-cell compatibility counts so each removal event costs
  O(P) rather than O(P²). `support[y, x, t, d]` tracks how many live patterns at
  the direction-d neighbor still support pattern t here; when it hits zero, t is
  eliminated.
- **Cell selection** — Shannon entropy `H = log(Σw) − Σ(w·log w)/Σw`, with a tiny
  random jitter for tie-breaking. Collapse chooses a pattern weighted by its
  frequency in the seed.
- **Rendering** — numpy fancy indexing maps collapsed pattern indices to their
  top-left pixel.

## Repo layout

```
frontend/           App Engine app (upload UI + dashboard)
  app.yaml
  main.py
  requirements.txt
  templates/index.html
worker/             Cloud Run service (WFC solver)
  Dockerfile
  main.py
  requirements.txt
infrastructure/
  main.tf           All GCP resources as Terraform
.github/workflows/
  deploy-frontend.yml
  deploy-backend.yml
```

## Deploy

### 1. Provision infrastructure

```sh
cd infrastructure
terraform init
terraform apply \
  -var project_id=YOUR_PROJECT_ID \
  -var github_repo=your-org/your-repo \
  -var cloud_run_url=https://wfc-worker-<hash>-uw.a.run.app
```

The `cloud_run_url` is a chicken-and-egg: first deploy the worker (step 2) so
Cloud Run assigns a URL, then re-run `terraform apply` with that URL to wire up
the Pub/Sub push subscription. Terraform outputs the values to paste into the
GitHub Actions workflows (`workload_identity_provider` and
`github_actions_service_account`).

### 2. Deploy code

Both deployments run via GitHub Actions on push to `main`:

- changes under `worker/**` trigger [.github/workflows/deploy-backend.yml](.github/workflows/deploy-backend.yml)
  (Cloud Run, source-based deploy via `gcloud run deploy`).
- changes under `frontend/**` trigger [.github/workflows/deploy-frontend.yml](.github/workflows/deploy-frontend.yml)
  (App Engine, `google-github-actions/deploy-appengine`).

Update the `workload_identity_provider` and `service_account` values in both
workflows to match your Terraform outputs.

## Using the app

Open the App Engine URL, upload a small seed image (max 128×128), pick a patch
size (2–5) and output dimensions (64–1024), and submit. The dashboard polls
Firestore every 3 s and shows input/output pairs as jobs complete.

Larger output sizes scale the solver's memory and runtime roughly cubically in
the pattern count × grid area; 256–512 is a reasonable ceiling for most seeds.
