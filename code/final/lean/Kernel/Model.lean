/-!
# The kernel, transcribed

`kernel.py` line for line, as total functions. Every Python function has a
namesake here, and every mutation of the transaction's document is a
`Doc.modify` or a `Doc.insert`: there is no other way to change a document,
which is what makes the invariants in `Kernel/Invariants.lean` provable one
operation at a time.

What is left out, and why:

* `clock`, `gen` and `timer_name` on the document. The kernel never reads
  them; the shell owns them.
* Malformed input the Python would crash on rather than refuse — a settle
  state that is not one of the five names, a bracketed host `urlsplit`
  rejects with `ValueError`. The driver never produces them.

What is kept, because the differential in `test/test_lean.py` compares it
byte for byte: every reply status and body, every effect and its order, and
every field of the committed document.
-/

namespace Kernel

/-! ## States and tags -/

inductive PState where
  | pending | resolved | rejected | rejectedCanceled | rejectedTimedout
  deriving DecidableEq, Repr, Inhabited

inductive TState where
  | pending | acquired | suspended | halted | fulfilled
  deriving DecidableEq, Repr, Inhabited

def TAG_TARGET := "resonate:target"
def TAG_TIMER := "resonate:timer"
def TAG_DELAY := "resonate:delay"
def TAG_BRANCH := "resonate:branch"
def TAG_SCOPE := "resonate:scope"
def TAG_EXTERNAL := "resonate:external"

structure Cfg where
  retryTimeout : Int := 30000
  preloadLimit : Nat := 10
  deriving Repr

/-! ## The document -/

structure Value where
  headers : Option (List (String × String)) := none
  data : Option String := none
  deriving DecidableEq, Repr, Inhabited

abbrev Tags := List (String × String)

structure Promise where
  state : PState := .pending
  param : Value := {}
  value : Value := {}
  tags : Tags := []
  timeoutAt : Int := 0
  createdAt : Int := 0
  settledAt : Option Int := none
  callbacks : List String := []
  listeners : List String := []
  deriving DecidableEq, Repr, Inhabited

namespace Promise

def tag (p : Promise) (k : String) : Option String := p.tags.lookup k

def target (p : Promise) : Option String := p.tag TAG_TARGET

def isTimer (p : Promise) : Bool := p.tag TAG_TIMER == some "true"

/-- Awaitable and armed: scope global, external, targeted, or a timer. -/
def isExternal (p : Promise) : Bool :=
  p.tag TAG_SCOPE == some "global" || p.tag TAG_EXTERNAL == some "true"
    || p.target.isSome || p.isTimer

def timeoutState (p : Promise) : PState :=
  if p.isTimer then .resolved else .rejectedTimedout

/-- Only a pending promise with a target has a deadline the sweep fires. -/
def timeoutArmed (p : Promise) : Bool := p.state == .pending && p.target.isSome

end Promise

structure Task where
  state : TState := .pending
  version : Nat := 0
  pid : Option String := none
  ttl : Option Int := none
  /-- A set in Python; kept free of duplicates here, compared as a set. -/
  resumes : List String := []
  retryAt : Option Int := none
  leaseAt : Option Int := none
  deriving DecidableEq, Repr, Inhabited

namespace Task

def disarm (t : Task) : Task := { t with retryAt := none, leaseAt := none }
def armRetry (t : Task) (at_ : Int) : Task := { t with retryAt := some at_, leaseAt := none }
def armLease (t : Task) (at_ : Int) : Task := { t with leaseAt := some at_, retryAt := none }

end Task

structure Obj where
  id : String
  promise : Promise
  task : Option Task := none
  deriving DecidableEq, Repr, Inhabited

/-! ### Dewey order -/

inductive Seg where
  | num (n : Nat)
  | str (s : String)
  deriving DecidableEq, Repr

/-- `(0, int)` sorts before `(1, str)`, as the Python tuple does. -/
def Seg.lt : Seg → Seg → Bool
  | .num a, .num b => decide (a < b)
  | .num _, .str _ => true
  | .str _, .num _ => false
  | .str a, .str b => decide (a < b)

def keyLt : List Seg → List Seg → Bool
  | [], [] => false
  | [], _ :: _ => true
  | _ :: _, [] => false
  | a :: as, b :: bs => if a.lt b then true else if b.lt a then false else keyLt as bs

/-- `re.split(r"[:.]", id)`. -/
def splitSegs (s : String) : List String :=
  go s.toList []
