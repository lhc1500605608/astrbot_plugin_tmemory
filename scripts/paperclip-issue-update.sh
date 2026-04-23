#!/bin/bash
ISSUE_ID="$1"
STATUS="$2"
COMMENT=$(cat)
jq -n --arg status "$STATUS" --arg comment "$COMMENT" '{status: $status, comment: $comment}' | curl -s -X PATCH "$PAPERCLIP_API_URL/api/issues/$ISSUE_ID" -H "Authorization: Bearer $PAPERCLIP_API_KEY" -H "X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID" -H "Content-Type: application/json" -d @-
