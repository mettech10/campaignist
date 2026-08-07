#!/usr/bin/env bash
# Set up a rented Vast.ai box to render Campaignist videos.
#
# Assumes ComfyUI is already installed and running (Pinokio does that part well).
# This adds the three things it does not: the video output node, the model
# checkpoints, and the worker itself.
#
# The worker deliberately lives *outside* the ComfyUI tree. It is a separate
# process that talks to ComfyUI over HTTP, so burying it in custom_nodes or the
# app folder only means a Pinokio reinstall deletes it.
#
# Safe to re-run: every step checks before it acts.
#
# Usage:
#   export WORKER_TOKEN=...        # matches RENDER_WORKER_TOKEN on Render
#   export HF_TOKEN=hf_...         # needed for SVD, see the note below
#   bash setup_vast.sh

set -euo pipefail

WORKER_HOME="${WORKER_HOME:-/opt/campaignist-worker}"
API="${CAMPAIGNIST_API:-https://campaignist.onrender.com}"
REPO="${CAMPAIGNIST_REPO:-https://github.com/mettech10/campaignist.git}"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mxx %s\033[0m\n' "$*" >&2; exit 1; }

# ── Find ComfyUI ────────────────────────────────────────────────────────────
say "locating ComfyUI"
if [ -n "${COMFY_ROOT:-}" ]; then
  [ -d "$COMFY_ROOT" ] || die "COMFY_ROOT=$COMFY_ROOT does not exist"
else
  for candidate in \
      /ComfyUI /workspace/ComfyUI /root/ComfyUI "$HOME/ComfyUI" \
      "$HOME"/pinokio/api/comfyui.git/app "$HOME"/pinokio/api/comfy.git/app \
      /workspace/pinokio/api/comfyui.git/app ; do
    if [ -d "$candidate/models/checkpoints" ]; then COMFY_ROOT="$candidate"; break; fi
  done
fi
[ -n "${COMFY_ROOT:-}" ] || die "could not find ComfyUI — set COMFY_ROOT=/path/to/ComfyUI"
echo "   $COMFY_ROOT"

CKPT_DIR="$COMFY_ROOT/models/checkpoints"
mkdir -p "$CKPT_DIR" "$COMFY_ROOT/custom_nodes"

# ── Disk ────────────────────────────────────────────────────────────────────
# The two checkpoints are ~16.5GB together. Vast instances often ship with a
# small default disk, and half a safetensors file fails in a way that reads like
# a corrupt model rather than a full disk.
say "checking disk"
AVAIL_GB=$(df -Pk "$CKPT_DIR" | awk 'NR==2 {print int($4/1048576)}')
echo "   ${AVAIL_GB}GB free at $CKPT_DIR"
[ "$AVAIL_GB" -ge 25 ] || warn "under 25GB free — the two checkpoints need ~16.5GB plus room to write. Resize the instance if a download dies partway."

# ── VideoHelperSuite ────────────────────────────────────────────────────────
# Supplies VHS_VideoCombine. Core ComfyUI can only write animated webp, which
# the storage bucket rejects: it accepts video/mp4 and video/webm only.
say "VideoHelperSuite"
VHS="$COMFY_ROOT/custom_nodes/ComfyUI-VideoHelperSuite"
if [ -d "$VHS/.git" ]; then
  echo "   already installed"
else
  git clone --depth 1 https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git "$VHS"
fi
if [ -f "$VHS/requirements.txt" ]; then
  python3 -m pip install -q -r "$VHS/requirements.txt" || warn "pip install failed — install VHS requirements into ComfyUI's own python"
fi
command -v ffmpeg >/dev/null || { warn "ffmpeg missing — VHS needs it to write mp4"; apt-get update -qq && apt-get install -y -qq ffmpeg || warn "could not install ffmpeg automatically"; }

# ── Checkpoints ─────────────────────────────────────────────────────────────
fetch() {  # fetch <url> <dest> [auth]
  local url="$1" dest="$2" auth="${3:-}"
  if [ -s "$dest" ]; then
    echo "   have $(basename "$dest") ($(du -h "$dest" | cut -f1))"
    return 0
  fi
  echo "   downloading $(basename "$dest") ..."
  # -C - resumes a partial file, which matters on a box that can be interrupted.
  if [ -n "$auth" ]; then
    curl -fL --retry 3 -C - -H "Authorization: Bearer $auth" -o "$dest.part" "$url" || return 1
  else
    curl -fL --retry 3 -C - -o "$dest.part" "$url" || return 1
  fi
  mv "$dest.part" "$dest"
}

say "checkpoints"
fetch "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors" \
      "$CKPT_DIR/sd_xl_base_1.0.safetensors" \
  || die "SDXL download failed"

# stable-video-diffusion-img2vid-xt is a GATED repo. Accepting the licence is
# free and instant, but until you do, an unauthenticated download returns a 401
# that curl reports as a generic failure rather than "you need to accept terms".
if ! fetch "https://huggingface.co/stabilityai/stable-video-diffusion-img2vid-xt/resolve/main/svd_xt.safetensors" \
           "$CKPT_DIR/svd_xt.safetensors" "${HF_TOKEN:-}"; then
  cat <<'EOF'

  SVD did not download. It is a gated repo, so this is almost certainly access
  rather than network. Two steps, both free:

    1. Accept the licence (signed in):
       https://huggingface.co/stabilityai/stable-video-diffusion-img2vid-xt
    2. Make a read token:
       https://huggingface.co/settings/tokens

  Then re-run with:  export HF_TOKEN=hf_xxx && bash setup_vast.sh

EOF
  die "svd_xt.safetensors missing"
fi

# A truncated safetensors file loads as a corrupt model with a confusing error,
# so check size rather than mere existence.
for f in "$CKPT_DIR/sd_xl_base_1.0.safetensors" "$CKPT_DIR/svd_xt.safetensors"; do
  sz=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f")
  [ "$sz" -gt 1000000000 ] || die "$(basename "$f") is only $((sz/1048576))MB — truncated, delete it and re-run"
done

# ── Worker ──────────────────────────────────────────────────────────────────
say "worker"
# BASH_SOURCE is unset when this is piped from curl — there is no script file,
# only stdin — and `set -u` turns that into a hard error on the documented
# install path. Default it, and only trust it if it points at a real file.
SRC="${BASH_SOURCE[0]:-}"
if [ -n "$SRC" ] && [ -f "$SRC" ]; then
  HERE="$(cd "$(dirname "$SRC")" && pwd)"
else
  HERE=""
fi
if [ -n "$HERE" ] && [ -f "$HERE/render_worker.py" ] && [ -d "$HERE/workflows" ]; then
  # Already have the files — copied up with scp, or running from a checkout.
  # This is the normal path: the repo is private, so a box cannot clone it
  # without being given a credential, and a rented box is the last place to
  # put one.
  echo "   using the files already at $HERE"
  if [ "$HERE" != "$WORKER_HOME" ]; then
    mkdir -p "$WORKER_HOME"
    cp -r "$HERE"/. "$WORKER_HOME"/
  fi
elif [ -d "$WORKER_HOME/.git" ]; then
  git -C "$WORKER_HOME" pull --ff-only
elif git ls-remote "$REPO" >/dev/null 2>&1; then
  rm -rf "$WORKER_HOME"
  git clone --depth 1 "$REPO" /tmp/campaignist-src
  mkdir -p "$WORKER_HOME"
  cp -r /tmp/campaignist-src/worker/. "$WORKER_HOME"/
  rm -rf /tmp/campaignist-src
else
  cat <<'EOF'

  Cannot reach the repo, and there are no worker files next to this script.
  campaignist is private, so GitHub answers 404 rather than 401 — the error
  says "not found" when it means "not yours".

  Copy the folder up from your own machine instead, which needs no credential
  on this box at all. From the repo root on your laptop:

      scp -P <vast-port> -r worker/ root@<vast-host>:/opt/campaignist-worker/

  then on the box:

      cd /opt/campaignist-worker && bash setup_vast.sh

EOF
  die "worker files not available"
fi
python3 -m pip install -q requests

ls "$WORKER_HOME/workflows"/*.json >/dev/null 2>&1 \
  || die "no workflow graphs at $WORKER_HOME/workflows"

if [ ! -f "$WORKER_HOME/.env" ]; then
  cat > "$WORKER_HOME/.env" <<EOF
CAMPAIGNIST_API=$API
WORKER_TOKEN=${WORKER_TOKEN:-PUT_YOUR_TOKEN_HERE}
WORKER_ID=$(hostname)
COMFY_URL=http://127.0.0.1:8188
WORKFLOW_DIR=$WORKER_HOME/workflows
EOF
  chmod 600 "$WORKER_HOME/.env"
fi
grep -q PUT_YOUR_TOKEN_HERE "$WORKER_HOME/.env" \
  && warn "set WORKER_TOKEN in $WORKER_HOME/.env — it must match RENDER_WORKER_TOKEN on Render"

# ── Service ─────────────────────────────────────────────────────────────────
# Restart=always and the server-side lease cover each other: the box reboots,
# the worker comes back, and whatever it was mid-way through has already
# returned to the queue on its own.
# The binary being present proves nothing: Vast instances are containers, where
# systemctl is installed but systemd is not PID 1 and every call fails with
# "Failed to connect to bus". /run/systemd/system only exists when it really is
# the init system.
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  say "service"
  cat > /etc/systemd/system/campaignist-worker.service <<EOF
[Unit]
Description=Campaignist GPU render worker
After=network-online.target

[Service]
WorkingDirectory=$WORKER_HOME
EnvironmentFile=$WORKER_HOME/.env
ExecStart=/usr/bin/env python3 $WORKER_HOME/render_worker.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  echo "   systemctl enable --now campaignist-worker"
  echo "   journalctl -u campaignist-worker -f"
else
  say "run script (no systemd in this container)"
  cat > "$WORKER_HOME/run.sh" <<'RUNNER'
#!/usr/bin/env bash
# Start the worker in the background and keep a log. Safe to re-run: it will
# not start a second worker alongside one that is already going.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f worker.pid ] && kill -0 "$(cat worker.pid)" 2>/dev/null; then
  echo "already running as PID $(cat worker.pid) — tail -f $(pwd)/worker.log"
  exit 0
fi
set -a; . ./.env; set +a
nohup python3 render_worker.py >> worker.log 2>&1 &
echo $! > worker.pid
sleep 2
echo "worker started as PID $(cat worker.pid)"
tail -n 20 worker.log
RUNNER
  chmod +x "$WORKER_HOME/run.sh"
  echo "   bash $WORKER_HOME/run.sh"
  echo "   tail -f $WORKER_HOME/worker.log"
fi

say "checks"
curl -fsS "${COMFY_URL:-http://127.0.0.1:8188}/system_stats" >/dev/null \
  && echo "   ComfyUI responding" \
  || warn "ComfyUI not answering on 127.0.0.1:8188 — start it before the worker"

if [ -d /run/systemd/system ]; then
  START_CMD="systemctl enable --now campaignist-worker && journalctl -u campaignist-worker -f"
else
  START_CMD="bash $WORKER_HOME/run.sh && tail -f $WORKER_HOME/worker.log"
fi

cat <<EOF

Ready. Start it with:

  $START_CMD

The first line should be "worker <id> up, polling $API". If the token is wrong
you get a clear failure there instead of silence that looks like an empty queue.
EOF
