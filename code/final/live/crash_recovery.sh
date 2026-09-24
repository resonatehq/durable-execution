#!/usr/bin/env bash
#
# Destroy a run's only dispatch, and see whether its deadline brings it back.
#
# This is the durability claim, on the deployment. `test_e2e.py` makes it
# against a simulator and `test_http.py` against a bound socket, both of
# which are worth having and neither of which involves Google deciding when
# a message is delivered.
#
# The trick is the pause. A dispatch carries no schedule, so Cloud Tasks
# delivers it within a second or so, and deleting it afterwards proves
# nothing -- the work already happened. Pausing the queue first turns a race
# into a sequence: the engine enqueues, nothing is delivered, we delete the
# dispatch at our leisure, and only the armed deadline still knows this run
# exists.
#
# Run it with credentials that can pause the queue and read the bucket.
# Everything it touches it created; it leaves the document behind on purpose.
#
#   ./live/crash_recovery.sh
#
set -euo pipefail

PROJECT="${PROJECT:-resonate-chess}"
REGION="${REGION:-europe-west1}"
QUEUE="${QUEUE:-de-q}"
BUCKET="${BUCKET:-de-contract-28425}"
URL="${URL:-https://de-svc-570193643085.europe-west1.run.app}"
ORIGIN="${ORIGIN:-research.crash1}"

#: `RETRY_TIMEOUT` on the service, plus room for the timeout message to be
#: delivered and the run to finish. Shorter than this and a pass is luck.
WAIT="${WAIT:-90}"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "0. clean slate"
gcloud storage rm "gs://$BUCKET/wf/**" --project="$PROJECT" 2>/dev/null || true
gcloud tasks queues purge "$QUEUE" --location="$REGION" --project="$PROJECT" --quiet
gcloud tasks queues resume "$QUEUE" --location="$REGION" --project="$PROJECT" --quiet

say "1. start the run"
TOKEN="$(gcloud auth print-identity-token --audiences="$URL")"
NOW="$(( $(date +%s) * 1000 ))"
PARAM='{\"f\": \"research\", \"a\": [\"What is durable execution?\"]}'
curl -sS -X POST "$URL/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"kind\":\"promise.create\",\"data\":{
        \"id\":\"$ORIGIN\",
        \"timeoutAt\":$(( NOW + 3600000 )),
        \"param\":{\"data\":\"$PARAM\"},
        \"tags\":{\"resonate:target\":\"$URL/\"}}}" | head -c 400
echo

say "2. pause, so nothing else is delivered while we interfere"
gcloud tasks queues pause "$QUEUE" --location="$REGION" --project="$PROJECT" --quiet
sleep 5

say "3. what is queued now"
gcloud tasks list --queue="$QUEUE" --location="$REGION" --project="$PROJECT" \
  --format="table(name.basename(),httpRequest.url,scheduleTime)"

say "4. destroy every dispatch, keep the deadline"
DELETED=0
# Both kinds go to the same URL; the body says which is which.
while read -r NAME; do
  BODY=$(gcloud tasks describe "$NAME" --queue="$QUEUE" --location="$REGION" \
           --project="$PROJECT" --response-view=full \
           --format="value(httpRequest.body)" | base64 -d)
  case "$BODY" in
    *'"kind": "execute"'*)
      gcloud tasks delete "$NAME" --queue="$QUEUE" --location="$REGION" \
        --project="$PROJECT" --quiet
      DELETED=$(( DELETED + 1 ))
      ;;
  esac
done < <(gcloud tasks list --queue="$QUEUE" --location="$REGION" \
           --project="$PROJECT" --format="value(name.basename())")
echo "destroyed $DELETED dispatch(es)"
if [ "$DELETED" -eq 0 ]; then
  echo "NOTHING WAS DESTROYED -- the dispatch was delivered before the pause."
  echo "The run is proceeding normally and this tells you nothing. Re-run."
  exit 2
fi

say "5. resume, and wait out the retry deadline ($WAIT s)"
gcloud tasks queues resume "$QUEUE" --location="$REGION" --project="$PROJECT" --quiet
sleep "$WAIT"

say "6. did the deadline bring it back?"
gcloud storage cat "gs://$BUCKET/wf/$ORIGIN" --project="$PROJECT"

cat <<'NOTE'

The document is one JSON object. `gen` is how many conditional writes
happened; then find the root promise in `objects` and read its state.

  resolved, six promises      the deadline rescued a run whose only dispatch
                              was destroyed. That is the claim.
  still pending               the recovery path does not work in the
                              deployment, which is worth knowing and worth
                              reporting as-is.
  rejected_timedout           it was not rescued in time and the root
                              expired instead.
NOTE
