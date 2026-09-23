import Kernel.Theorems

/-!
# What a step can never undo

`Evolves o o'` is everything about one object that no step may take back:

* its id, tags, param, deadline and creation time never change;
* once its promise has settled, the settlement — state, value, `settledAt` —
  never changes again. First writer wins, and every writer after it is told
  what the first one wrote;
* it keeps a task if it had one, and never gains one;
* the task's version, which is its fencing token, never goes down;
* a fulfilled task stays fulfilled.

`DocEvolves d d'` says every object of `d` survives into `d'` and evolved.
Both are reflexive and transitive, and `reachable_evolves` is the statement
over whole runs: from any reachable document, along any sequence of steps.
-/

namespace Kernel

open Doc

structure Evolves (o o' : Obj) : Prop where
  id : o'.id = o.id
  tags : o'.promise.tags = o.promise.tags
  param : o'.promise.param = o.promise.param
  timeoutAt : o'.promise.timeoutAt = o.promise.timeoutAt
  createdAt : o'.promise.createdAt = o.promise.createdAt
  settled : o.promise.state ≠ .pending →
    o'.promise.state = o.promise.state ∧ o'.promise.value = o.promise.value ∧ o'.promise.settledAt = o.promise.settledAt
  task : o'.task.isSome = o.task.isSome
  version : ∀ t t', o.task = some t → o'.task = some t' → t.version ≤ t'.version
  done : ∀ t t', o.task = some t → o'.task = some t' → t.state = .fulfilled → t'.state = .fulfilled

theorem Evolves.refl (o : Obj) : Evolves o o :=
  ⟨rfl, rfl, rfl, rfl, rfl, fun _ => ⟨rfl, rfl, rfl⟩, rfl,
    fun t t' h h' => by rw [h] at h'; cases h'; exact Nat.le_refl _,
    fun t t' h h' hf => by rw [h] at h'; cases h'; exact hf⟩

theorem Evolves.trans {a b c : Obj} (h1 : Evolves a b) (h2 : Evolves b c) : Evolves a c := by
  refine ⟨h2.id.trans h1.id, h2.tags.trans h1.tags, h2.param.trans h1.param, h2.timeoutAt.trans h1.timeoutAt,
    h2.createdAt.trans h1.createdAt, ?_, h2.task.trans h1.task, ?_, ?_⟩
  · intro hs
    obtain ⟨e1, v1, s1⟩ := h1.settled hs
    obtain ⟨e2, v2, s2⟩ := h2.settled (by rw [e1]; exact hs)
    exact ⟨e2.trans e1, v2.trans v1, s2.trans s1⟩
  · intro t t'' ha hc
    cases hb : b.task with
    | none => have := h2.task; rw [hc, hb] at this; cases this
    | some t' => exact Nat.le_trans (h1.version t t' ha hb) (h2.version t' t'' hb hc)
  · intro t t'' ha hc hf
    cases hb : b.task with
    | none => have := h2.task; rw [hc, hb] at this; cases this
    | some t' => exact h2.done t' t'' hb hc (h1.done t t' ha hb hf)

def DocEvolves (d d' : Doc) : Prop := ∀ o ∈ d.objects, ∃ o' ∈ d'.objects, Evolves o o'

theorem DocEvolves.refl (d : Doc) : DocEvolves d d := fun o ho => ⟨o, ho, Evolves.refl o⟩

theorem DocEvolves.trans {a b c : Doc} (h1 : DocEvolves a b) (h2 : DocEvolves b c) : DocEvolves a c := by
  intro o ho
  obtain ⟨o', ho', e1⟩ := h1 o ho
  obtain ⟨o'', ho'', e2⟩ := h2 o' ho'
  exact ⟨o'', ho'', e1.trans e2⟩

/-! ## The writes -/

theorem evolves_map {d : Doc} {g : Obj → Obj} (h : ∀ o ∈ d.objects, Evolves o (g o)) (t) :
    DocEvolves d { objects := d.objects.map g, timerAt := t } :=
  fun o ho => ⟨g o, List.mem_map_of_mem ho, h o ho⟩

theorem evolves_modify {d : Doc} {id f} (h : ∀ o ∈ d.objects, o.id = id → Evolves o (f o)) :
    DocEvolves d (d.modify id f) := by
  intro o ho
  refine ⟨if o.id == id then f o else o, List.mem_map_of_mem ho, ?_⟩
  by_cases hx : o.id = id
  · simpa [hx] using h o ho hx
  · simpa [hx] using Evolves.refl o

theorem evolves_modify_get {d : Doc} (hn : d.ids.Nodup) {id f o} (hg : d.get id = some o)
    (he : Evolves o (f o)) : DocEvolves d (d.modify id f) :=
  evolves_modify fun o' ho' hx => by rw [eq_of_get hn hg ho' hx]; exact he

theorem evolves_modify_any {d : Doc} {id f} (he : ∀ o, Evolves o (f o)) : DocEvolves d (d.modify id f) :=
  evolves_modify fun o _ _ => he o

theorem evolves_insert (d : Doc) (o) : DocEvolves d (d.insert o) :=
  fun o' ho' => ⟨o', mem_insert.2 (Or.inr ho'), Evolves.refl o'⟩

/-- Changing the task alone, keeping its version and not reviving a
fulfilled one. -/
theorem evolves_taskMap {o : Obj} {g : Task → Task}
    (hv : ∀ t, o.task = some t → t.version ≤ (g t).version)
    (hd : ∀ t, o.task = some t → t.state = .fulfilled → (g t).state = .fulfilled) :
    Evolves o { o with task := o.task.map g } := by
  refine ⟨rfl, rfl, rfl, rfl, rfl, fun _ => ⟨rfl, rfl, rfl⟩, by simp, ?_, ?_⟩
  · intro t t' h h'; simp [h] at h'; subst h'; exact hv t h
  · intro t t' h h' hf; simp [h] at h'; subst h'; exact hd t h hf

theorem evolves_promise {o o' : Obj} (hi : o'.id = o.id) (hp : o'.promise.tags = o.promise.tags ∧
    o'.promise.param = o.promise.param ∧ o'.promise.timeoutAt = o.promise.timeoutAt ∧
    o'.promise.createdAt = o.promise.createdAt ∧ o'.promise.state = o.promise.state ∧
    o'.promise.value = o.promise.value ∧ o'.promise.settledAt = o.promise.settledAt) (ht : o'.task = o.task) :
    Evolves o o' := by
  obtain ⟨a, b, c, e, f, g, i⟩ := hp
  refine ⟨hi, a, b, c, e, fun _ => ⟨f, g, i⟩, by rw [ht], ?_, ?_⟩
  · intro t t' h h'; rw [ht, h] at h'; cases h'; exact Nat.le_refl _
  · intro t t' h h' hf; rw [ht, h] at h'; cases h'; exact hf

section Objects

theorem evolves_fulfilTask (o : Obj) : Evolves o (fulfilTask o) := by
  unfold fulfilTask
  split
  · rename_i t ht
    split
    · refine ⟨rfl, rfl, rfl, rfl, rfl, fun _ => ⟨rfl, rfl, rfl⟩, by simp [ht], ?_, ?_⟩
      · intro t1 t2 h1 h2; rw [ht] at h1; cases h1; cases h2; exact Nat.le_refl _
      · intro t1 t2 h1 h2 _; cases h2; rfl
    · exact Evolves.refl o
  · exact Evolves.refl o

theorem evolves_clearCallbacks (o : Obj) : Evolves o (clearCallbacks o) :=
  evolves_promise rfl ⟨rfl, rfl, rfl, rfl, rfl, rfl, rfl⟩ rfl

theorem evolves_clearListeners (o : Obj) : Evolves o (clearListeners o) :=
  evolves_promise rfl ⟨rfl, rfl, rfl, rfl, rfl, rfl, rfl⟩ rfl

theorem evolves_addCallback (a) (o : Obj) : Evolves o (addCallback a o) := by
  unfold addCallback; split
  · exact Evolves.refl o
  · exact evolves_promise rfl ⟨rfl, rfl, rfl, rfl, rfl, rfl, rfl⟩ rfl

theorem evolves_addListener (a) (o : Obj) : Evolves o (addListener a o) := by
  unfold addListener; split
  · exact Evolves.refl o
  · exact evolves_promise rfl ⟨rfl, rfl, rfl, rfl, rfl, rfl, rfl⟩ rfl

theorem evolves_wake (id now cfg) (o : Obj) : Evolves o (wake id now cfg o) := by
  unfold wake
  split
  · rename_i t ht
    split
    · rename_i hs
      refine ⟨rfl, rfl, rfl, rfl, rfl, fun _ => ⟨rfl, rfl, rfl⟩, by simp [ht], ?_, ?_⟩
      · intro t1 t2 h1 h2; rw [ht] at h1; cases h1; cases h2; exact Nat.le_refl _
      · intro t1 t2 h1 h2 hf; rw [ht] at h1; cases h1; simp at hs; rw [hs] at hf; cases hf
    · split
      · refine ⟨rfl, rfl, rfl, rfl, rfl, fun _ => ⟨rfl, rfl, rfl⟩, by simp [ht], ?_, ?_⟩
        · intro t1 t2 h1 h2; rw [ht] at h1; cases h1; cases h2; exact Nat.le_refl _
        · intro t1 t2 h1 h2 hf; rw [ht] at h1; cases h1; cases h2; exact hf
      · exact Evolves.refl o
  · exact Evolves.refl o

theorem evolves_setSettled {s v now} {o : Obj} (hp : o.promise.state = .pending) :
    Evolves o (setSettled s v now o) := by
  refine ⟨rfl, rfl, rfl, rfl, rfl, fun h => absurd hp h, rfl, ?_, ?_⟩
  · intro t t' h h'; simp only [setSettled] at h'; rw [h] at h'; cases h'; exact Nat.le_refl _
  · intro t t' h h' hf; simp only [setSettled] at h'; rw [h] at h'; cases h'; exact hf

theorem evolves_expire {now} (o : Obj) : Evolves o (if expiring now o then expire o else o) := by
  split
  · rename_i he
    have hp : o.promise.state = .pending := by simp [expiring] at he; exact he.1
    refine ⟨rfl, rfl, rfl, rfl, rfl, fun h => absurd hp h, rfl, ?_, ?_⟩
    · intro t t' h h'; simp only [expire] at h'; rw [h] at h'; cases h'; exact Nat.le_refl _
    · intro t t' h h' hf; simp only [expire] at h'; rw [h] at h'; cases h'; exact hf
  · exact Evolves.refl o

theorem evolves_beat (pid v now) (o : Obj) : Evolves o (beat pid v now o) := by
  unfold beat
  split
  · rename_i t ht
    split
    · split
      · refine ⟨rfl, rfl, rfl, rfl, rfl, fun _ => ⟨rfl, rfl, rfl⟩, by simp [ht], ?_, ?_⟩
        · intro t1 t2 h1 h2; rw [ht] at h1; cases h1; cases h2; exact Nat.le_refl _
        · intro t1 t2 h1 h2 hf; rw [ht] at h1; cases h1; cases h2; exact hf
      · exact Evolves.refl o
    · exact Evolves.refl o
  · exact Evolves.refl o

theorem evolves_clearResumes (o : Obj) : Evolves o (clearResumes o) :=
  evolves_taskMap (fun _ _ => Nat.le_refl _) (fun _ _ hf => hf)

end Objects

/-! ## The sweep -/

theorem evolves_wakeAll {id now cfg} :
    ∀ (l : List String) (tx : Tx), DocEvolves tx.doc (l.foldl (wakeOne id now cfg) tx).doc
  | [], tx => DocEvolves.refl _
  | a :: l, tx => by
    refine DocEvolves.trans ?_ (evolves_wakeAll l _)
    unfold wakeOne
    split
    · exact DocEvolves.refl _
    · split
      · exact DocEvolves.refl _
      · split
        · exact DocEvolves.refl _
        · split
          · simpa using evolves_modify_any (evolves_wake id now cfg)
          · simpa using evolves_modify_any (evolves_wake id now cfg)

theorem evolves_trigger {tx : Tx} {id now cfg} : DocEvolves tx.doc (triggerSettlement tx id now cfg).doc := by
  unfold triggerSettlement
  split
  · exact DocEvolves.refl _
  · rename_i o _
    dsimp only
    have h1 : DocEvolves tx.doc ((tx.doc.modify id fulfilTask).modify id clearCallbacks) :=
      (evolves_modify_any evolves_fulfilTask).trans (evolves_modify_any evolves_clearCallbacks)
    have h2 := h1.trans (evolves_wakeAll (id := id) (now := now) (cfg := cfg) o.promise.callbacks
      ((tx.modify id fulfilTask).modify id clearCallbacks))
    split
    · exact h2
    · exact h2.trans (evolves_modify_any evolves_clearListeners)

theorem evolves_triggers {now cfg} :
    ∀ (l : List String) (tx : Tx), DocEvolves tx.doc (l.foldl (fun tx id => triggerSettlement tx id now cfg) tx).doc
  | [], _ => DocEvolves.refl _
  | _ :: l, _ => evolves_trigger.trans (evolves_triggers l _)

theorem evolves_sweepPhase {tx : Tx} {due : Obj → Bool} {f : Obj → Obj}
    (he : ∀ o, due o → Evolves o (f o)) : DocEvolves tx.doc (sweepPhase due f tx).doc := by
  unfold sweepPhase
  refine evolves_map (fun o _ => ?_) _
  split
  · rename_i hd; exact he o hd
  · exact Evolves.refl o

theorem evolves_sweepTx (d : Doc) (now cfg) : DocEvolves d (sweepTx d now cfg).doc := by
  unfold sweepTx
  have h1 : DocEvolves d { d with objects := d.objects.map fun o => if expiring now o then expire o else o } :=
    evolves_map (fun o _ => evolves_expire o) _
  refine (h1.trans (evolves_triggers _ { doc := _ })).trans ((evolves_sweepPhase ?_).trans (evolves_sweepPhase ?_))
  · intro o _
    exact evolves_taskMap (fun _ _ => Nat.le_refl _) (fun _ _ hf => hf)
  · intro o hd
    unfold leaseDue at hd
    split at hd
    · rename_i t ht
      simp only [Bool.and_eq_true, beq_iff_eq] at hd
      exact evolves_taskMap (fun _ _ => Nat.le_refl _) (fun t' h hf => by rw [ht] at h; cases h; rw [hd.1] at hf; cases hf)
    · simp at hd

/-! ## The operations -/

section Ops

variable {tx : Tx} {now : Int} {cfg : Cfg}

theorem evolves_settle (h : Core tx.doc) {id s v o} (hg : tx.doc.get id = some o) (hp : o.promise.state = .pending) :
    DocEvolves tx.doc (settle tx id s v now cfg).1.doc := by
  unfold settle
  exact (evolves_modify_get h.nodup hg (evolves_setSettled hp)).trans
    (evolves_trigger (tx := tx.modify id (setSettled s v now)) (id := id) (now := now) (cfg := cfg))

/-- A guarded task change: the guard says the task is not fulfilled. -/
theorem evolves_guarded {d : Doc} (h : Core d) {id o t} {g : Task → Task} (hg : d.get id = some o)
    (ht : o.task = some t) (hne : t.state ≠ .fulfilled) (hv : t.version ≤ (g t).version) :
    DocEvolves d (d.modify id fun o => { o with task := o.task.map g }) :=
  evolves_modify_get h.nodup hg (evolves_taskMap (fun t' e => by rw [ht] at e; cases e; exact hv)
    (fun t' e hf => by rw [ht] at e; cases e; exact absurd hf hne))

theorem evolves_promiseCreate (r : PromiseCreate) : DocEvolves tx.doc (promiseCreate tx r now cfg).1.doc := by
  unfold promiseCreate
  split
  · exact DocEvolves.refl _
  · split
    · exact DocEvolves.refl _
    · dsimp only
      split
      · exact evolves_insert _ _
      · split
        · exact evolves_insert _ _
        · split
          · split
            · exact evolves_insert _ _
            · simpa using evolves_insert _ _
          · simpa using evolves_insert _ _

theorem evolves_promiseSettle (h : Core tx.doc) (r : PromiseSettle) :
    DocEvolves tx.doc (promiseSettle tx r now cfg).1.doc := by
  unfold promiseSettle
  split
  · exact DocEvolves.refl _
  · split
    · exact DocEvolves.refl _
    · rename_i o hg
      split
      · exact DocEvolves.refl _
      · rename_i hp
        exact evolves_settle h hg (by simpa using hp)

theorem evolves_taskCreate (h : Core tx.doc) (pid ttl a) : DocEvolves tx.doc (taskCreate tx pid ttl a now cfg).1.doc := by
  unfold taskCreate
  split
  · exact DocEvolves.refl _
  · split
    · exact DocEvolves.refl _
    · split
      · exact DocEvolves.refl _
      · split
        · exact DocEvolves.refl _
        · split
          · exact DocEvolves.refl _
          · split
            · rename_i o hg
              split
              · rename_i t ht
                split
                · rename_i hp
                  unfold claim
                  exact evolves_guarded h hg ht (by simp at hp; simp [hp]) (Nat.le_succ _)
                · split <;> exact DocEvolves.refl _
              · exact DocEvolves.refl _
            · exact evolves_insert _ _

theorem evolves_taskAcquire (h : Core tx.doc) (id v pid ttl) :
    DocEvolves tx.doc (taskAcquire tx id v pid ttl now cfg).1.doc := by
  unfold taskAcquire
  split
  · exact DocEvolves.refl _
  · split
    · rename_i id' p' t hg
      split
      · exact DocEvolves.refl _
      · split
        · exact DocEvolves.refl _
        · rename_i hp _
          unfold claim
          exact evolves_guarded h hg rfl (by simp at hp; simp [hp]) (Nat.le_succ _)
    · exact DocEvolves.refl _

theorem evolves_taskRelease (h : Core tx.doc) (id v) : DocEvolves tx.doc (taskRelease tx id v now cfg).1.doc := by
  unfold taskRelease
  split
  · exact DocEvolves.refl _
  · split
    · exact DocEvolves.refl _
    · rename_i t hat
      obtain ⟨o, hg, ht, hs, _⟩ := acquiredAt_spec hat
      simp only [sendExecute_doc, Tx.modify_doc]
      exact evolves_guarded h hg ht (by simp [hs]) (Nat.le_refl _)

theorem evolves_taskFulfill (h : Core tx.doc) (id v a) : DocEvolves tx.doc (taskFulfill tx id v a now cfg).1.doc := by
  unfold taskFulfill
  split
  · exact DocEvolves.refl _
  · split
    · exact DocEvolves.refl _
    · split
      · exact DocEvolves.refl _
      · split
        · exact DocEvolves.refl _
        · split
          · exact DocEvolves.refl _
          · rename_i po hg
            split
            · simpa using evolves_modify_any evolves_fulfilTask
            · rename_i hp
              exact evolves_settle h hg (by simpa using hp)

theorem evolves_foldl_addCallback {id : String} :
    ∀ (l : List String) (tx : Tx), DocEvolves tx.doc (l.foldl (fun tx a => tx.modify a (addCallback id)) tx).doc
  | [], _ => DocEvolves.refl _
  | a :: l, tx => by
    simp only [List.foldl_cons]
    exact (evolves_modify_any (id := a) (evolves_addCallback id)).trans
      (evolves_foldl_addCallback l (tx.modify a (addCallback id)))

theorem evolves_taskSuspend (h : Core tx.doc) (id v awaited) :
    DocEvolves tx.doc (taskSuspend tx id v awaited cfg).1.doc := by
  unfold taskSuspend
  split
  · exact DocEvolves.refl _
  · split
    · exact DocEvolves.refl _
    · split
      · exact DocEvolves.refl _
      · split
        · exact DocEvolves.refl _
        · split
          · exact DocEvolves.refl _
          · split
            · exact DocEvolves.refl _
            · rename_i t hat
              obtain ⟨o, hg, ht, hs, _⟩ := acquiredAt_spec hat
              split
              · exact DocEvolves.refl _
              · split
                · exact DocEvolves.refl _
                · dsimp only
                  have e1 : DocEvolves tx.doc (tx.modify id clearResumes).doc :=
                    evolves_modify_any evolves_clearResumes
                  split
                  · exact e1
                  · have hne : ∀ a ∈ awaited, a ≠ id := by
                      intro a ha e; subst e; simp_all
                    have h1 : Core (tx.modify id clearResumes).doc :=
                      held_modify' h clearResumes_id hg weak_clearResumes agrees_clearResumes refs_clearResumes
                    have hid1 : id ∈ (tx.modify id clearResumes).doc.ids := by
                      rw [Tx.modify_doc, ids_modify _ _ clearResumes_id]
                      exact (get_mem hg).2 ▸ mem_ids (get_mem hg).1
                    obtain ⟨h2, _⟩ := core_foldl_addCallback awaited _ h1 hid1
                    have hg2 := (get_foldl_addCallback awaited (tx.modify id clearResumes) hne).trans
                      (get_modify_self h.nodup clearResumes_id hg)
                    have ht2 : (clearResumes o).task = some { t with resumes := [] } := by simp [clearResumes, ht]
                    refine (e1.trans (evolves_foldl_addCallback (id := id) awaited (tx.modify id clearResumes))).trans ?_
                    simp only [Tx.modify_doc]
                    unfold park
                    exact evolves_guarded h2 hg2 ht2 (by simp [hs]) (Nat.le_refl _)

theorem evolves_taskFence (h : Core tx.doc) (id v c a) : DocEvolves tx.doc (taskFence tx id v c a now cfg).1.doc := by
  unfold taskFence
  cases a with
  | create r =>
    dsimp only
    split
    · exact DocEvolves.refl _
    · split
      · exact DocEvolves.refl _
      · split
        · exact DocEvolves.refl _
        · have h' := evolves_promiseCreate (now := now) (cfg := cfg) (tx := tx) r
          revert h'
          generalize promiseCreate tx r now cfg = p
          rcases p with ⟨tx', n⟩
          intro h'
          dsimp only
          split <;> exact h'
  | settle r =>
    dsimp only
    split
    · exact DocEvolves.refl _
    · split
      · exact DocEvolves.refl _
      · split
        · exact DocEvolves.refl _
        · have h' := evolves_promiseSettle (now := now) (cfg := cfg) h r
          revert h'
          generalize promiseSettle tx r now cfg = p
          rcases p with ⟨tx', n⟩
          intro h'
          exact h'

theorem evolves_taskHeartbeat (pid tasks) : DocEvolves tx.doc (taskHeartbeat tx pid tasks now).1.doc := by
  unfold taskHeartbeat
  split
  · exact DocEvolves.refl _
  · dsimp only
    suffices ∀ (l : List (String × Nat)) (tx : Tx),
        DocEvolves tx.doc (l.foldl (fun tx (x : String × Nat) => tx.modify x.1 (beat pid x.2 now)) tx).doc from this _ _
    intro l
    induction l with
    | nil => exact fun _ => DocEvolves.refl _
    | cons x l ih =>
      intro tx
      simp only [List.foldl_cons]
      exact (evolves_modify_any (id := x.1) (evolves_beat pid x.2 now)).trans (ih (tx.modify x.1 (beat pid x.2 now)))

theorem evolves_taskHalt (h : Core tx.doc) (id) : DocEvolves tx.doc (taskHalt tx id).1.doc := by
  unfold taskHalt
  split
  · rename_i id' p' t hg
    split
    · exact DocEvolves.refl _
    · split
      · exact DocEvolves.refl _
      · rename_i hf _
        unfold halt
        exact evolves_guarded h hg rfl (by simpa using hf) (Nat.le_refl _)
  · exact DocEvolves.refl _

theorem evolves_taskContinue (h : Core tx.doc) (id) : DocEvolves tx.doc (taskContinue tx id now cfg).1.doc := by
  unfold taskContinue
  split
  · rename_i id' p' t hg
    split
    · exact DocEvolves.refl _
    · rename_i hs
      simp only [sendExecute_doc, Tx.modify_doc]
      have hs : t.state = .halted := by simpa using hs
      unfold resume
      exact evolves_guarded h hg rfl (by simp [hs]) (Nat.le_refl _)
  · exact DocEvolves.refl _

theorem evolves_promiseGet (id) : DocEvolves tx.doc (promiseGet tx id).1.doc := by
  unfold promiseGet; split <;> exact DocEvolves.refl _

theorem evolves_taskGet (id) : DocEvolves tx.doc (taskGet tx id).1.doc := by
  unfold taskGet; split <;> exact DocEvolves.refl _

theorem evolves_promiseRegisterCallback (a b) : DocEvolves tx.doc (promiseRegisterCallback tx a b).1.doc := by
  unfold promiseRegisterCallback
  repeat' (first | exact DocEvolves.refl _ | exact evolves_modify_any (evolves_addCallback _) | split)

theorem evolves_promiseRegisterListener (a b) : DocEvolves tx.doc (promiseRegisterListener tx a b).1.doc := by
  unfold promiseRegisterListener
  repeat' (first | exact DocEvolves.refl _ | exact evolves_modify_any (evolves_addListener _) | split)

end Ops

theorem evolves_decide {tx : Tx} {now cfg} (h : Core tx.doc) (req : Req) :
    DocEvolves tx.doc (decide_ tx req now cfg).1.doc := by
  cases req with
  | promiseGet id => exact evolves_promiseGet id
  | promiseCreate r => exact evolves_promiseCreate r
  | promiseSettle r => exact evolves_promiseSettle h r
  | promiseRegisterCallback a b => exact evolves_promiseRegisterCallback a b
  | promiseRegisterListener a b => exact evolves_promiseRegisterListener a b
  | taskGet id => exact evolves_taskGet id
  | taskCreate pid ttl a => exact evolves_taskCreate h pid ttl a
  | taskAcquire id v pid ttl => exact evolves_taskAcquire h id v pid ttl
  | taskRelease id v => exact evolves_taskRelease h id v
  | taskFulfill id v a => exact evolves_taskFulfill h id v a
  | taskSuspend id v aw => exact evolves_taskSuspend h id v aw
  | taskFence id v c a => exact evolves_taskFence h id v c a
  | taskHeartbeat pid ts => exact evolves_taskHeartbeat pid ts
  | taskHalt id => exact evolves_taskHalt h id
  | taskContinue id => exact evolves_taskContinue h id

/-! ## Over steps and runs -/

theorem handleInternal_evolves {d : Doc} {now cfg} :
    ∃ d', committed (handleInternal d now cfg) = some d' ∧ DocEvolves d d' :=
  ⟨_, committed_linearize _ _ _, evolves_sweepTx d now cfg⟩

theorem handleExternal_evolves {d : Doc} {req now cfg} (h : Core d) :
    ∃ d', committed (handleExternal d req now cfg).1 = some d' ∧ DocEvolves d d' := by
  unfold handleExternal
  dsimp only
  rw [List.append_assoc]
  have h1 := core_timerAt (core_sweepTx (now := now) (cfg := cfg) h) (minDeadline (sweepTx d now cfg).doc)
  exact ⟨_, committed_linearize _ _ _,
    (evolves_sweepTx d now cfg).trans (evolves_decide (now := now) (cfg := cfg) (tx := { doc := _ }) h1 req)⟩

theorem step_evolves {cfg} {d d' : Doc} (h : Inv d) (s : Step cfg d d') : DocEvolves d d' := by
  cases s with
  | internal now e =>
    obtain ⟨d'', e', h''⟩ := handleInternal_evolves (d := d) (now := now) (cfg := cfg)
    rw [e] at e'; cases e'; exact h''
  | external req now e =>
    obtain ⟨d'', e', h''⟩ := handleExternal_evolves (req := req) (now := now) (cfg := cfg) h.1
    rw [e] at e'; cases e'; exact h''

/-- Any number of steps. -/
inductive Steps (cfg : Cfg) : Doc → Doc → Prop where
  | refl (d) : Steps cfg d d
  | tail {a b c} : Steps cfg a b → Step cfg b c → Steps cfg a c

theorem Steps.reachable {cfg} {a b : Doc} (hr : Reachable cfg a) (hs : Steps cfg a b) : Reachable cfg b := by
  induction hs with
  | refl => exact hr
  | tail _ s ih => exact Reachable.step ih s

/-- **From any reachable document, along any run: a settled promise keeps its
settlement, a task's version never goes down, a fulfilled task stays
fulfilled, and nothing is ever deleted.** -/
theorem reachable_evolves {cfg} {d d' : Doc} (hr : Reachable cfg d) (hs : Steps cfg d d') : DocEvolves d d' := by
  induction hs with
  | refl => exact DocEvolves.refl _
  | tail hab hbc ih => exact ih.trans (step_evolves (reachable_inv (hab.reachable hr)) hbc)

end Kernel
