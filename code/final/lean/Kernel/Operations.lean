import Kernel.Sweep

/-!
# Every operation preserves the invariants

One theorem per protocol operation: from a `Core` document, the operation
leaves a `Core` document. Each is the operation's own case analysis, with
the object lemmas doing the work: a refused request changes nothing, and an
accepted one changes objects only through writes that keep them whole.
-/

namespace Kernel

open Doc

/-! ## Writes that need no guard -/

theorem held_modify_any {E : String → Prop} {d : Doc} {id f}
    (h : Held E d) (hf : IdPres f)
    (hw : ∀ o, Weak o → Weak (f o)) (ha : ∀ o, Agrees o → Agrees (f o))
    (hr : ∀ o, Refs d.ids o → Refs d.ids (f o)) : Held E (d.modify id f) := by
  cases hg : d.get id with
  | none => rw [modify_of_get_none hg]; exact h
  | some o => exact held_modify' h hf hg (hw o) (ha o) (hr o)

theorem core_modify_local {d : Doc} {id f} (h : Core d) (hf : IdPres f)
    (hl : ∀ o, Local o → Local (f o)) (hr : ∀ o, Refs d.ids o → Refs d.ids (f o)) :
    Core (d.modify id f) := by
  cases hg : d.get id with
  | none => rw [modify_of_get_none hg]; exact h
  | some o =>
    obtain ⟨ho, _⟩ := get_mem hg
    have := hl o (h.local o ho)
    exact held_modify h hf hg this.1 (fun _ => this.2) (hr o (h.refs o ho)) (fun _ _ he => he)

section Writes

attribute [local simp] TaskShape Task.disarm Task.armRetry Task.armLease

theorem addCallback_id (a) : IdPres (addCallback a) := by
  intro o; unfold addCallback; split <;> rfl

theorem weak_addCallback {a} {o : Obj} (h : Weak o) : Weak (addCallback a o) := by
  unfold addCallback; split
  · exact h
  · exact ⟨h.settledAt, h.listeners, h.shape, h.target⟩

theorem agrees_addCallback {a} {o : Obj} (h : Agrees o) : Agrees (addCallback a o) := by
  unfold addCallback; split
  · exact h
  · exact h

theorem refs_addCallback {ids a} {o : Obj} (h : Refs ids o) (ha : a ∈ ids) : Refs ids (addCallback a o) := by
  unfold addCallback; split
  · exact h
  · refine ⟨fun x hx => ?_, h.2⟩
    simp only [List.mem_append, List.mem_singleton] at hx
    rcases hx with hx | rfl
    · exact h.1 x hx
    · exact ha

theorem addCallback_state (a) (o : Obj) : (addCallback a o).promise.state = o.promise.state := by
  unfold addCallback; split <;> rfl

theorem addCallback_task (a) (o : Obj) : (addCallback a o).task = o.task := by
  unfold addCallback; split <;> rfl

theorem addListener_id (a) : IdPres (addListener a) := by
  intro o; unfold addListener; split <;> rfl

theorem weak_addListener {a} {o : Obj} (h : Weak o) : Weak (addListener a o) := by
  unfold addListener; split
  · exact h
  · rename_i hc
    refine ⟨h.settledAt, ?_, h.shape, h.target⟩
    simp only [List.contains_iff_mem, Bool.not_eq_true] at hc
    simp only
    rw [List.nodup_append]
    refine ⟨h.listeners, List.nodup_cons.2 ⟨List.not_mem_nil, List.nodup_nil⟩, ?_⟩
    intro x hx y hy
    simp only [List.mem_singleton] at hy
    subst hy
    intro e; subst e; simp_all

theorem agrees_addListener {a} {o : Obj} (h : Agrees o) : Agrees (addListener a o) := by
  unfold addListener; split
  · exact h
  · exact h

theorem refs_addListener {ids a} {o : Obj} (h : Refs ids o) : Refs ids (addListener a o) := by
  unfold addListener; split
  · exact h
  · exact h

theorem clearResumes_id : IdPres clearResumes := fun _ => rfl

theorem weak_clearResumes {o : Obj} (h : Weak o) : Weak (clearResumes o) := by
  refine ⟨h.settledAt, h.listeners, ?_, ?_⟩
  · intro t e
    simp only [clearResumes, Option.map_eq_some_iff] at e
    obtain ⟨t0, h0, rfl⟩ := e
    simpa using h.shape t0 h0
  · simpa [clearResumes] using h.target