where
  go : List Char → List Char → List String
    | [], acc => [String.ofList acc.reverse]
    | c :: cs, acc =>
      if c == ':' || c == '.' then String.ofList acc.reverse :: go cs []
      else go cs (c :: acc)

/-- `str.isdigit`, for the ASCII ids the driver uses. -/
def isDigits (s : String) : Bool := !s.isEmpty && s.all Char.isDigit

def digitsVal (s : String) : Nat :=
  s.toList.foldl (fun n c => 10 * n + (c.toNat - '0'.toNat)) 0

def dewey (id : String) : List Seg :=
  (splitSegs id).map fun seg => if isDigits seg then .num (digitsVal seg) else .str seg

structure Doc where
  /-- Kept sorted by `dewey (id)`. -/
  objects : List Obj := []
  /-- The one deadline armed for this origin. -/
  timerAt : Option Int := none
  deriving DecidableEq, Repr, Inhabited

namespace Doc

def ids (d : Doc) : List String := d.objects.map (·.id)

def get (d : Doc) (id : String) : Option Obj := d.objects.find? (·.id == id)

/-- The only way an existing object changes. -/
def modify (d : Doc) (id : String) (f : Obj → Obj) : Doc :=
  { d with objects := d.objects.map fun o => if o.id == id then f o else o }

/-- `insort`, to the right of any equal key. -/
def insertSorted (o : Obj) : List Obj → List Obj
  | [] => [o]
  | x :: xs => if keyLt (dewey o.id) (dewey x.id) then o :: x :: xs else x :: insertSorted o xs

/-- The only way an object is born. -/
def insert (d : Doc) (o : Obj) : Doc := { d with objects := insertSorted o d.objects }

end Doc

def objDeadlines (o : Obj) : List Int :=
  (if o.promise.timeoutArmed then [o.promise.timeoutAt] else [])
    ++ (match o.task with
        | none => []
        | some t => t.retryAt.toList ++ t.leaseAt.toList)

/-- The earliest deadline the document has armed. -/
def minDeadline (d : Doc) : Option Int := (d.objects.flatMap objDeadlines).min?

/-! ## Requests -/

structure PromiseCreate where
  id : String
  timeoutAt : Int
  param : Value := {}
  tags : Tags := []
  deriving Repr

structure PromiseSettle where
  id : String
  state : PState
  value : Value := {}
  deriving Repr

inductive FenceAction where
  | create (a : PromiseCreate)
  | settle (a : PromiseSettle)
  deriving Repr

inductive Req where
  | promiseGet (id : String)
  | promiseCreate (r : PromiseCreate)
  | promiseSettle (r : PromiseSettle)
  | promiseRegisterCallback (awaited awaiter : String)
  | promiseRegisterListener (awaited address : String)
  | taskGet (id : String)
  | taskCreate (pid : String) (ttl : Int) (action : PromiseCreate)
  | taskAcquire (id : String) (version : Nat) (pid : String) (ttl : Int)
  | taskRelease (id : String) (version : Nat)
  | taskFulfill (id : String) (version : Nat) (action : PromiseSettle)
  | taskSuspend (id : String) (version : Nat) (awaited : List String)
  | taskFence (id : String) (version : Nat) (corrId : String) (action : FenceAction)
  | taskHeartbeat (pid : String) (tasks : List (String × Nat))
  | taskHalt (id : String)
  | taskContinue (id : String)
  deriving Repr

/-! ## Effects and replies -/

/-- A promise as the wire reports it: `Promise.to_record`, which carries
neither callbacks nor listeners. -/
abbrev PRec := String × Promise
/-- `Task.to_record`. -/
abbrev TRec := String × Task

inductive Msg where
  | execute (taskId : String) (version : Nat)
  | unblock (promise : PRec)
  deriving Repr

structure Send where
  address : String
  msg : Msg
  deriving Repr

inductive Effect where
  | setTimeout (at_ : Int)
  | setDocument (doc : Doc)
  | delTimeout (at_ : Int)
  | send (s : Send)
  deriving Repr

inductive RData where
  | empty
  | message (s : String)
  | promise (p : PRec)
  | taskOnly (t : TRec)
  | task (t : TRec) (p : PRec) (preload : List PRec)
  | preload (ps : List PRec)
  | fence (kind corrId : String) (status : Nat) (nested : RData) (preload : List PRec)
  deriving Repr

structure Reply where
  status : Nat
  data : RData
  deriving Repr

