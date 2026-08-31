#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

cd "$PROJECT_DIR"
if [ "${FEEDBACK_ONLY:-false}" = "true" ]; then
    : "${BROKER_FILLS_FILE:?BROKER_FILLS_FILE is required when FEEDBACK_ONLY=true}"
    APPLY_STATE_ARG=""
    if [ "${APPLY_BROKER_STATE:-false}" = "true" ]; then
        APPLY_STATE_ARG="--apply-broker-state"
    fi
    if [ -n "${EXECUTION_PLAN_PATH:-}" ]; then
        python main.py --publish --feedback-only $APPLY_STATE_ARG --execution-plan "$EXECUTION_PLAN_PATH" --broker-fills "$BROKER_FILLS_FILE"
    else
        python main.py --publish --feedback-only $APPLY_STATE_ARG --broker-fills "$BROKER_FILLS_FILE"
    fi
elif [ -n "${BROKER_FILLS_FILE:-}" ]; then
    python main.py --publish --broker-fills "$BROKER_FILLS_FILE"
else
    VIRTUAL_ARG=""
    if [ "${VIRTUAL_BROKER_CONFIRM:-false}" = "true" ]; then
        VIRTUAL_ARG="--virtual-confirm"
    fi
    python main.py --publish $VIRTUAL_ARG
fi
