#!/usr/bin/env bash
#
# Put a real run into Cloud Trace, and read it back by an id nobody sent.
#
# The claim `otel.py` makes is that a distributed trace needs no propagated
# context, because this system's ids are already durable and already
# positional. That claim is cheap to assert in a test and only means
# something across machines, so this script does the version that counts:
# it computes the trace id from the origin *before* the run starts, on this
# laptop, and then asks Cloud Trace for exactly that id afterwards. Nothing
# in between is told what the trace is called.
#
#   ./live/trace.sh
#
# Two things have to be true first, and this script does neither, because
# both are changes to the deployment rather than observations of it.
#
#   1. The service runs with `TRACE=1` and `PROJECT` set. Off is the
#      default and off means no spans are built at all, so a run against a
#      service without it produces a perfectly healthy document and no
#      trace. `app.py` reads them; changing them is a redeploy.
#
#   2. Whatever account the service runs as can write traces --
#      `roles/cloudtrace.agent` on the project. Without it the spans are
#      built, the export fails, and `otel_gcp.py` logs `dropped N spans`
#      and carries on. That is on purpose: a trace is what you look at when
#      something else went wrong, and it must never be the thing that goes
#      wrong. It does mean a missing role looks exactly like a missing
#      trace, so step 5 tells you where to look.
#
set -euo pipefail

PROJECT="${PROJECT:-resonate-chess}"
BUCKET="${BUCKET:-de-contract-28425}"
URL="${URL:-https://de-svc-570193643085.europe-west1.run.app}"
ORIGIN="${ORIGIN:-research.trace1}"
WAIT="${WAIT:-45}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "0. what the trace will be called, derived here, before anything runs"
TRACE="$(cd "$HERE" && python -c "import otel; print(otel.trace_id('$ORIGIN').hex())")"
ROOT="$(cd "$HERE" && python -c "import otel; print(otel.span_id('$ORIGIN').hex())")"
echo "origin    $ORIGIN"
echo "trace     $TRACE"
echo "root span $ROOT"

say "1. is the deployment actually tracing"
gcloud run services describe "${SERVICE:-de-svc}" --project="$PROJECT" \
  --region="${REGION:-europe-west1}" \
  --format="value(spec.template.spec.containers[0].env)" | tr ',' '\n' | grep -i -E "trace|project" \
  || echo "no TRACE in the environment -- see the header; this run will produce no spans"

say "2. start a run"
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
        \"tags\":{\"resonate:target\":\"$URL/execute\"}}}" | head -c 300
echo

say "3. let it finish, and let the exporter flush ($WAIT s)"
sleep "$WAIT"

say "4. the document -- the other resolution of the same run"
gcloud storage cat "gs://$BUCKET/wf/$ORIGIN" --project="$PROJECT" | head -c 400
echo

say "5. ask Cloud Trace for the id derived in step 0"
ACCESS="$(gcloud auth print-access-token)"
curl -sS -H "Authorization: Bearer $ACCESS" \
  "https://cloudtrace.googleapis.com/v1/projects/$PROJECT/traces/$TRACE?view=COMPLETE" \
  > /tmp/de-trace.json
python - <<'PY'
import json, sys
t = json.load(open("/tmp/de-trace.json"))
if "error" in t:
    print("Cloud Trace says:", t["error"].get("message"))
    print()
    print("Not there. In order of likelihood: TRACE was never set on the")
    print("service; the account cannot write traces, in which case the logs")
    print("say 'dropped N spans'; or it has not flushed yet -- a batch is")
    print("held until it is full or the process exits, and a container that")
    print("is still warm has neither happened to it.")
    print()
    print("What it does not mean is that the id was derived wrongly. That")
    print("would put the spans in a trace under some other name, which the")
    print("console would show and this lookup would not.")
    sys.exit(1)

spans = t["spans"]
print(f"{len(spans)} spans in trace {t['traceId']}\n")
known = {s["spanId"] for s in spans}
kids = {}
for s in spans:
    kids.setdefault(s.get("parentSpanId"), []).append(s)

def show(s, d=0):
    a = s.get("labels", {})
    kind = a.get("de.span", "?")
    extra = a.get("de.state") or a.get("de.outcome", "")
    print(f"{'  ' * d}{kind:<9} {s['name']:<10} {a.get('de.promise', ''):<20} {extra}")
    for k in sorted(kids.get(s["spanId"], []), key=lambda x: x["startTime"]):
        show(k, d + 1)

for r in sorted((s for s in spans if s.get("parentSpanId") not in known),
                key=lambda x: x["startTime"]):
    show(r)
PY

cat <<'NOTE'

What to read.

The trace id in step 5 is the one printed in step 0, computed from the
origin alone, before the run existed. Nothing carried it between the two.

Then find the root promise and compare its logical span against its
physical ones added together. The difference is the run waiting rather
than working, and it is the number neither the document nor an ordinary
profiler will give you.
NOTE