def Reply.ok (d : RData) : Reply := ⟨200, d⟩
def Reply.err (s : Nat) (m : String) : Reply := ⟨s, .message m⟩

def isSettleState (s : PState) : Bool :=
  s == .resolved || s == .rejected || s == .rejectedCanceled

/-- Everything before the first `:`. -/
def originOf (id : String) : String := String.ofList (id.toList.takeWhile (· ≠ ':'))

/-! ### Addresses

`re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", a)`, then `urlsplit`'s netloc for the
five schemes that must have one. `urlsplit` first deletes tabs and newlines
anywhere, which is why `http:/\t/x` is an address. -/

def isSchemeChar (c : Char) : Bool := c.isAlphanum || c == '+' || c == '.' || c == '-'

def schemeOf? (cs : List Char) : Option (List Char × List Char) :=
  match cs with
  | c :: _ =>
    if c.isAlpha then
      let s := cs.takeWhile isSchemeChar
      match cs.drop s.length with
      | ':' :: rest => some (s, rest)
      | _ => none
    else none
  | [] => none

def isValidAddress (a : String) : Bool :=
  match schemeOf? a.toList with
  | none => false
  | some (scheme, _) =>
    let cleaned := a.toList.filter fun c => c != '\t' && c != '\r' && c != '\n'
    match schemeOf? cleaned with
    | none => false
    | some (_, rest) =>
      let s := String.ofList (scheme.map Char.toLower)
      if ["http", "https", "ws", "wss", "ftp"].contains s then
        match rest with
        | '/' :: '/' :: host => !(host.takeWhile fun c => c != '/' && c != '?' && c != '#').isEmpty
        | _ => false
      else true

/-! ## A decision in progress -/

structure Tx where
  doc : Doc
  sends : List Send := []

namespace Tx
def modify (tx : Tx) (id : String) (f : Obj → Obj) : Tx := { tx with doc := tx.doc.modify id f }
end Tx

/-- Queue a dispatch. A promise with no target has nowhere to send. -/
def sendExecute (tx : Tx) (taskId : String) (version : Nat) : Tx :=
  match tx.doc.get taskId with
  | some o =>
    match o.promise.target with
    | some a => { tx with sends := tx.sends ++ [⟨a, .execute taskId version⟩] }
    | none => tx
  | none => tx

def Promise.record (id : String) (p : Promise) : PRec := (id, p)

/-! ## Shared state transitions -/

/-- The own task of a settled promise is done. -/
def fulfilTask (o : Obj) : Obj :=
  match o.task with
  | some t =>
    if t.state ≠ .fulfilled then
      { o with task := some { t.disarm with state := .fulfilled, pid := none, ttl := none, resumes := [] } }
    else o
  | none => o

def addResume (id : String) (rs : List String) : List String :=
  if rs.contains id then rs else rs ++ [id]

/-- One awaiter observes the settlement of `id`. -/
def wake (id : String) (now : Int) (cfg : Cfg) (o : Obj) : Obj :=
  match o.task with
  | some t =>
    if t.state == .suspended then
      { o with task := some { t.armRetry (now + cfg.retryTimeout) with state := .pending, resumes := [id] } }
    else if t.state == .pending || t.state == .acquired || t.state == .halted then
      { o with task := some { t with resumes := addResume id t.resumes } }
    else o
  | none => o

def wakeOne (id : String) (now : Int) (cfg : Cfg) (tx : Tx) (awaiter : String) : Tx :=
  match tx.doc.get awaiter with
  | none => tx
  | some ao =>
    match ao.task with
    | none => tx
    | some t =>
      if ao.promise.state ≠ .pending || now ≥ ao.promise.timeoutAt then tx
      else if t.state == .suspended then
        sendExecute (tx.modify awaiter (wake id now cfg)) awaiter t.version
      else tx.modify awaiter (wake id now cfg)

def clearCallbacks (o : Obj) : Obj := { o with promise := { o.promise with callbacks := [] } }
def clearListeners (o : Obj) : Obj := { o with promise := { o.promise with listeners := [] } }

/-- The settlement chain, in one pass and in this order: fulfil the promise's
own task, wake its awaiters, notify its listeners. -/
def triggerSettlement (tx : Tx) (id : String) (now : Int) (cfg : Cfg) : Tx :=
  match tx.doc.get id with
  | none => tx
  | some o =>
    let tx := tx.modify id fulfilTask
    let tx := tx.modify id clearCallbacks
    let tx := o.promise.callbacks.foldl (wakeOne id now cfg) tx
    match tx.doc.get id with
    | none => tx
    | some o' =>
      let tx := tx.modify id clearListeners
      { tx with sends := tx.sends ++ o'.promise.listeners.map fun a => ⟨a, .unblock (Promise.record id { o'.promise with listeners := [] })⟩ }

