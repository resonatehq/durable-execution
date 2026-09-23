import Kernel
import Lean.Data.Json

/-!
# The differential driver

Reads a script from stdin, one JSON line per step, runs it through the Lean
kernel from the empty document, and writes one JSON line per step: what the
step returned. `test/test_lean.py` runs the same script through `kernel.py`
and requires the two outputs to be equal.

    {"now": 5, "sweep": true}                        handle_internal
    {"now": 5, "req": {"kind": "promise.create", ...}}  handle_external

Each step starts from the document the previous one committed. The encoding
is the one `test/test_lean.py` also writes, field for field.
-/
open Lean Kernel

/-! ## Decoding a request -/

def strMap (j : Json) : Except String (List (String × String)) := do
  let o ← j.getObj?
  o.foldlM (init := []) fun acc k v => do return acc ++ [(k, ← v.getStr?)]

def optField (j : Json) (k : String) : Option Json :=
  match j.getObjVal? k with
  | .ok .null => none
  | .ok v => some v
  | .error _ => none

def decValue (j : Json) : Except String Value := do
  let headers ← match optField j "headers" with
    | some h => some <$> strMap h
    | none => pure none
  let data ← match optField j "data" with
    | some d => some <$> d.getStr?
    | none => pure none
  return { headers, data }

def decPState : String → Except String PState
  | "pending" => pure .pending
  | "resolved" => pure .resolved
  | "rejected" => pure .rejected
  | "rejected_canceled" => pure .rejectedCanceled
  | "rejected_timedout" => pure .rejectedTimedout
  | s => throw s!"unknown promise state {s}"

def decCreate (j : Json) : Except String PromiseCreate := do
  return { id := ← j.getObjValAs? String "id", timeoutAt := ← j.getObjValAs? Int "timeoutAt",
           param := ← decValue (← j.getObjVal? "param"), tags := ← strMap (← j.getObjVal? "tags") }

def decSettle (j : Json) : Except String PromiseSettle := do
  return { id := ← j.getObjValAs? String "id", state := ← decPState (← j.getObjValAs? String "state"),
           value := ← decValue (← j.getObjVal? "value") }

def decReq (j : Json) : Except String Req := do
  let s (k : String) := j.getObjValAs? String k
  let n (k : String) := j.getObjValAs? Nat k
  let i (k : String) := j.getObjValAs? Int k
  match ← s "kind" with
  | "promise.get" => return .promiseGet (← s "id")
  | "promise.create" => return .promiseCreate (← decCreate j)
  | "promise.settle" => return .promiseSettle (← decSettle j)
  | "promise.register_callback" => return .promiseRegisterCallback (← s "awaited") (← s "awaiter")
  | "promise.register_listener" => return .promiseRegisterListener (← s "awaited") (← s "address")
  | "task.get" => return .taskGet (← s "id")
  | "task.create" => return .taskCreate (← s "pid") (← i "ttl") (← decCreate (← j.getObjVal? "action"))
  | "task.acquire" => return .taskAcquire (← s "id") (← n "version") (← s "pid") (← i "ttl")
  | "task.release" => return .taskRelease (← s "id") (← n "version")
  | "task.fulfill" => return .taskFulfill (← s "id") (← n "version") (← decSettle (← j.getObjVal? "action"))
  | "task.suspend" =>
    return .taskSuspend (← s "id") (← n "version") ((← (← j.getObjVal? "awaited").getArr?).toList.filterMap fun x => x.getStr?.toOption)
  | "task.fence" =>
    let a ← j.getObjVal? "action"
    let act ← match ← a.getObjValAs? String "kind" with
      | "promise.create" => FenceAction.create <$> decCreate a
      | "promise.settle" => FenceAction.settle <$> decSettle a
      | k => throw s!"unknown fence action {k}"
    return .taskFence (← s "id") (← n "version") (← s "corrId") act
  | "task.heartbeat" =>
    let ts ← (← j.getObjVal? "tasks").getArr?
    let ts ← ts.toList.mapM fun t => do
      let a ← t.getArr?
      return (← a[0]!.getStr?, ← a[1]!.getNat?)
    return .taskHeartbeat (← s "pid") ts
  | "task.halt" => return .taskHalt (← s "id")
  | "task.continue" => return .taskContinue (← s "id")
  | k => throw s!"unknown request kind {k}"

/-! ## Encoding what came back -/

def encPState : PState → Json
  | .pending => "pending"
  | .resolved => "resolved"
  | .rejected => "rejected"
  | .rejectedCanceled => "rejected_canceled"
  | .rejectedTimedout => "rejected_timedout"

def encTState : TState → Json
  | .pending => "pending"
  | .acquired => "acquired"
  | .suspended => "suspended"
  | .halted => "halted"
  | .fulfilled => "fulfilled"

def encMap (m : List (String × String)) : Json := Json.mkObj (m.map fun (k, v) => (k, Json.str v))

def encValue (v : Value) : Json :=
  Json.mkObj ((match v.headers with | some h => [("headers", encMap h)] | none => [])
    ++ (match v.data with | some d => [("data", Json.str d)] | none => []))

def encOpt {α} [ToJson α] : Option α → Json
  | some a => toJson a
  | none => .null

