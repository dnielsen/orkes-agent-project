#!/usr/bin/env bash
# One-shot setup for video_dialogue_pipeline on a fresh machine or cloud session:
#   1. Conductor CLI   -> ./.bin/conductor   (release binary, no global install)
#   2. Python workers  -> ./.venv            (requirements.txt)
#   3. Local Conductor server on :8080       (skipped if CONDUCTOR_SERVER_URL is set)
#   4. Task definitions + workflow registered (authenticated on Orkes, see below)
#   5. Workers started in the background     (logs in ./.run/workers.log)
#
# Usage:  ./setup.sh          set everything up
#         ./setup.sh --run    ...then run the workflow on sample/meeting.mp4
#         ./setup.sh --stop   stop the workers and the local server
#
# Local server (CONDUCTOR_SERVER_URL unset): no auth. The summary step needs
# ANTHROPIC_API_KEY (a workspace-scoped sk-ant-api03- key) in the environment of
# the *server*, so export it before running this script.
#
# Orkes (e.g. CONDUCTOR_SERVER_URL=https://developer.orkescloud.com/api): export
# CONDUCTOR_AUTH_KEY + CONDUCTOR_AUTH_SECRET (an application access key). This
# script exchanges them at POST /api/token and sends the JWT as X-Authorization;
# the CLI and the Python workers read the same variables. The Anthropic key lives
# in the cluster's AI integration (llmProvider "anthropic"), not here.
set -euo pipefail

cd "$(dirname "$0")"
HERE=$PWD
BIN=$HERE/.bin
RUN=$HERE/.run
CONDUCTOR=$BIN/conductor
LOCAL_SERVER=0
if [[ -z "${CONDUCTOR_SERVER_URL:-}" ]]; then
  export CONDUCTOR_SERVER_URL=http://localhost:8080/api
  LOCAL_SERVER=1
fi
mkdir -p "$BIN" "$RUN"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Auth. The local OSS server has no /token endpoint, so don't let stray Orkes
# credentials reach the CLI or the workers there.
if [[ $LOCAL_SERVER == 1 ]]; then
  if [[ -n "${CONDUCTOR_AUTH_KEY:-}${CONDUCTOR_AUTH_SECRET:-}${CONDUCTOR_AUTH_TOKEN:-}" ]]; then
    log "local server: ignoring CONDUCTOR_AUTH_* (set CONDUCTOR_SERVER_URL to use Orkes)"
  fi
  unset CONDUCTOR_AUTH_KEY CONDUCTOR_AUTH_SECRET CONDUCTOR_AUTH_TOKEN
fi
TOKEN=
get_token() {
  if [[ -n "${CONDUCTOR_AUTH_TOKEN:-}" ]]; then
    TOKEN=$CONDUCTOR_AUTH_TOKEN
  elif [[ -n "${CONDUCTOR_AUTH_KEY:-}" && -n "${CONDUCTOR_AUTH_SECRET:-}" ]]; then
    # Credentials go through jq's env and stdin, never argv.
    TOKEN=$(jq -nc '{keyId: env.CONDUCTOR_AUTH_KEY, keySecret: env.CONDUCTOR_AUTH_SECRET}' \
      | curl -fsS -X POST "$CONDUCTOR_SERVER_URL/token" -H 'content-type: application/json' -d @- \
      | jq -r '.token // empty') || true
    [[ -n $TOKEN ]] || die "could not get a token from $CONDUCTOR_SERVER_URL/token (check CONDUCTOR_AUTH_KEY / CONDUCTOR_AUTH_SECRET)"
    export CONDUCTOR_SERVER_TYPE=${CONDUCTOR_SERVER_TYPE:-Enterprise}
  fi
}
# curl against the Conductor API; the header is fed from a process substitution
# so the token never appears on a command line.
api() {
  if [[ -n $TOKEN ]]; then
    curl "$@" -H @<(printf 'X-Authorization: %s\n' "$TOKEN")
  else
    curl "$@"
  fi
}

stop_all() {
  if [[ -f $RUN/workers.pid ]]; then
    kill "$(cat "$RUN/workers.pid")" 2>/dev/null || true
    rm -f "$RUN/workers.pid"
    log "workers stopped"
  fi
  if [[ $LOCAL_SERVER == 1 && -x $CONDUCTOR ]]; then
    (cd "$RUN" && "$CONDUCTOR" server stop) || true
  fi
}

if [[ "${1:-}" == "--stop" ]]; then
  stop_all
  exit 0
fi

tools="ffmpeg ffprobe python3 curl jq"
[[ $LOCAL_SERVER == 1 ]] && tools="$tools java"
for tool in $tools; do
  command -v "$tool" >/dev/null || die "$tool is required but not on PATH"