def setSettled (state : PState) (value : Value) (now : Int) (o : Obj) : Obj :=
  { o with promise := { o.promise with state := state, value := value, settledAt := some now } }

/-- Settle a pending promise and run its chain. The record is the one captured
before the chain runs. -/
def settle (tx : Tx) (id : String) (state : PState) (value : Value) (now : Int) (cfg : Cfg) : Tx × PRec :=
  let tx := tx.modify id (setSettled state value now)
  let rec_ := match tx.doc.get id with
    | some o => Promise.record id o.promise
    | none => (id, {})
  (triggerSettlement tx id now cfg, rec_)

def preload (d : Doc) (id : String) (cfg : Cfg) : List PRec :=
  match d.get id with
  | none => []
  | some o =>
    match o.promise.tag TAG_BRANCH with
    | none => []
    | some "" => []
    | some b =>
      ((d.objects.filter fun x => x.id != id && x.promise.tag TAG_BRANCH == some b).map
        fun x => Promise.record x.id x.promise).take cfg.preloadLimit

/-- A promise, alone: born settled if it is created past its deadline. -/
def newPromise (r : PromiseCreate) (now : Int) : Promise :=
  let p : Promise := { param := r.param, tags := r.tags, timeoutAt := r.timeoutAt,
                       createdAt := if now ≥ r.timeoutAt then r.timeoutAt else now }
  if now ≥ r.timeoutAt then { p with state := p.timeoutState, settledAt := some r.timeoutAt } else p

/-! ## The sweep -/

def expiring (now : Int) (o : Obj) : Bool := o.promise.state == .pending && now ≥ o.promise.timeoutAt

def expire (o : Obj) : Obj :=
  { o with promise := { o.promise with state := o.promise.timeoutState, settledAt := some o.promise.timeoutAt } }

def retryDue (now : Int) (o : Obj) : Bool :=
  match o.task with
  | some t => t.state == .pending && (match t.retryAt with | some r => r ≤ now | none => false)
  | none => false

def leaseDue (now : Int) (o : Obj) : Bool :=
  match o.task with
  | some t => t.state == .acquired && (match t.leaseAt with | some l => l ≤ now | none => false)
  | none => false

def rearm (now : Int) (cfg : Cfg) (o : Obj) : Obj :=
  { o with task := o.task.map fun t => t.armRetry (now + cfg.retryTimeout) }

def reclaim (now : Int) (cfg : Cfg) (o : Obj) : Obj :=
  { o with task := o.task.map fun t => { t.armRetry (now + cfg.retryTimeout) with state := .pending, pid := none, ttl := none } }

def taskVersion (o : Obj) : Nat := (o.task.map (·.version)).getD 0

/-- The dispatch for an object's task, if its promise has somewhere to send it. -/
def dispatchOf (o : Obj) : List Send :=
  match o.promise.target with
  | some a => [⟨a, .execute o.id (taskVersion o)⟩]
  | none => []

/-- Phase 3 and phase 4 share a shape: change every object the predicate
picks, in document order, and dispatch each. Python walks `doc.objects` and
mutates each object in place, which is a map. -/
def sweepPhase (due : Obj → Bool) (f : Obj → Obj) (tx : Tx) : Tx :=
  { doc := { tx.doc with objects := tx.doc.objects.map fun o => if due o then f o else o },
    sends := tx.sends ++ (tx.doc.objects.filter due).flatMap dispatchOf }

/-- Everything whose deadline is at or before `now`, in four phases, without
the timer effects. -/
def sweepTx (d : Doc) (now : Int) (cfg : Cfg) : Tx :=
  let expired := (d.objects.filter (expiring now)).map (·.id)
  -- Phase 1: settle first, all of them.
  let tx : Tx := { doc := { d with objects := d.objects.map fun o => if expiring now o then expire o else o } }
  -- Phase 2: the chains, in id order.
  let tx := expired.foldl (fun tx id => triggerSettlement tx id now cfg) tx
  -- Phase 3: re-dispatch pending tasks past their retry deadline.
  let tx := sweepPhase (retryDue now) (rearm now cfg) tx
  -- Phase 4: expire leases.
  sweepPhase (leaseDue now) (reclaim now cfg) tx