/-- `Promise.to_record`. -/
def encPRec (r : PRec) : Json :=
  let (id, p) := r
  Json.mkObj ([("id", Json.str id), ("state", encPState p.state), ("param", encValue p.param),
    ("value", encValue p.value), ("tags", encMap p.tags), ("timeoutAt", toJson p.timeoutAt),
    ("createdAt", toJson p.createdAt)]
    ++ (match p.settledAt with | some s => [("settledAt", toJson s)] | none => []))

/-- `Task.to_record`. -/
def encTRec (r : TRec) : Json :=
  let (id, t) := r
  Json.mkObj ([("id", Json.str id), ("state", encTState t.state), ("version", toJson t.version),
    ("resumes", toJson t.resumes.length)]
    ++ (match t.ttl with | some x => [("ttl", toJson x)] | none => [])
    ++ (match t.pid with | some x => [("pid", Json.str x)] | none => []))

def sortStrs (l : List String) : List String := (l.toArray.qsort (· < ·)).toList

/-- The whole document: every field, the resumes as a sorted set. -/
def encDoc (d : Doc) : Json :=
  Json.mkObj [("timerAt", encOpt d.timerAt), ("objects", Json.arr (d.objects.toArray.map fun o =>
    Json.mkObj [("id", Json.str o.id),
      ("promise", Json.mkObj [("state", encPState o.promise.state), ("param", encValue o.promise.param),
        ("value", encValue o.promise.value), ("tags", encMap o.promise.tags),
        ("timeoutAt", toJson o.promise.timeoutAt), ("createdAt", toJson o.promise.createdAt),
        ("settledAt", encOpt o.promise.settledAt), ("callbacks", toJson o.promise.callbacks),
        ("listeners", toJson o.promise.listeners)]),
      ("task", match o.task with
        | none => .null
        | some t => Json.mkObj [("state", encTState t.state), ("version", toJson t.version),
            ("pid", encOpt t.pid), ("ttl", encOpt t.ttl), ("resumes", toJson (sortStrs t.resumes)),
            ("retryAt", encOpt t.retryAt), ("leaseAt", encOpt t.leaseAt)])]))]

def encEffect : Effect → Json
  | .setTimeout a => Json.arr #["setTimeout", toJson a]
  | .setDocument d => Json.arr #["setDocument", encDoc d]
  | .delTimeout a => Json.arr #["delTimeout", toJson a]
  | .send ⟨addr, .execute tid v⟩ => Json.arr #["send", Json.str addr, Json.mkObj [("execute", Json.arr #[Json.str tid, toJson v])]]
  | .send ⟨addr, .unblock r⟩ => Json.arr #["send", Json.str addr, Json.mkObj [("unblock", encPRec r)]]

def encData : RData → Json
  | .empty => Json.mkObj []
  | .message m => Json.str m
  | .promise p => Json.mkObj [("promise", encPRec p)]
  | .taskOnly t => Json.mkObj [("task", encTRec t)]
  | .task t p pl => Json.mkObj [("task", encTRec t), ("promise", encPRec p), ("preload", Json.arr (pl.toArray.map encPRec))]
  | .preload pl => Json.mkObj [("preload", Json.arr (pl.toArray.map encPRec))]
  | .fence kind corrId status nested pl =>
    Json.mkObj [("action", Json.mkObj [("kind", Json.str kind),
        ("head", Json.mkObj [("corrId", Json.str corrId), ("status", toJson status), ("version", "2026-04-01")]),
        ("data", encData nested)]),
      ("preload", Json.arr (pl.toArray.map encPRec))]

/-! ## The loop -/

def step (cfg : Cfg) (d : Doc) (line : Json) : Except String (Doc × Json) := do
  let now ← line.getObjValAs? Int "now"
  let (fx, reply) ← match line.getObjVal? "req" with
    | .ok r => do
      let (fx, reply) := handleExternal d (← decReq r) now cfg
      pure (fx, Json.mkObj [("status", toJson reply.status), ("data", encData reply.data)])
    | .error _ => pure (handleInternal d now cfg, Json.null)
  let d' := (committed fx).getD d
  return (d', Json.mkObj [("effects", Json.arr (fx.toArray.map encEffect)), ("reply", reply)])

partial def loop (stdin : IO.FS.Stream) (stdout : IO.FS.Stream) (cfg : Cfg) (d : Doc) : IO Unit := do
  let line ← stdin.getLine
  if line.isEmpty then return
  let line := line.trimAscii.toString
  if line.isEmpty then
    -- A blank line ends one script: the next one starts from nothing.
    stdout.putStrLn ""
    stdout.flush
    loop stdin stdout cfg {}
  else
    match Json.parse line >>= step cfg d with
    | .ok (d', out) =>
      stdout.putStrLn out.compress
      loop stdin stdout cfg d'
    | .error e =>
      stdout.putStrLn (Json.mkObj [("error", Json.str e)]).compress
      loop stdin stdout cfg d

/-- The configuration is the first argument, as JSON: `{"retryTimeout": 100, "preloadLimit": 10}`. -/
def main (args : List String) : IO Unit := do
  let cfg : Cfg ← match args.head? with
    | some a =>
      match Json.parse a with
      | .ok j => pure { retryTimeout := (j.getObjValAs? Int "retryTimeout").toOption.getD 30000,
                        preloadLimit := (j.getObjValAs? Nat "preloadLimit").toOption.getD 10 }
      | .error e => throw (IO.userError e)
    | none => pure {}
  loop (← IO.getStdin) (← IO.getStdout) cfg {}