done

# 1. Conductor CLI. The npm package's postinstall downloads this same binary but
#    ignores HTTPS proxies, so fetch it directly.
if [[ ! -x $CONDUCTOR ]]; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) asset=conductor_linux_amd64 ;;
    Linux-aarch64) asset=conductor_linux_arm64 ;;
    Darwin-x86_64) asset=conductor_darwin_amd64 ;;
    Darwin-arm64) asset=conductor_darwin_arm64 ;;
    *) die "unsupported platform $(uname -sm)" ;;
  esac
  log "downloading Conductor CLI ($asset)"
  curl -fsSL -o "$CONDUCTOR.part" "https://github.com/conductor-oss/conductor-cli/releases/latest/download/$asset"
  chmod +x "$CONDUCTOR.part" && mv "$CONDUCTOR.part" "$CONDUCTOR"
fi
"$CONDUCTOR" --version

# 2. Worker dependencies.
if [[ ! -x .venv/bin/python ]]; then
  log "creating .venv"
  python3 -m venv .venv
fi
log "installing worker requirements"
.venv/bin/pip install -q -r requirements.txt

# 3. Server.
if [[ $LOCAL_SERVER == 1 ]]; then
  if curl -fs "$CONDUCTOR_SERVER_URL/metadata/workflow" >/dev/null 2>&1; then
    log "Conductor server already running at $CONDUCTOR_SERVER_URL"
    log "  (restart it with './setup.sh --stop' if it was started without ANTHROPIC_API_KEY)"
  else
    [[ -n "${ANTHROPIC_API_KEY:-}" ]] || log "WARNING: ANTHROPIC_API_KEY is not set; the summary step will fail"
    log "starting local Conductor server (first run downloads ~450 MB)"
    (cd "$RUN" && "$CONDUCTOR" server start)
  fi
else
  log "using existing server $CONDUCTOR_SERVER_URL"
  get_token
  if [[ -n $TOKEN ]]; then
    log "authenticated (Orkes); the LLM key must be in the server's 'anthropic' integration"
  else
    log "no CONDUCTOR_AUTH_KEY/SECRET set; assuming an OSS server (its own environment must hold the LLM provider key)"
  fi
fi

# 4. Register task definitions, then the workflow (PUT = create or update).
log "registering task definitions"
api -fsS -X POST "$CONDUCTOR_SERVER_URL/metadata/taskdefs" -H 'content-type: application/json' -d @taskdefs.json >/dev/null
log "registering workflow"
jq -c '[.]' workflow.json | api -fsS -X PUT "$CONDUCTOR_SERVER_URL/metadata/workflow" -H 'content-type: application/json' -d @- >/dev/null
missing=$(comm -23 <(jq -r '.[].name' taskdefs.json | sort) \
                   <("$CONDUCTOR" task list --json | jq -r '.[].name' | sort))
[[ -z $missing ]] || die "task definitions missing on server: $missing"

# 5. Workers.
if [[ -f $RUN/workers.pid ]] && kill -0 "$(cat "$RUN/workers.pid")" 2>/dev/null; then
  log "workers already running (pid $(cat "$RUN/workers.pid"))"
else
  log "starting workers"
  nohup .venv/bin/python workers.py >"$RUN/workers.log" 2>&1 &
  echo $! >"$RUN/workers.pid"
  sleep 5
  kill -0 "$(cat "$RUN/workers.pid")" 2>/dev/null || die "workers exited; see $RUN/workers.log"
fi

log "ready. UI: ${CONDUCTOR_SERVER_URL%/api}/workflowDef/video_dialogue_pipeline"

if [[ "${1:-}" == "--run" ]]; then
  input=$(jq -nc --arg v "$HERE/sample/meeting.mp4" --arg o "$HERE/sample/out" '{videoPath: $v, outputDir: $o}')
  id=$("$CONDUCTOR" workflow start -w video_dialogue_pipeline -i "$input" | tail -1)
  log "started execution $id"
  for _ in $(seq 1 200); do
    status=$(api -fs "$CONDUCTOR_SERVER_URL/workflow/$id?includeTasks=false" | jq -r .status)
    [[ $status == RUNNING ]] || break
    sleep 3
  done
  api -fs "$CONDUCTOR_SERVER_URL/workflow/$id" \
    | jq -r '"status: \(.status)", (.reasonForIncompletion // empty),
             "", "encodings:", (.output.encodings // [] | .[] | "  \(.format)  \(.resolution)  \(.sizeBytes) bytes  \(.outputPath)"),
             "", "dialogue: \(.output.dialogue.source) (\(.output.dialogue.segmentCount) segments)",
             "", (.output.summary // "(no summary)")'
fi