/-- Arm the new timer, commit the document, clear the old timer. -/
def linearize (old : Option Int) (d : Doc) : Doc × List Effect :=
  let new := minDeadline d
  let d := { d with timerAt := new }
  let arm := match new with
    | some n => if old ≠ new then [Effect.setTimeout n] else []
    | none => []
  let disarm := match old with
    | some o => if old ≠ new then [Effect.delTimeout o] else []
    | none => []
  (d, arm ++ [Effect.setDocument d] ++ disarm)

def handleInternal (d : Doc) (now : Int) (cfg : Cfg) : List Effect :=
  let tx := sweepTx d now cfg
  (linearize d.timerAt tx.doc).2 ++ tx.sends.map .send

/-! ## Promise operations -/

def promiseGet (tx : Tx) (id : String) : Tx × Reply :=
  match tx.doc.get id with
  | none => (tx, .err 404 "Promise not found")
  | some o => (tx, .ok (.promise (Promise.record id o.promise)))

/-- The checks `promise.create` makes on its tags, before it looks anything up. -/
def createRefusal (r : PromiseCreate) : Option Reply :=
  let address := r.tags.lookup TAG_TARGET
  if (address.map (!isValidAddress ·)).getD false then some (.err 400 "Invalid resonate:target address")
  else if r.tags.lookup TAG_TIMER == some "true" && address.isSome then
    some (.err 400 "A timer promise must not have a resonate:target tag")
  else match r.tags.lookup TAG_DELAY with
    | none => none
    | some delay =>
      if !isDigits delay then some (.err 400 "resonate:delay must be a non-negative integer")
      else if (digitsVal delay : Int) ≥ r.timeoutAt then some (.err 400 "resonate:delay must be less than timeoutAt")
      else if address.isNone then some (.err 400 "resonate:delay requires a resonate:target tag")
      else none

def promiseCreate (tx : Tx) (r : PromiseCreate) (now : Int) (cfg : Cfg) : Tx × Reply :=
  match createRefusal r with
  | some e => (tx, e)
  | none =>
  match tx.doc.get r.id with
  | some o => (tx, .ok (.promise (Promise.record r.id o.promise)))
  | none =>
    let p := newPromise r now
    let reply := Reply.ok (.promise (Promise.record r.id p))
    match p.target with
    | none => ({ tx with doc := tx.doc.insert ⟨r.id, p, none⟩ }, reply)
    | some _ =>
      if p.state ≠ .pending then
        ({ tx with doc := tx.doc.insert ⟨r.id, p, some { state := .fulfilled }⟩ }, reply)
      else
        match (r.tags.lookup TAG_DELAY).map digitsVal with
        | some delay =>
          if now < delay then
            ({ tx with doc := tx.doc.insert ⟨r.id, p, some (({} : Task).armRetry delay)⟩ }, reply)
          else
            (sendExecute { tx with doc := tx.doc.insert ⟨r.id, p, some (({} : Task).armRetry (p.createdAt + cfg.retryTimeout))⟩ } r.id 0, reply)
        | none =>
          (sendExecute { tx with doc := tx.doc.insert ⟨r.id, p, some (({} : Task).armRetry (p.createdAt + cfg.retryTimeout))⟩ } r.id 0, reply)

def promiseSettle (tx : Tx) (r : PromiseSettle) (now : Int) (cfg : Cfg) : Tx × Reply :=
  if !isSettleState r.state then (tx, .err 400 "Invalid settle state")
  else match tx.doc.get r.id with
  | none => (tx, .err 404 "Promise not found")
  | some o =>
    if o.promise.state ≠ .pending then (tx, .ok (.promise (Promise.record r.id o.promise)))
    else
      let (tx, rec_) := settle tx r.id r.state r.value now cfg
      (tx, .ok (.promise rec_))

def addCallback (awaiter : String) (o : Obj) : Obj :=
  if o.promise.callbacks.contains awaiter then o
  else { o with promise := { o.promise with callbacks := o.promise.callbacks ++ [awaiter] } }

def addListener (address : String) (o : Obj) : Obj :=
  if o.promise.listeners.contains address then o
  else { o with promise := { o.promise with listeners := o.promise.listeners ++ [address] } }