theorem agrees_clearResumes {o : Obj} (h : Agrees o) : Agrees (clearResumes o) := by
  intro t e
  simp only [clearResumes, Option.map_eq_some_iff] at e
  obtain ⟨t0, h0, rfl⟩ := e
  exact h t0 h0

theorem refs_clearResumes {ids} {o : Obj} (h : Refs ids o) : Refs ids (clearResumes o) := by
  refine ⟨h.1, fun t e r hr => ?_⟩
  simp only [clearResumes, Option.map_eq_some_iff] at e
  obtain ⟨t0, h0, rfl⟩ := e
  simp at hr

theorem beat_id (pid v now) : IdPres (beat pid v now) := by
  intro o; unfold beat; split <;> (try split) <;> (try split) <;> rfl

theorem local_beat {pid v now} {o : Obj} (h : Local o) : Local (beat pid v now o) := by
  obtain ⟨⟨h1, h2, h3, h4⟩, ha⟩ := h
  unfold beat
  split
  · rename_i t ht
    split
    · rename_i hc
      simp only [Bool.and_eq_true, beq_iff_eq] at hc
      split
      · refine ⟨⟨h1, h2, ?_, by simpa [ht] using h4⟩, ?_⟩
        · intro t' e; cases e; simp [hc.1.1]
        · intro t' e; cases e; simpa using ha t ht
      · exact ⟨⟨h1, h2, h3, h4⟩, ha⟩
    · exact ⟨⟨h1, h2, h3, h4⟩, ha⟩
  · exact ⟨⟨h1, h2, h3, h4⟩, ha⟩

theorem refs_beat {ids pid v now} {o : Obj} (h : Refs ids o) : Refs ids (beat pid v now o) := by
  obtain ⟨h1, h2⟩ := h
  unfold beat
  split
  · rename_i t ht
    split
    · split
      · exact ⟨h1, fun t' e r hr => by cases e; exact h2 t ht r hr⟩
      · exact ⟨h1, h2⟩
    · exact ⟨h1, h2⟩
  · exact ⟨h1, h2⟩

/-- The task-state changes that a guard makes safe: each takes a task in one
state and puts it in another that the same promise state allows. -/
theorem local_taskMap {o : Obj} {g : Task → Task} {t : Task} (ht : o.task = some t)
    (h : Local o) (hs : TaskShape (g t)) (hf : t.state ≠ .fulfilled → (g t).state ≠ .fulfilled)
    (hne : t.state ≠ .fulfilled) :
    Local { o with task := o.task.map g } := by
  obtain ⟨⟨h1, h2, h3, h4⟩, ha⟩ := h
  have hp := (ha t ht).2 hne
  refine ⟨⟨h1, h2, ?_, by simpa [ht] using h4⟩, ?_⟩
  · intro t' e; simp [ht] at e; subst e; exact hs
  · intro t' e; simp [ht] at e; subst e; simp [hp, hf hne]

theorem refs_taskMap {ids} {o : Obj} {g : Task → Task} (h : Refs ids o)
    (hg : ∀ t, ∀ r ∈ (g t).resumes, r ∈ t.resumes) : Refs ids { o with task := o.task.map g } := by
  refine ⟨h.1, fun t e r hr => ?_⟩
  simp only [Option.map_eq_some_iff] at e
  obtain ⟨t0, h0, rfl⟩ := e
  exact h.2 t0 h0 r (hg t0 r hr)

end Writes

/-- A guarded write to the object `get` found: the guard is about that object. -/
theorem core_modify_task {d : Doc} {id o t} {g : Task → Task}
    (h : Core d) (hg : d.get id = some o) (ht : o.task = some t)
    (hs : TaskShape (g t)) (hne : t.state ≠ .fulfilled) (hf : (g t).state ≠ .fulfilled)
    (hres : ∀ t, ∀ r ∈ (g t).resumes, r ∈ t.resumes) :
    Core (d.modify id fun o => { o with task := o.task.map g }) := by
  obtain ⟨ho, _⟩ := get_mem hg
  have hl := local_taskMap ht (h.local o ho) hs (fun _ => hf) hne
  exact held_modify h (fun _ => rfl) hg hl.1 (fun _ => hl.2) (refs_taskMap (h.refs o ho) hres)
    (fun _ _ he => he)

/-! ## Settling -/

