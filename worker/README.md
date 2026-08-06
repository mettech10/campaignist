# GPU render worker

Turns a Campaignist shot brief into an actual video, on a rented GPU.

```
Render (brain)                     Vast.ai A4000 (muscle)
  render_jobs queue  ◀── polls ──  render_worker.py
  signed upload URL  ──────────▶   ComfyUI (installed by Pinokio)
```

## Why it pulls instead of being pushed to

A Vast instance has an ephemeral address and is replaced whenever it is
interrupted. Anything that needed to reach *in* would need a tunnel kept alive
forever. Polling needs no inbound networking, and a replaced box just starts
polling again.

## Why the job is leased, not assigned

The instance is interruptible — that is what makes it £1.8/day rather than £6.
It can disappear between any two lines of the worker, with no chance to tidy up.
So the server hands out a *lease*: the worker holds the job only while it keeps
beating, and a lease that stops beating goes back on the queue. A job on a dead
box is never lost, and a user never waits forever for a video nobody is making.

## Why the worker has no database credential

Vast.ai is a marketplace of anonymous hosts — this is someone else's machine.
The Supabase service-role key bypasses row-level security entirely, so it stays
on Render. The worker gets one token that can claim jobs and one URL per job that
can write exactly one object. **Losing the box costs GPU time, not data.** Keep
it that way: never put `SUPABASE_SERVICE_KEY` on this machine.

## Setup

1. Rent an A4000 (16GB) on Vast.ai. Interruptible is fine and is the point.
2. Install Pinokio, and use it to install ComfyUI plus your video pipeline
   (AnimateDiff or SVD). Pinokio is genuinely good at this part — models,
   dependencies, CUDA versions.
3. Start ComfyUI so it listens on `127.0.0.1:8188`.
4. Drop workflow graphs into `workflows/`, one JSON per format, plus a
   `default.json`. Adding a format is dropping in a file — no code change, the
   same way adding an agent is dropping in a spec.

   These placeholders are substituted before the graph is sent:

   | Token | Becomes |
   |---|---|
   | `__PROMPT__` | the shot brief plus format guidance |
   | `__HOOK__` | the asset's hook line |
   | `__WIDTH__` / `__HEIGHT__` | from the aspect ratio |
   | `__SEED__` | fresh random seed per render |

5. Run it:

```bash
export CAMPAIGNIST_API=https://campaignist.onrender.com
export WORKER_TOKEN=...          # matches RENDER_WORKER_TOKEN on Render
export WORKER_ID=vast-a4000-1
python render_worker.py
```

`GET /api/worker/hello` is called once on boot, so a misconfigured box says so
in its own log rather than silently polling an endpoint that rejects it.

## Keeping it up

```bash
# /etc/systemd/system/campaignist-worker.service
[Service]
ExecStart=/usr/bin/python3 /root/worker/render_worker.py
EnvironmentFile=/root/worker/.env
Restart=always
RestartSec=10
```

`Restart=always` and the lease cover each other: the box reboots, the worker
comes back, and anything it was mid-way through has already returned to the
queue.

## Cost

An A4000 at roughly £1.8/day is about £54/month, flat, whether or not anything
is rendering. If that starts looking like the wrong shape, the queue already
supports on-demand — nothing here assumes the worker is always up, so a box can
be started when `render_jobs` has depth and stopped when it drains.