def promiseRegisterCallback (tx : Tx) (awaited awaiter : String) : Tx × Reply :=
  if awaited == awaiter then (tx, .err 400 "Awaited and awaiter must be different promises")
  else if originOf awaited != originOf awaiter then (tx, .err 400 "Awaiter and awaited must belong to the same origin")
  else match tx.doc.get awaited with
  | none => (tx, .err 404 "Awaited promise not found")
  | some ad =>
    match tx.doc.get awaiter with
    | none => (tx, .err 422 "Awaiter promise not found")
    | some ar =>
      if ar.promise.target.isNone then (tx, .err 422 "Awaiter promise has no resonate:target tag")
      else if !ad.promise.isExternal then (tx, .err 422 "Awaited promise is not awaitable")
      else
        let reply := Reply.ok (.promise (Promise.record awaited ad.promise))
        if ad.promise.state == .pending && ar.promise.state == .pending then
          (tx.modify awaited (addCallback awaiter), reply)
        else (tx, reply)

def promiseRegisterListener (tx : Tx) (awaited address : String) : Tx × Reply :=
  if !isValidAddress address then (tx, .err 400 "Invalid listener address")
  else match tx.doc.get awaited with
  | none => (tx, .err 404 "Awaited promise not found")
  | some o =>
    if !o.promise.isExternal then (tx, .err 422 "Awaited promise is not awaitable")
    else
      let reply := Reply.ok (.promise (Promise.record awaited o.promise))
      if o.promise.state == .pending then (tx.modify awaited (addListener address), reply)
      else (tx, reply)

/-! ## Task operations -/

def taskGet (tx : Tx) (id : String) : Tx × Reply :=
  match tx.doc.get id with
  | some ⟨_, _, some t⟩ => (tx, .ok (.taskOnly (id, t)))
  | _ => (tx, .err 404 "Task not found")

/-- Claim: bump the version (the fence), take the lease, drop the buffered
resumes. -/
def claim (pid : String) (ttl : Int) (now : Int) (o : Obj) : Obj :=
  { o with task := o.task.map fun t =>
      { t.armLease (now + ttl) with state := .acquired, version := t.version + 1, pid := some pid,
                                    ttl := some ttl, resumes := [] } }

def taskReply (d : Doc) (id : String) (cfg : Cfg) (withPreload : Bool) : Reply :=
  match d.get id with
  | some ⟨_, p, some t⟩ => .ok (.task (id, t) (id, p) (if withPreload then preload d id cfg else []))
  | _ => .err 404 "Task not found"

def taskCreate (tx : Tx) (pid : String) (ttl : Int) (a : PromiseCreate) (now : Int) (cfg : Cfg) : Tx × Reply :=
  match a.tags.lookup TAG_TARGET with
  | none => (tx, .err 400 "Action must have a resonate:target tag")
  | some address =>
  if !isValidAddress address then (tx, .err 400 "Invalid resonate:target address")
  else if a.tags.lookup TAG_TIMER == some "true" then (tx, .err 400 "A timer promise must not have a resonate:target tag")
  else if (a.tags.lookup TAG_DELAY).isSome then (tx, .err 400 "Action must not have a resonate:delay tag")
  else if ttl < 1 then (tx, .err 400 "TTL must be a positive integer")
  else match tx.doc.get a.id with
  | some o =>
    match o.task with
    | some t =>
      if t.state == .pending then
        let tx := tx.modify a.id (claim pid ttl now)
        (tx, taskReply tx.doc a.id cfg true)
      else if t.state == .fulfilled then (tx, taskReply tx.doc a.id cfg false)
      else (tx, .err 409 "Already exists")
    | none => (tx, .err 422 "The promise does not have a resonate:target tag")
  | none =>
    let p := newPromise a now
    let t : Task := if p.state == .pending
      then { (({} : Task).armLease (now + ttl)) with state := .acquired, version := 1, pid := some pid, ttl := some ttl }
      else { state := .fulfilled }
    let tx := { tx with doc := tx.doc.insert ⟨a.id, p, some t⟩ }
    (tx, taskReply tx.doc a.id cfg true)

/-- The guard every fenced task operation shares. -/
def acquiredAt (tx : Tx) (id : String) (version : Nat) : Option Task :=
  match tx.doc.get id with
  | some ⟨_, _, some t⟩ => if t.state == .acquired && t.version == version then some t else none
  | _ => none

def hasTask (tx : Tx) (id : String) : Bool :=
  match tx.doc.get id with
  | some ⟨_, _, some _⟩ => true
  | _ => false

