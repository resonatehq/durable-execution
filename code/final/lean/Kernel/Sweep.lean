import Kernel.Invariants

/-!
# The sweep preserves the invariants

`handle_internal` in four phases. Phase 1 settles every expiring promise and
leaves its task alone, which is the one moment a document breaks `Agrees`;
`Held` names the objects it breaks it for. Phase 2 runs one settlement chain
per expired id, and each chain takes its own id out of the broken set, so
when phase 2 ends the set is empty and the document is `Core` again. Phases
3 and 4 each change a task within its own object and nowhere else.
-/

namespace Kernel

open Doc

/-- The promise named `x`, if it exists, has settled. -/
def Settled (d : Doc) (x : String) : Prop := ∀ o ∈ d.objects, o.id = x → o.promise.state ≠ .pending

@[simp] theorem Tx.modify_doc (tx : Tx) (id f) : (tx.modify id f).doc = tx.doc.modify id f := rfl

@[simp] theorem sendExecute_doc (tx : Tx) (id v) : (sendExecute tx id v).doc = tx.doc := by
  unfold sendExecute; split <;> (try split) <;> rfl

/-! ## Changing one object -/

theorem held_modify {E E' : String → Prop} {d : Doc} {id f o}
    (h : Held E d) (hf : IdPres f) (hg : d.get id = some o)
    (hw : Weak (f o)) (ha : ¬ E' id → Agrees (f o)) (hr : Refs d.ids (f o))
    (hE : ∀ x, x ≠ id → E x → E' x) : Held E' (d.modify id f) := by
  have hids := ids_modify d id hf
  obtain ⟨ho, hoid⟩ := get_mem hg
  refine ⟨hids ▸ h.nodup, sorted_map (fun o => by
      show (if o.id == id then f o else o).id = o.id
      split
      · exact hf o
      · rfl) h.sorted, ?_, ?_, ?_⟩
  · intro o' ho'
    obtain ⟨o2, ho2, rfl⟩ := mem_modify ho'
    by_cases hx : o2.id = id
    · rw [eq_of_get h.nodup hg ho2 hx]; simpa [hoid] using hw
    · simpa [hx] using h.weak o2 ho2
  · intro o' ho' hn
    obtain ⟨o2, ho2, rfl⟩ := mem_modify ho'
    by_cases hx : o2.id = id
    · rw [eq_of_get h.nodup hg ho2 hx] at hn ⊢; simp only [hoid, beq_self_eq_true, if_true] at hn ⊢
      rw [hf, hoid] at hn; exact ha hn
    · simp only [hx, beq_iff_eq, if_false] at hn ⊢
      exact h.agrees o2 ho2 (fun he => hn (hE _ hx he))
  · intro o' ho'
    rw [hids]
    obtain ⟨o2, ho2, rfl⟩ := mem_modify ho'
    by_cases hx : o2.id = id
    · rw [eq_of_get h.nodup hg ho2 hx]; simpa [hoid] using hr
    · simpa [hx] using h.refs o2 ho2

/-- The usual case: a change that keeps the object's own invariants. -/
theorem held_modify' {E : String → Prop} {d : Doc} {id f o}
    (h : Held E d) (hf : IdPres f) (hg : d.get id = some o)
    (hw : Weak o → Weak (f o)) (ha : Agrees o → Agrees (f o)) (hr : Refs d.ids o → Refs d.ids (f o)) :
    Held E (d.modify id f) := by
  obtain ⟨ho, hoid⟩ := get_mem hg
  exact held_modify h hf hg (hw (h.weak o ho)) (fun hn => ha (h.agrees o ho (hoid ▸ hn)))
    (hr (h.refs o ho)) (fun _ _ he => he)

theorem held_insert {E : String → Prop} {d : Doc} {o}
    (h : Held E d) (hg : d.get o.id = none) (hw : Weak o) (ha : Agrees o) (hr : Refs d.ids o) :
    Held E (d.insert o) := by
  have hsub : ∀ x ∈ d.ids, x ∈ (d.insert o).ids := fun x hx => mem_ids_insert.2 (Or.inr hx)
  refine ⟨?_, sorted_insertSorted o h.sorted, ?_, ?_, ?_⟩
  · refine (ids_insert_perm d o).nodup_iff.2 (List.nodup_cons.2 ⟨?_, h.nodup⟩)
    intro hm
    obtain ⟨o', ho', he⟩ := List.mem_map.1 hm
    exact get_none hg o' ho' he
  · intro o' ho'
    rcases mem_insert.1 ho' with rfl | ho'
    · exact hw
    · exact h.weak o' ho'
  · intro o' ho' hn
    rcases mem_insert.1 ho' with rfl | ho'
    · exact ha
    · exact h.agrees o' ho' hn
  · intro o' ho'
    rcases mem_insert.1 ho' with rfl | ho'
    · exact hr.mono hsub
    · exact (h.refs o' ho').mono hsub

theorem settled_modify {d : Doc} {id f x} (hf : IdPres f)
    (hp : ∀ o, (f o).promise.state = o.promise.state) (h : Settled d x) : Settled (d.modify id f) x := by
  intro o' ho' hx
  obtain ⟨o2, ho2, rfl⟩ := mem_modify ho'
  by_cases hy : o2.id = id
  · simp only [hy, beq_self_eq_true, if_true] at hx ⊢
    rw [hp]; rw [hf] at hx; exact h o2 ho2 hx
  · simp only [hy, beq_iff_eq, if_false] at hx ⊢
    exact h o2 ho2 hx

/-! ## The settlement chain -/

theorem refs_fulfilTask {ids} {o : Obj} (h : Refs ids o) : Refs ids (fulfilTask o) := by
  obtain ⟨h1, h2⟩ := h
  unfold fulfilTask
  split
  · split
    · exact ⟨h1, fun t e r hr => by cases e; simp at hr⟩
    · exact ⟨h1, h2⟩
  · exact ⟨h1, h2⟩

theorem wake_state (id now cfg) (o : Obj) : (wake id now cfg o).promise.state = o.promise.state := by
  rw [wake_promise]

/-- One awaiter observes a settlement: nothing breaks, nothing unsettles. -/
theorem held_wakeOne {E : String → Prop} {tx : Tx} {id now cfg a}
    (h : Held E tx.doc) (hid : id ∈ tx.doc.ids) :
    Held E (wakeOne id now cfg tx a).doc ∧ (wakeOne id now cfg tx a).doc.ids = tx.doc.ids ∧
      ∀ x, Settled tx.doc x → Settled (wakeOne id now cfg tx a).doc x := by
  unfold wakeOne
  split
  · exact ⟨h, rfl, fun _ hx => hx⟩
  · rename_i ao hg
    split
    · exact ⟨h, rfl, fun _ hx => hx⟩
    · have key : Held E (tx.doc.modify a (wake id now cfg)) :=
        held_modify' h (wake_id id now cfg) hg weak_wake agrees_wake (fun hr => refs_wake hr hid)
      have hids := ids_modify tx.doc a (wake_id id now cfg)
      have hs : ∀ x, Settled tx.doc x → Settled (tx.doc.modify a (wake id now cfg)) x :=
        fun x hx => settled_modify (wake_id id now cfg) (wake_state id now cfg) hx
      split
      · exact ⟨h, rfl, fun _ hx => hx⟩
      · split
        · simpa using ⟨key, hids, hs⟩
        · simpa using ⟨key, hids, hs⟩

theorem held_wakeAll {E : String → Prop} {id now cfg} :
    ∀ (l : List String) (tx : Tx), Held E tx.doc → id ∈ tx.doc.ids →
      Held E (l.foldl (wakeOne id now cfg) tx).doc ∧ (l.foldl (wakeOne id now cfg) tx).doc.ids = tx.doc.ids ∧
        ∀ x, Settled tx.doc x → Settled (l.foldl (wakeOne id now cfg) tx).doc x
  | [], _, h, _ => ⟨h, rfl, fun _ hx => hx⟩
  | a :: l, tx, h, hid => by
    obtain ⟨h1, h2, h3⟩ := held_wakeOne (a := a) (now := now) (cfg := cfg) h hid
    obtain ⟨h4, h5, h6⟩ := held_wakeAll l _ h1 (h2 ▸ hid)
    exact ⟨h4, h5.trans h2, fun x hx => h6 x (h3 x hx)⟩

/-- A settlement chain on a settled promise repairs that promise's object and
breaks nothing else. -/
theorem held_trigger {E : String → Prop} {tx : Tx} {id now cfg}
    (h : Held E tx.doc) (hs : Settled tx.doc id) :
    Held (fun x => E x ∧ x ≠ id) (triggerSettlement tx id now cfg).doc ∧
      (triggerSettlement tx id now cfg).doc.ids = tx.doc.ids ∧
      ∀ x, Settled tx.doc x → Settled (triggerSettlement tx id now cfg).doc x := by
  unfold triggerSettlement
  split
  · rename_i hg
    refine ⟨⟨h.nodup, h.sorted, h.weak, (fun o ho hn => h.agrees o ho fun he => hn ⟨he, get_none hg o ho⟩), h.refs⟩,
      rfl, fun _ hx => hx⟩
  · rename_i o hg
    obtain ⟨ho, hoid⟩ := get_mem hg
    -- fulfil the promise's own task
    have h1 : Held (fun x => E x ∧ x ≠ id) (tx.doc.modify id fulfilTask) :=
      held_modify h fulfilTask_id hg (weak_fulfilTask (h.weak o ho))
        (fun _ => agrees_fulfilTask (hs o ho hoid))
        (refs_fulfilTask (h.refs o ho)) (fun x hx he => ⟨he, hx⟩)
    have hg1 := get_modify_self h.nodup fulfilTask_id hg
    have hids1 := ids_modify tx.doc id fulfilTask_id
    -- take its callbacks
    have h2 : Held (fun x => E x ∧ x ≠ id) ((tx.doc.modify id fulfilTask).modify id clearCallbacks) :=
      held_modify' h1 clearCallbacks_id hg1 weak_clearCallbacks agrees_clearCallbacks refs_clearCallbacks
    have hids2 := ids_modify (tx.doc.modify id fulfilTask) id clearCallbacks_id
    have hid : id ∈ ((tx.doc.modify id fulfilTask).modify id clearCallbacks).ids := by
      rw [hids2, hids1]; exact hoid ▸ mem_ids ho
    have hs2 : ∀ x, Settled tx.doc x → Settled ((tx.doc.modify id fulfilTask).modify id clearCallbacks) x :=
      fun x hx => settled_modify clearCallbacks_id (fun _ => rfl)
        (settled_modify fulfilTask_id (fun o => by rw [fulfilTask_promise]) hx)
    -- wake every awaiter
    obtain ⟨h3, hids3, hs3⟩ := held_wakeAll (now := now) (cfg := cfg) o.promise.callbacks
      ((tx.modify id fulfilTask).modify id clearCallbacks) h2 hid
    simp only [Tx.modify_doc] at h3 hids3 hs3
    -- then its listeners
    dsimp only
    split
    · exact ⟨h3, by rw [hids3, hids2, hids1], fun x hx => hs3 x (hs2 x hx)⟩
    · rename_i o' hg'
      obtain ⟨ho', ho'id⟩ := get_mem hg'
      refine ⟨?_, ?_, ?_⟩
      · exact held_modify h3 clearListeners_id hg' (weak_clearListeners (h3.weak o' ho'))
          (fun _ => agrees_clearListeners (h3.agrees o' ho' (by simp [ho'id])))
          (refs_clearListeners (h3.refs o' ho')) (fun _ _ he => he)
      · simp only [Tx.modify_doc]
        rw [ids_modify _ _ clearListeners_id, hids3, hids2, hids1]
      · intro x hx
        exact settled_modify clearListeners_id (fun _ => rfl) (hs3 x (hs2 x hx))

/-! ## The four phases -/

theorem ids_map {d : Doc} {g : Obj → Obj} (hg : ∀ o, (g o).id = o.id) :
    ({ d with objects := d.objects.map g } : Doc).ids = d.ids := by
  simp only [Doc.ids, List.map_map]
  congr 1; funext o; simp [hg]

theorem held_phase2 {now cfg} :
    ∀ (l : List String) (tx : Tx) (E : String → Prop), Held E tx.doc →
      (∀ x ∈ l, Settled tx.doc x) → (∀ x, E x → x ∈ l) →
      Core (l.foldl (fun tx id => triggerSettlement tx id now cfg) tx).doc
  | [], _, _, h, _, hE => h.mono fun x hx => by simpa using hE x hx
  | id :: l, tx, E, h, hs, hE => by
    obtain ⟨h1, _, h3⟩ := held_trigger (now := now) (cfg := cfg) h (hs id (by simp))
    refine held_phase2 l _ _ h1 (fun x hx => h3 x (hs x (by simp [hx]))) ?_
    intro x ⟨he, hne⟩
    rcases List.mem_cons.1 (hE x he) with rfl | hx
    · exact absurd rfl hne
    · exact hx

theorem core_phase1 {d : Doc} {now} (h : Core d) :
    Held (fun x => x ∈ (d.objects.filter (expiring now)).map (·.id))
      { d with objects := d.objects.map fun o => if expiring now o then expire o else o } ∧
    ∀ x ∈ (d.objects.filter (expiring now)).map (·.id),
      Settled { d with objects := d.objects.map fun o => if expiring now o then expire o else o } x := by
  have hid : ∀ o : Obj, (if expiring now o then expire o else o).id = o.id := by
    intro o; split <;> rfl
  have hids := ids_map (d := d) hid
  refine ⟨⟨hids ▸ h.nodup, sorted_map hid h.sorted, ?_, ?_, ?_⟩, ?_⟩
  · intro o' ho'
    obtain ⟨o, ho, rfl⟩ := List.mem_map.1 ho'
    split
    · exact weak_expire (h.weak o ho)
    · exact h.weak o ho
  · intro o' ho' hn
    obtain ⟨o, ho, rfl⟩ := List.mem_map.1 ho'
    split
    · rename_i he
      exact absurd (List.mem_map.2 ⟨o, List.mem_filter.2 ⟨ho, he⟩, rfl⟩) (by simpa [hid] using hn)
    · exact h.agrees o ho (by simp)
  · intro o' ho'
    rw [hids]
    obtain ⟨o, ho, rfl⟩ := List.mem_map.1 ho'
    split
    · exact h.refs o ho
    · exact h.refs o ho
  · intro x hx o' ho' hox
    obtain ⟨o3, ho3, rfl⟩ := List.mem_map.1 hx
    obtain ⟨ho3, he3⟩ := List.mem_filter.1 ho3
    obtain ⟨o, ho, rfl⟩ := List.mem_map.1 ho'
    have : o = o3 := nodup_map_inj h.nodup ho ho3 (by simpa [hid] using hox)
    subst this
    simp only [he3, if_true]
    exact expire_settled o

theorem core_sweepPhase {tx : Tx} {due : Obj → Bool} {f : Obj → Obj}
    (h : Core tx.doc) (hf : IdPres f)
    (hl : ∀ o, due o → Local o → Local (f o)) (hr : ∀ ids o, Refs ids o → Refs ids (f o)) :
    Core (sweepPhase due f tx).doc := by
  have hid : ∀ o : Obj, (if due o then f o else o).id = o.id := by
    intro o; split
    · exact hf o
    · rfl
  have hids := ids_map (d := tx.doc) hid
  have hloc := h.local
  refine ⟨?_, sorted_map hid h.sorted, ?_, ?_, ?_⟩
  · simp only [sweepPhase]; rw [hids]; exact h.nodup
  · intro o' ho'
    obtain ⟨o, ho, rfl⟩ := List.mem_map.1 ho'
    split
    · rename_i hd; exact (hl o hd (hloc o ho)).1
    · exact (hloc o ho).1
  · intro o' ho' _
    obtain ⟨o, ho, rfl⟩ := List.mem_map.1 ho'
    split
    · rename_i hd; exact (hl o hd (hloc o ho)).2
    · exact (hloc o ho).2
  · intro o' ho'
    simp only [sweepPhase]; rw [hids]
    obtain ⟨o, ho, rfl⟩ := List.mem_map.1 ho'
    split
    · exact hr _ o (h.refs o ho)
    · exact h.refs o ho

section Phases

attribute [local simp] TaskShape Task.disarm Task.armRetry Task.armLease

theorem local_rearm {now cfg} {o : Obj} (hd : retryDue now o) (h : Local o) : Local (rearm now cfg o) := by
  obtain ⟨⟨h1, h2, h3, h4⟩, ha⟩ := h
  unfold retryDue at hd
  unfold rearm
  split at hd
  · rename_i t ht
    simp only [Bool.and_eq_true, beq_iff_eq] at hd
    refine ⟨⟨h1, h2, ?_, by simpa [ht] using h4⟩, ?_⟩
    · intro t' e; simp [ht] at e; subst e; simp [hd.1]
    · intro t' e; simp [ht] at e; subst e; simpa using ha t ht
  · simp at hd

theorem local_reclaim {now cfg} {o : Obj} (hd : leaseDue now o) (h : Local o) : Local (reclaim now cfg o) := by
  obtain ⟨⟨h1, h2, h3, h4⟩, ha⟩ := h
  unfold leaseDue at hd
  unfold reclaim
  split at hd
  · rename_i t ht
    simp only [Bool.and_eq_true, beq_iff_eq] at hd
    have hp := (ha t ht).2 (by simp [hd.1])
    refine ⟨⟨h1, h2, ?_, by simpa [ht] using h4⟩, ?_⟩
    · intro t' e; simp [ht] at e; subst e; simp
    · intro t' e; simp [ht] at e; subst e; simp [hp]
  · simp at hd

theorem refs_rearm {now cfg} (ids) (o : Obj) (h : Refs ids o) : Refs ids (rearm now cfg o) := by
  refine ⟨h.1, fun t e r hr => ?_⟩
  simp only [rearm, Option.map_eq_some_iff] at e
  obtain ⟨t0, h0, rfl⟩ := e
  exact h.2 t0 h0 r hr

theorem refs_reclaim {now cfg} (ids) (o : Obj) (h : Refs ids o) : Refs ids (reclaim now cfg o) := by
  refine ⟨h.1, fun t e r hr => ?_⟩
  simp only [reclaim, Option.map_eq_some_iff] at e
  obtain ⟨t0, h0, rfl⟩ := e
  exact h.2 t0 h0 r hr

end Phases

/-- The sweep, all four phases. -/
theorem core_sweepTx {d : Doc} {now cfg} (h : Core d) : Core (sweepTx d now cfg).doc := by
  unfold sweepTx
  obtain ⟨h1, hs1⟩ := core_phase1 (now := now) h
  have h2 := held_phase2 (now := now) (cfg := cfg) _ { doc := _ } _ h1 hs1 (fun _ hx => hx)
  have h3 := core_sweepPhase (due := retryDue now) (f := rearm now cfg) h2 (fun _ => rfl)
    (fun _ hd hl => local_rearm hd hl) refs_rearm
  exact core_sweepPhase h3 (fun _ => rfl) (fun _ hd hl => local_reclaim hd hl) refs_reclaim

end Kernel