theorem settled_modify_self {d : Doc} (hn : d.ids.Nodup) {id f o} (hf : IdPres f)
    (hg : d.get id = some o) (hs : (f o).promise.state ≠ .pending) : Settled (d.modify id f) id := by
  intro o' ho' hx
  obtain ⟨o2, ho2, rfl⟩ := mem_modify ho'
  have hx' : o2.id = id := by
    by_cases hy : o2.id = id
    · exact hy
    · simpa [hy] using hx
  rw [eq_of_get hn hg ho2 hx']
  simpa [(get_mem hg).2] using hs

theorem ne_pending_of_settleState {s : PState} (h : isSettleState s = true) : s ≠ .pending := by
  intro e; subst e; simp [isSettleState] at h

theorem core_settle {tx : Tx} {id s v now cfg o}
    (h : Core tx.doc) (hg : tx.doc.get id = some o) (hs : s ≠ .pending) :
    Core (settle tx id s v now cfg).1.doc := by
  obtain ⟨ho, _⟩ := get_mem hg
  have h1 : Held (fun x => x = id) (tx.doc.modify id (setSettled s v now)) :=
    held_modify h (setSettled_id s v now) hg (weak_setSettled (h.weak o ho) hs)
      (fun hn => absurd rfl hn) (refs_setSettled (h.refs o ho)) (fun _ _ he => he.elim)
  have hs1 : Settled (tx.doc.modify id (setSettled s v now)) id :=
    settled_modify_self h.nodup (setSettled_id s v now) hg (by simpa [setSettled] using hs)
  obtain ⟨h2, _, _⟩ := held_trigger (tx := tx.modify id (setSettled s v now)) (now := now) (cfg := cfg) h1 hs1
  exact h2.mono fun x ⟨e, ne⟩ => absurd e ne

/-! ## New objects -/

section New

theorem newPromise_settledAt (r : PromiseCreate) (now : Int) :
    (newPromise r now).state = .pending ↔ (newPromise r now).settledAt = none := by
  unfold newPromise; split <;> simp [timeoutState_ne]

theorem newPromise_listeners (r : PromiseCreate) (now : Int) : (newPromise r now).listeners = [] := by
  unfold newPromise; split <;> rfl

theorem newPromise_callbacks (r : PromiseCreate) (now : Int) : (newPromise r now).callbacks = [] := by
  unfold newPromise; split <;> rfl

theorem newPromise_target (r : PromiseCreate) (now : Int) :
    (newPromise r now).target = r.tags.lookup TAG_TARGET := by
  unfold newPromise; split <;> rfl

/-- A new object is whole when its task, if any, agrees with its promise. -/
theorem held_insert_new {d : Doc} {id p} {t : Option Task} (h : Core d) (hg : d.get id = none)
    (hsa : p.state = .pending ↔ p.settledAt = none) (hl : p.listeners = []) (hc : p.callbacks = [])
    (hs : ∀ t', t = some t' → TaskShape t') (htg : t.isSome = p.target.isSome)
    (ha : ∀ t', t = some t' → (p.state = .pending ↔ t'.state ≠ .fulfilled))
    (hr : ∀ t', t = some t' → t'.resumes = []) :
    Core (d.insert ⟨id, p, t⟩) :=
  held_insert h hg ⟨hsa, by simp [hl], hs, htg⟩ ha
    ⟨fun a ha => by simp [hc] at ha, fun t' e r hr' => by simp [hr t' e] at hr'⟩

end New

/-! ## The operations -/

section Ops

variable {tx : Tx} {now : Int} {cfg : Cfg}

attribute [local simp] TaskShape Task.disarm Task.armRetry Task.armLease

theorem core_promiseGet (h : Core tx.doc) (id) : Core (promiseGet tx id).1.doc := by
  unfold promiseGet; split <;> exact h

theorem core_promiseCreate (h : Core tx.doc) (r : PromiseCreate) : Core (promiseCreate tx r now cfg).1.doc := by
  unfold promiseCreate
  split
  · exact h
  · split
    · exact h
    · rename_i hg
      have hsa := newPromise_settledAt r now
      have hl := newPromise_listeners r now
      have hc := newPromise_callbacks r now
      dsimp only
      split
      · rename_i htg
        exact held_insert_new h hg hsa hl hc (by simp) (by simp [htg]) (by simp) (by simp)
      · rename_i a htg
        split
        · rename_i hns
          exact held_insert_new h hg hsa hl hc (by simp) (by simp [htg])
            (fun t e => by cases e; simp [hns]) (fun t e => by cases e; rfl)
        · rename_i hps
          have hps : (newPromise r now).state = .pending := by simpa using hps
          have hins : ∀ at_ : Int, Core (tx.doc.insert ⟨r.id, newPromise r now, some (({} : Task).armRetry at_)⟩) :=
            fun at_ => held_insert_new h hg hsa hl hc (fun t e => by cases e; simp) (by simp [htg])
              (fun t e => by cases e; simp [hps]) (fun t e => by cases e; rfl)
          split
          · split
            · exact hins _
            · simpa using hins _
          · simpa using hins _

theorem core_promiseSettle (h : Core tx.doc) (r : PromiseSettle) : Core (promiseSettle tx r now cfg).1.doc := by
  unfold promiseSettle
  split
  · exact h
  · rename_i hs
    split
    · exact h
    · rename_i o hg
      split
      · exact h
      · exact core_settle h hg (ne_pending_of_settleState (by simpa using hs))

theorem core_promiseRegisterCallback (h : Core tx.doc) (awaited awaiter) :
    Core (promiseRegisterCallback tx awaited awaiter).1.doc := by
  unfold promiseRegisterCallback
  split
  · exact h
  · split
    · exact h
    · split
      · exact h
      · rename_i ad hgd
        split
        · exact h
        · rename_i ar hgr
          have har : awaiter ∈ tx.doc.ids := (get_mem hgr).2 ▸ mem_ids (get_mem hgr).1
          split
          · exact h
          · split
            · exact h
            · dsimp only
              split
              · exact held_modify' h (addCallback_id _) hgd weak_addCallback agrees_addCallback
                  (fun hr => refs_addCallback hr har)
              · exact h

theorem core_promiseRegisterListener (h : Core tx.doc) (awaited address) :
    Core (promiseRegisterListener tx awaited address).1.doc := by
  unfold promiseRegisterListener
  split
  · exact h
  · split
    · exact h
    · rename_i o hg
      split
      · exact h
      · dsimp only
        split
        · exact held_modify' h (addListener_id _) hg weak_addListener agrees_addListener refs_addListener
        · exact h

theorem core_taskGet (h : Core tx.doc) (id) : Core (taskGet tx id).1.doc := by
  unfold taskGet; split <;> exact h

theorem core_claim {d : Doc} {id o t pid ttl now} (h : Core d) (hg : d.get id = some o)
    (ht : o.task = some t) (hp : t.state = .pending) : Core (d.modify id (claim pid ttl now)) := by
  unfold claim
  exact core_modify_task h hg ht (by simp) (by simp [hp]) (by simp) (fun _ _ hr => by simp at hr)

theorem core_taskCreate (h : Core tx.doc) (pid ttl a) : Core (taskCreate tx pid ttl a now cfg).1.doc := by
  unfold taskCreate
  split
  · exact h
  · rename_i address hta
    split
    · exact h
    · split
      · exact h
      · split
        · exact h
        · split
          · exact h
          · split
            · rename_i o hg
              split
              · rename_i t ht
                split
                · rename_i hp
                  exact core_claim h hg ht (by simpa using hp)
                · split
                  · exact h
                  · exact h
              · exact h
            · rename_i hg
              have hsa := newPromise_settledAt a now
              have hl := newPromise_listeners a now
              have hc := newPromise_callbacks a now
              have htg : (newPromise a now).target.isSome := by simp [newPromise_target, hta]
              dsimp only
              split
              · rename_i hps
                have hps : (newPromise a now).state = .pending := by simpa using hps
                exact held_insert_new h hg hsa hl hc (fun t e => by cases e; simp) (by simp [htg])
                  (fun t e => by cases e; simp [hps]) (fun t e => by cases e; rfl)
              · rename_i hps
                have hps : (newPromise a now).state ≠ .pending := by simpa using hps
                exact held_insert_new h hg hsa hl hc (fun t e => by cases e; simp) (by simp [htg])
                  (fun t e => by cases e; simp [hps]) (fun t e => by cases e; rfl)

theorem acquiredAt_spec {tx : Tx} {id v t} (h : acquiredAt tx id v = some t) :
    ∃ o, tx.doc.get id = some o ∧ o.task = some t ∧ t.state = .acquired ∧ t.version = v := by
  unfold acquiredAt at h
  split at h
  · rename_i o' id' p' t' hg
    split at h
    · rename_i hc
      cases h
      simp only [Bool.and_eq_true, beq_iff_eq] at hc
      exact ⟨_, hg, rfl, hc.1, hc.2⟩
    · cases h
  · cases h

theorem core_taskAcquire (h : Core tx.doc) (id v pid ttl) : Core (taskAcquire tx id v pid ttl now cfg).1.doc := by
  unfold taskAcquire
  split
  · exact h
  · split
    · rename_i id' p' t hg
      split
      · exact h
      · split
        · exact h
        · rename_i hp _
          exact core_claim h hg rfl (by simpa using hp)
    · exact h

theorem core_taskRelease (h : Core tx.doc) (id v) : Core (taskRelease tx id v now cfg).1.doc := by
  unfold taskRelease
  split
  · exact h
  · split
    · exact h
    · rename_i t hat
      obtain ⟨o, hg, ht, hs, _⟩ := acquiredAt_spec hat
      simp only [sendExecute_doc, Tx.modify_doc]
      exact core_modify_task h hg ht (by simp) (by simp [hs]) (by simp) (fun _ _ hr => hr)

theorem core_taskFulfill (h : Core tx.doc) (id v a) : Core (taskFulfill tx id v a now cfg).1.doc := by
  unfold taskFulfill
  split
  · exact h
  · rename_i hid
    split
    · exact h
    · rename_i hs
      split
      · exact h
      · split
        · exact h
        · split
          · exact h
          · rename_i po hg
            have hid : a.id = id := by simpa using hid
            split
            · rename_i hps
              simp only [Tx.modify_doc]
              rw [hid] at hg
              obtain ⟨ho, _⟩ := get_mem hg
              exact held_modify h fulfilTask_id hg (weak_fulfilTask (h.weak po ho))
                (fun _ => agrees_fulfilTask hps) (refs_fulfilTask (h.refs po ho)) (fun _ _ he => he)
            · exact core_settle h hg (ne_pending_of_settleState (by simpa using hs))

theorem get_foldl_addCallback {id : String} :
    ∀ (l : List String) (tx : Tx), (∀ a ∈ l, a ≠ id) →
      (l.foldl (fun tx a => tx.modify a (addCallback id)) tx).doc.get id = tx.doc.get id
  | [], _, _ => rfl
  | a :: l, tx, hl => by
    simp only [List.foldl_cons]
    rw [get_foldl_addCallback l _ (fun x hx => hl x (by simp [hx]))]
    simp only [Tx.modify_doc]
    exact get_modify_ne (addCallback_id id) (Ne.symm (hl a (by simp)))

theorem core_foldl_addCallback {id : String} :
    ∀ (l : List String) (tx : Tx), Core tx.doc → id ∈ tx.doc.ids →
      Core (l.foldl (fun tx a => tx.modify a (addCallback id)) tx).doc ∧
        (l.foldl (fun tx a => tx.modify a (addCallback id)) tx).doc.ids = tx.doc.ids
  | [], _, h, _ => ⟨h, rfl⟩
  | a :: l, tx, h, hid => by
    simp only [List.foldl_cons]
    have h1 : Core (tx.modify a (addCallback id)).doc :=
      held_modify_any h (addCallback_id id) (fun _ => weak_addCallback) (fun _ => agrees_addCallback)
        (fun _ hr => refs_addCallback hr hid)
    have hids1 : (tx.modify a (addCallback id)).doc.ids = tx.doc.ids := ids_modify _ _ (addCallback_id id)
    obtain ⟨h2, hids2⟩ := core_foldl_addCallback l _ h1 (hids1 ▸ hid)
    exact ⟨h2, hids2.trans hids1⟩

theorem core_taskSuspend (h : Core tx.doc) (id v awaited) : Core (taskSuspend tx id v awaited cfg).1.doc := by
  unfold taskSuspend
  split
  · exact h
  · rename_i hself
    split
    · exact h
    · split
      · exact h
      · split
        · exact h
        · split
          · exact h
          · split
            · exact h
            · rename_i t hat
              obtain ⟨o, hg, ht, hs, _⟩ := acquiredAt_spec hat
              obtain ⟨ho, hoid⟩ := get_mem hg
              split
              · exact h
              · split
                · exact h
                · dsimp only
                  have h1 : Core (tx.modify id clearResumes).doc :=
                    held_modify' h clearResumes_id hg weak_clearResumes agrees_clearResumes refs_clearResumes
                  have hg1 : (tx.modify id clearResumes).doc.get id = some (clearResumes o) :=
                    get_modify_self h.nodup clearResumes_id hg
                  have hid1 : id ∈ (tx.modify id clearResumes).doc.ids := by
                    rw [Tx.modify_doc, ids_modify _ _ clearResumes_id]; exact hoid ▸ mem_ids ho
                  split
                  · exact h1
                  · have hne : ∀ a ∈ awaited, a ≠ id := by
                      intro a ha e; subst e; simp_all
                    obtain ⟨h2, _⟩ := core_foldl_addCallback awaited _ h1 hid1
                    have hg2 := (get_foldl_addCallback awaited (tx.modify id clearResumes) hne).trans hg1
                    simp only [Tx.modify_doc]
                    have ht2 : (clearResumes o).task = some { t with resumes := [] } := by
                      simp [clearResumes, ht]
                    exact core_modify_task h2 hg2 ht2 (by simp) (by simp [hs]) (by simp) (fun _ _ hr => hr)

theorem core_taskFence (h : Core tx.doc) (id v c a) : Core (taskFence tx id v c a now cfg).1.doc := by
  unfold taskFence
  cases a with
  | create r =>
    dsimp only
    split
    · exact h
    · split
      · exact h
      · split
        · exact h
        · have h' := core_promiseCreate (now := now) (cfg := cfg) h r
          revert h'
          generalize promiseCreate tx r now cfg = p
          rcases p with ⟨tx', n⟩
          intro h'
          dsimp only
          split <;> exact h'
  | settle r =>
    dsimp only
    split
    · exact h
    · split
      · exact h
      · split
        · exact h
        · have h' := core_promiseSettle (now := now) (cfg := cfg) h r
          revert h'
          generalize promiseSettle tx r now cfg = p
          rcases p with ⟨tx', n⟩
          intro h'
          exact h'

theorem core_taskHeartbeat (h : Core tx.doc) (pid tasks) : Core (taskHeartbeat tx pid tasks now).1.doc := by
  unfold taskHeartbeat
  split
  · exact h
  · dsimp only
    suffices ∀ (l : List (String × Nat)) (tx : Tx), Core tx.doc →
        Core (l.foldl (fun tx (x : String × Nat) => tx.modify x.1 (beat pid x.2 now)) tx).doc from this _ _ h
    intro l
    induction l with
    | nil => exact fun _ h => h
    | cons x l ih =>
      intro tx h
      exact ih _ (core_modify_local h (beat_id _ _ _) (fun _ => local_beat) (fun _ => refs_beat))

theorem core_taskHalt (h : Core tx.doc) (id) : Core (taskHalt tx id).1.doc := by
  unfold taskHalt
  split
  · rename_i id' p' t hg
    split
    · exact h
    · split
      · exact h
      · rename_i hf _
        exact core_modify_task h hg rfl (by simp) (by simpa using hf) (by simp) (fun _ _ hr => hr)
  · exact h

theorem core_taskContinue (h : Core tx.doc) (id) : Core (taskContinue tx id now cfg).1.doc := by
  unfold taskContinue
  split
  · rename_i id' p' t hg
    split
    · exact h
    · rename_i hs
      simp only [sendExecute_doc, Tx.modify_doc]
      have hs : t.state = .halted := by simpa using hs
      exact core_modify_task h hg rfl (by simp) (by simp [hs]) (by simp) (fun _ _ hr => hr)
  · exact h

end Ops

theorem core_decide {tx : Tx} {now cfg} (h : Core tx.doc) (req : Req) : Core (decide_ tx req now cfg).1.doc := by
  cases req with
  | promiseGet id => exact core_promiseGet h id
  | promiseCreate r => exact core_promiseCreate h r
  | promiseSettle r => exact core_promiseSettle h r
  | promiseRegisterCallback a b => exact core_promiseRegisterCallback h a b
  | promiseRegisterListener a b => exact core_promiseRegisterListener h a b
  | taskGet id => exact core_taskGet h id
  | taskCreate pid ttl a => exact core_taskCreate h pid ttl a
  | taskAcquire id v pid ttl => exact core_taskAcquire h id v pid ttl
  | taskRelease id v => exact core_taskRelease h id v
  | taskFulfill id v a => exact core_taskFulfill h id v a
  | taskSuspend id v aw => exact core_taskSuspend h id v aw
  | taskFence id v c a => exact core_taskFence h id v c a
  | taskHeartbeat pid ts => exact core_taskHeartbeat h pid ts
  | taskHalt id => exact core_taskHalt h id
  | taskContinue id => exact core_taskContinue h id

end Kernel