def taskAcquire (tx : Tx) (id : String) (version : Nat) (pid : String) (ttl : Int) (now : Int) (cfg : Cfg) : Tx × Reply :=
  if ttl < 1 then (tx, .err 400 "TTL must be a positive integer")
  else match tx.doc.get id with
  | some ⟨_, _, some t⟩ =>
    if t.state ≠ .pending then (tx, .err 409 "Task is not pending")
    else if t.version ≠ version then (tx, .err 409 "Version mismatch")
    else
      let tx := tx.modify id (claim pid ttl now)
      (tx, taskReply tx.doc id cfg true)
  | _ => (tx, .err 404 "Task not found")

def unclaim (now : Int) (cfg : Cfg) (o : Obj) : Obj :=
  { o with task := o.task.map fun t => { t.armRetry (now + cfg.retryTimeout) with state := .pending, pid := none, ttl := none } }

def taskRelease (tx : Tx) (id : String) (version : Nat) (now : Int) (cfg : Cfg) : Tx × Reply :=
  if !hasTask tx id then (tx, .err 404 "Task not found")
  else match acquiredAt tx id version with
  | none => (tx, .err 409 "Task version mismatch or invalid state")
  | some t => (sendExecute (tx.modify id (unclaim now cfg)) id t.version, .ok .empty)

def taskFulfill (tx : Tx) (id : String) (version : Nat) (a : PromiseSettle) (now : Int) (cfg : Cfg) : Tx × Reply :=
  if a.id != id then (tx, .err 400 "Action ID must match the task ID")
  else if !isSettleState a.state then (tx, .err 400 "Invalid settle state")
  else if !hasTask tx id then (tx, .err 404 "Task not found")
  else match acquiredAt tx id version with
  | none => (tx, .err 409 "Task version mismatch or invalid state")
  | some _ =>
    match tx.doc.get a.id with
    | none => (tx, .err 404 "Promise not found")
    | some po =>
      if po.promise.state ≠ .pending then
        (tx.modify id fulfilTask, .ok (.promise (Promise.record a.id po.promise)))
      else
        let (tx, rec_) := settle tx a.id a.state a.value now cfg
        (tx, .ok (.promise rec_))

def clearResumes (o : Obj) : Obj := { o with task := o.task.map fun t => { t with resumes := [] } }

def park (o : Obj) : Obj :=
  { o with task := o.task.map fun t => { t.disarm with state := .suspended, pid := none, ttl := none } }

def taskSuspend (tx : Tx) (id : String) (version : Nat) (awaited : List String) (cfg : Cfg) : Tx × Reply :=
  if awaited.isEmpty then (tx, .err 400 "Actions array cannot be empty")
  else if awaited.contains id then (tx, .err 400 "Action awaited promise must not equal the task ID")
  else if awaited.eraseDups.length != awaited.length then (tx, .err 400 "Awaited promise IDs must be unique")
  else if awaited.any (originOf · != originOf id) then (tx, .err 400 "Awaited promise must belong to the same origin as the task")
  else if !hasTask tx id then (tx, .err 404 "Task not found")
  else match acquiredAt tx id version with
  | none => (tx, .err 409 "Task is not acquired or version mismatch")
  | some _ =>
    if awaited.any (fun a => (tx.doc.get a).isNone) then (tx, .err 422 "Awaited promise not found")
    else if awaited.any (fun a => match tx.doc.get a with | some o => !o.promise.isExternal | none => false) then
      (tx, .err 422 "Awaited promise is not awaitable")
    else
      let anySettled := awaited.any fun a => match tx.doc.get a with
        | some o => o.promise.state ≠ .pending
        | none => false
      let tx := tx.modify id clearResumes
      if anySettled then (tx, ⟨300, .preload (preload tx.doc id cfg)⟩)
      else
        let tx := awaited.foldl (fun tx a => tx.modify a (addCallback id)) tx
        (tx.modify id park, .ok .empty)

def taskFence (tx : Tx) (id : String) (version : Nat) (corrId : String) (a : FenceAction) (now : Int) (cfg : Cfg) : Tx × Reply :=
  let aid := match a with | .create c => c.id | .settle s => s.id
  if aid == id then (tx, .err 400 "Action ID must not equal the task ID")
  else if !hasTask tx id then (tx, .err 404 "Task not found")
  else match acquiredAt tx id version with
  | none => (tx, .err 409 "Version mismatch")
  | some _ =>
    match a with
    | .create c =>
      let (tx, nested) := promiseCreate tx c now cfg
      if nested.status == 400 then (tx, nested)
      else (tx, .ok (.fence "promise.create" corrId nested.status nested.data (preload tx.doc id cfg)))
    | .settle s =>
      let (tx, nested) := promiseSettle tx s now cfg
      (tx, .ok (.fence "promise.settle" corrId nested.status nested.data (preload tx.doc id cfg)))

