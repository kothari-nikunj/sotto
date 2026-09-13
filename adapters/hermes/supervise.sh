#!/usr/bin/env bash
# Sourced by start.sh; Bash 3-compatible so fake-process contracts run on macOS too.
# One child owns the receiver and its descendants; one owns Hermes and its descendants.
RECEIVER_PID=""
GW_PID=""

sotto_signal_group() {
  local signal="$1" pid="$2"
  [ -n "$pid" ] || return 0
  kill -"$signal" -- "-$pid" 2>/dev/null || kill -"$signal" "$pid" 2>/dev/null || true
}

sotto_shutdown() {
  local code="$1" tick
  trap - EXIT TERM INT
  sotto_signal_group TERM "$GW_PID"
  sotto_signal_group TERM "$RECEIVER_PID"
  # Give handlers a bounded opportunity to persist leases/receipts and stop their workers.
  for ((tick=0; tick<50; tick++)); do
    # Empty PIDs must not be interpreted as our own process group.
    if { [ -z "$GW_PID" ] || ! kill -0 "$GW_PID" 2>/dev/null; } \
      && { [ -z "$RECEIVER_PID" ] || ! kill -0 "$RECEIVER_PID" 2>/dev/null; }; then
      break
    fi
    sleep 0.2
  done
  sotto_signal_group KILL "$GW_PID"
  sotto_signal_group KILL "$RECEIVER_PID"
  if [ -n "$GW_PID" ]; then wait "$GW_PID" 2>/dev/null || true; fi
  if [ -n "$RECEIVER_PID" ]; then wait "$RECEIVER_PID" 2>/dev/null || true; fi
  exit "$code"
}

sotto_install_traps() {
  trap 'sotto_shutdown $?' EXIT
  trap 'sotto_shutdown 0' TERM INT
}

sotto_supervise() {
  local code
  # The receiver was started early so setup remains available while boot reconciles Hermes.
  # An unexpected clean exit is still a service failure, including the receiver-only path.
  while :; do
    if ! kill -0 "$RECEIVER_PID" 2>/dev/null; then
      code=0; wait "$RECEIVER_PID" || code=$?
      [ "$code" -ne 0 ] || code=1
      echo "[sotto] receiver exited ($code); recycling the instance"
      return "$code"
    fi
    if [ -n "$GW_PID" ] && ! kill -0 "$GW_PID" 2>/dev/null; then
      code=0; wait "$GW_PID" || code=$?
      [ "$code" -ne 0 ] || code=1
      echo "[sotto] gateway exited ($code); recycling the instance"
      return "$code"
    fi
    sleep 0.2
  done
}
