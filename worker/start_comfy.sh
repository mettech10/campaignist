#!/usr/bin/env bash
# Start ComfyUI and check it can actually run our graphs.
#
# The preflight matters more than the start. A render that fails because a node
# is missing or a checkpoint is named differently fails *after* the job is
# claimed, several minutes in, and reports as a generic worker error. Every
# check here is something that would otherwise be discovered that way.
#
# Safe to re-run: it will not start a second ComfyUI beside a live one.
#
# Usage:  bash start_comfy.sh

set -euo pipefail

COMFY_URL="${COMFY_URL:-http://127.0.0.1:8188}"
WORKER_HOME="${WORKER_HOME:-/opt/campaignist-worker}"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '   \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[31mxx %s\033[0m\n' "$*" >&2; exit 1; }

# ── Locate ──────────────────────────────────────────────────────────────────
say "locating ComfyUI"
if [ -z "${COMFY_ROOT:-}" ]; then
  for candidate in \
      /ComfyUI /workspace/ComfyUI /root/ComfyUI "$HOME/ComfyUI" \
      "$HOME"/pinokio/api/comfyui.git/app /workspace/pinokio/api/comfyui.git/app ; do
    if [ -f "$candidate/main.py" ]; then COMFY_ROOT="$candidate"; break; fi
  done
fi
[ -n "${COMFY_ROOT:-}" ] || die "no main.py found — set COMFY_ROOT=/path/to/ComfyUI"
echo "   $COMFY_ROOT"

# ── Start ───────────────────────────────────────────────────────────────────
say "starting"
if curl -fsS --max-time 5 "$COMFY_URL/system_stats" >/dev/null 2>&1; then
  ok "already running at $COMFY_URL"
else
  cd "$COMFY_ROOT"
  # --listen 127.0.0.1 only. The worker is on this same box, so there is no
  # reason to expose a ComfyUI with no authentication to the internet.
  nohup python3 main.py --listen 127.0.0.1 --port 8188 >> /tmp/comfyui.log 2>&1 &
  echo $! > /tmp/comfyui.pid
  echo "   started as PID $(cat /tmp/comfyui.pid), waiting for it to answer..."
  for _ in $(seq 1 60); do
    sleep 3
    if curl -fsS --max-time 5 "$COMFY_URL/system_stats" >/dev/null 2>&1; then break; fi
  done
  curl -fsS --max-time 5 "$COMFY_URL/system_stats" >/dev/null 2>&1 \
    || { tail -30 /tmp/comfyui.log; die "ComfyUI did not come up — log above, full log at /tmp/comfyui.log"; }
  ok "responding at $COMFY_URL"
fi

# ── Preflight ───────────────────────────────────────────────────────────────
# Everything below is a question the worker cannot answer for itself until it
# has already claimed a job and spent GPU minutes finding out.
say "preflight"

python3 - "$COMFY_URL" "$WORKER_HOME" <<'PY'
import json, sys, urllib.request, glob, os, re

comfy, worker_home = sys.argv[1], sys.argv[2]

def get(path):
    with urllib.request.urlopen(f"{comfy}{path}", timeout=60) as r:
        return json.load(r)

stats = get("/system_stats")
for d in stats.get("devices", []):
    total = d.get("vram_total", 0) / 1e9
    print(f"   gpu   {d.get('name','?')}  {total:.1f}GB VRAM")
    if total and total < 12:
        print(f"   !!    under 12GB — SVD at 576x1024 x25 frames may OOM")

info = get("/object_info")

# Nodes our graphs reference but that are not all core.
for node, why in [
    ("VHS_VideoCombine", "VideoHelperSuite — writes the mp4"),
    ("SVD_img2vid_Conditioning", "core, SVD support"),
    ("ImageOnlyCheckpointLoader", "core, loads the SVD checkpoint"),
    ("VideoLinearCFGGuidance", "core, SVD guidance"),
]:
    print(f"   {'ok ' if node in info else '!! '}  {node:<26} {why}"
          + ("" if node in info else "  MISSING"))

# The single most likely cause of a first-render failure: the workflow names a
# checkpoint file that is not on this box.
available = set()
for loader in ("CheckpointLoaderSimple", "ImageOnlyCheckpointLoader"):
    try:
        available |= set(info[loader]["input"]["required"]["ckpt_name"][0])
    except Exception:
        pass
print(f"\n   checkpoints ComfyUI can see: {sorted(available) or 'NONE'}")

wanted = set()
for path in glob.glob(os.path.join(worker_home, "workflows", "*.json")):
    for name in re.findall(r'"ckpt_name"\s*:\s*"([^"]+)"', open(path).read()):
        wanted.add(name)

missing = wanted - available
for name in sorted(wanted):
    print(f"   {'ok ' if name in available else '!! '}  workflows want {name}"
          + ("" if name in available else "   NOT FOUND"))

if missing:
    print("\n   Fix by editing the ckpt_name values in "
          f"{worker_home}/workflows/*.json to match the names above,")
    print("   or by putting those files in ComfyUI/models/checkpoints/.")
    sys.exit(1)
PY

say "ready"
echo "   ComfyUI is up and can run the graphs."
echo "   Start the worker:  bash $WORKER_HOME/run.sh && tail -f $WORKER_HOME/worker.log"
