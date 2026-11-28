#!/usr/bin/env bash
# Keep one open issue for a check nobody watches: open it when the check fails, comment on each
# later failure and close it when the check passes, so each change reaches the owner by email.
# A cancelled or skipped check says nothing either way.
#
#   alert.sh TITLE RESULT LINK [DETAIL]
set -euo pipefail
title=$1 result=$2 link=$3 detail=${4:-}
case $result in success | failure) ;; *) exit 0 ;; esac
issue=$(TITLE=$title gh issue list --state open --search "in:title \"$title\"" --json number,title \
  --jq '[.[] | select(.title == env.TITLE) | .number][0] // empty')
if [[ $result == success ]]; then
  if [[ -n $issue ]]; then gh issue close "$issue" --comment "Passing again: $link"; fi
elif [[ -n $issue ]]; then
  gh issue comment "$issue" --body "Failed again${detail:+ ($detail)}: $link"
else
  gh issue create --title "$title" \
    --body "$(printf 'Failed%s: %s\n\nThis closes itself when the check passes again.\n' "${detail:+ ($detail)}" "$link")"
fi