def beat (pid : String) (version : Nat) (now : Int) (o : Obj) : Obj :=
  match o.task with
  | some t =>
    if t.state == .acquired && t.version == version && t.pid == some pid then
      match t.ttl with
      | some ttl => { o with task := some (t.armLease (now + ttl)) }
      | none => o
    else o
  | none => o

def taskHeartbeat (tx : Tx) (pid : String) (tasks : List (String × Nat)) (now : Int) : Tx × Reply :=
  if (tasks.map fun (id, _) => originOf id).eraseDups.length > 1 then
    (tx, .err 400 "All tasks must belong to the same origin")
  else (tasks.foldl (fun tx (id, v) => tx.modify id (beat pid v now)) tx, .ok .empty)

def halt (o : Obj) : Obj :=
  { o with task := o.task.map fun t => { t.disarm with state := .halted, pid := none, ttl := none } }

def taskHalt (tx : Tx) (id : String) : Tx × Reply :=
  match tx.doc.get id with
  | some ⟨_, _, some t⟩ =>
    if t.state == .fulfilled then (tx, .err 409 "Task is fulfilled")
    else if t.state == .halted then (tx, .ok .empty)
    else (tx.modify id halt, .ok .empty)
  | _ => (tx, .err 404 "Task not found")

def resume (now : Int) (cfg : Cfg) (o : Obj) : Obj :=
  { o with task := o.task.map fun t => { t.armRetry (now + cfg.retryTimeout) with state := .pending } }

def taskContinue (tx : Tx) (id : String) (now : Int) (cfg : Cfg) : Tx × Reply :=
  match tx.doc.get id with
  | some ⟨_, _, some t⟩ =>
    if t.state ≠ .halted then (tx, .err 409 "Task is not halted")
    else (sendExecute (tx.modify id (resume now cfg)) id t.version, .ok .empty)
  | _ => (tx, .err 404 "Task not found")

/-! ## The two entry points -/

def decide_ (tx : Tx) (req : Req) (now : Int) (cfg : Cfg) : Tx × Reply :=
  match req with
  | .promiseGet id => promiseGet tx id
  | .promiseCreate r => promiseCreate tx r now cfg
  | .promiseSettle r => promiseSettle tx r now cfg
  | .promiseRegisterCallback awaited awaiter => promiseRegisterCallback tx awaited awaiter
  | .promiseRegisterListener awaited address => promiseRegisterListener tx awaited address
  | .taskGet id => taskGet tx id
  | .taskCreate pid ttl a => taskCreate tx pid ttl a now cfg
  | .taskAcquire id v pid ttl => taskAcquire tx id v pid ttl now cfg
  | .taskRelease id v => taskRelease tx id v now cfg
  | .taskFulfill id v a => taskFulfill tx id v a now cfg
  | .taskSuspend id v awaited => taskSuspend tx id v awaited cfg
  | .taskFence id v c a => taskFence tx id v c a now cfg
  | .taskHeartbeat pid tasks => taskHeartbeat tx pid tasks now
  | .taskHalt id => taskHalt tx id
  | .taskContinue id => taskContinue tx id now cfg

/-- A sweep dispatch the request overtook is dropped: the task it names is no
longer pending at that version. -/
def stillDue (d : Doc) : Send → Bool
  | ⟨_, .execute tid v⟩ =>
    match d.get tid with
    | some ⟨_, _, some t⟩ => t.state == .pending && t.version == v
    | _ => false
  | ⟨_, .unblock _⟩ => true

def handleExternal (d : Doc) (req : Req) (now : Int) (cfg : Cfg) : List Effect × Reply :=
  let swept := sweepTx d now cfg
  let start : Tx := { doc := { swept.doc with timerAt := minDeadline swept.doc } }
  let r := decide_ start req now cfg
  ((linearize d.timerAt r.1.doc).2 ++ (swept.sends.filter (stillDue r.1.doc)).map .send ++ r.1.sends.map .send, r.2)

/-- The committed document, which is the transition. -/
def committed (fx : List Effect) : Option Doc :=
  fx.findSome? fun | .setDocument d => some d | _ => none

end Kernel
