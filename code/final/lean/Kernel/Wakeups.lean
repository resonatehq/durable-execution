import Kernel.Fencing

/-!
# No lost wakeups

A suspended task has given up its lease and waits to be told that something
it awaits has settled. The only thing that ever tells it is a settlement
chain running on a promise it is registered on. So if some reachable
document held a suspended task registered on no pending promise, that task
would be parked forever. `reachable_noLost` says that never happens:

**In every reachable document, every suspended task is registered as a
callback on a promise that is still pending.**

This is the argument `kernel.py`'s `promise_register_callback` makes in prose
for why registering on a settled promise may do nothing, here as a theorem.

Mid-sweep, a promise may have expired without its chain having run yet. The
proof carries `Rungs E`, which also accepts a registration on such a promise,
or a suspended task that is itself expiring (its own chain will fulfil it);
phase 2 empties `E` one chain at a time. What makes a chain wake its
awaiters rather than skip them is `Fresh`: after the sweep's first phase,
every pending promise's deadline is in the future.
-/

namespace Kernel

open Doc

def Suspended (o : Obj) : Prop := ∃ t, o.task = some t ∧ t.state = .suspended

/-- Every suspended task can still be woken: registered on a pending promise,
or on one whose chain is yet to run (`E`), or expiring itself. -/
def Rungs (E : String → Prop) (d : Doc) : Prop :=
  ∀ o ∈ d.objects, Suspended o →
    E o.id ∨ ∃ p ∈ d.objects, o.id ∈ p.promise.callbacks ∧ (p.promise.state = .pending ∨ E p.id)

/-- **Every suspended task is registered on a pending promise.** -/
def NoLost (d : Doc) : Prop :=
  ∀ o ∈ d.objects, Suspended o → ∃ p ∈ d.objects, o.id ∈ p.promise.callbacks ∧ p.promise.state = .pending

theorem noLost_iff {d : Doc} : NoLost d ↔ Rungs (fun _ => False) d := by
  constructor
  · intro h o ho hs
    obtain ⟨p, hp, hc, hs⟩ := h o ho hs
    exact Or.inr ⟨p, hp, hc, Or.inl hs⟩
  · intro h o ho hs
    rcases h o ho hs with he | ⟨p, hp, hc, hs | he⟩
    · exact he.elim
    · exact ⟨p, hp, hc, hs⟩
    · exact he.elim

/-- Every pending promise's deadline is after `now`. -/
def Fresh (now : Int) (d : Doc) : Prop := ∀ o ∈ d.objects, o.promise.state = .pending → now < o.promise.timeoutAt

/-! ## Changing objects in place -/

/-- What an in-place change keeps: the id, the promise's state and deadline,
every callback unless the object is one `K` picks, and it never parks a task. -/
structure Keeps (K : String → Prop) (o o' : Obj) : Prop where
  id : o'.id = o.id
  state : o'.promise.state = o.promise.state
  timeoutAt : o'.promise.timeoutAt = o.promise.timeoutAt
  callbacks : ¬ K o.id → ∀ a ∈ o.promise.callbacks, a ∈ o'.promise.callbacks
  susp : Suspended o' → Suspended o

theorem Keeps.refl (K) (o : Obj) : Keeps K o o := ⟨rfl, rfl, rfl, fun _ _ h => h, fun h => h⟩

theorem Keeps.trans {K} {a b c : Obj} (h1 : Keeps K a b) (h2 : Keeps K b c) : Keeps K a c :=
  ⟨h2.id.trans h1.id, h2.state.trans h1.state, h2.timeoutAt.trans h1.timeoutAt,
    fun hk x hx => h2.callbacks (by rw [h1.id]; exact hk) x (h1.callbacks hk x hx), fun hs => h1.susp (h2.susp hs)⟩

/-- `d'` is `d`, object for object, each changed in a way `Keeps` allows. -/
def MapsBy (K : String → Prop) (d d' : Doc) : Prop :=
  ∃ g : Obj → Obj, d'.objects = d.objects.map g ∧ ∀ o ∈ d.objects, Keeps K o (g o)

theorem MapsBy.refl (K) (d : Doc) : MapsBy K d d := ⟨fun o => o, by simp, fun o _ => Keeps.refl K o⟩

theorem MapsBy.trans {K} {a b c : Doc} (h1 : MapsBy K a b) (h2 : MapsBy K b c) : MapsBy K a c := by
  obtain ⟨g1, e1, k1⟩ := h1
  obtain ⟨g2, e2, k2⟩ := h2
  refine ⟨g2 ∘ g1, by rw [e2, e1, List.map_map], fun o ho => (k1 o ho).trans (k2 _ ?_)⟩
  rw [e1]; exact List.mem_map_of_mem ho

theorem mapsBy_modify {K} {d : Doc} {id f} (h : ∀ o ∈ d.objects, o.id = id → Keeps K o (f o)) :
    MapsBy K d (d.modify id f) := by
  refine ⟨fun o => if o.id == id then f o else o, rfl, fun o ho => ?_⟩
  by_cases hx : o.id = id
  · simpa [hx] using h o ho hx
  · simpa [hx] using Keeps.refl K o

theorem mapsBy_modify_any {K} {d : Doc} {id f} (h : ∀ o, Keeps K o (f o)) : MapsBy K d (d.modify id f) :=
  mapsBy_modify fun o _ _ => h o

theorem MapsBy.ids {K} {d d' : Doc} (h : MapsBy K d d') : d'.ids = d.ids := by
  obtain ⟨g, e, k⟩ := h
  simp only [Doc.ids, e, List.map_map]
  exact List.map_congr_left fun o ho => (k o ho).id

/-- Every object of `d'` comes from one of `d`. -/
theorem MapsBy.back {K} {d d' : Doc} (h : MapsBy K d d') {o'} (ho' : o' ∈ d'.objects) :
    ∃ o ∈ d.objects, Keeps K o o' := by
  obtain ⟨g, e, k⟩ := h
  rw [e] at ho'
  obtain ⟨o, ho, rfl⟩ := List.mem_map.1 ho'
  exact ⟨o, ho, k o ho⟩

/-- Every object of `d` goes to one of `d'`. -/
theorem MapsBy.fwd {K} {d d' : Doc} (h : MapsBy K d d') {o} (ho : o ∈ d.objects) :
    ∃ o' ∈ d'.objects, Keeps K o o' := by
  obtain ⟨g, e, k⟩ := h
  exact ⟨g o, by rw [e]; exact List.mem_map_of_mem ho, k o ho⟩

/-- The workhorse: a change that loses no callback keeps every rung. -/
theorem Rungs.maps {E : String → Prop} {d d' : Doc} (h : Rungs E d) (hm : MapsBy (fun _ => False) d d') :
    Rungs E d' := by
  intro o' ho' hs'
  obtain ⟨o, ho, k⟩ := hm.back ho'
  rcases h o ho (k.susp hs') with he | ⟨p, hp, hc, hps⟩
  · exact Or.inl (k.id ▸ he)
  · obtain ⟨p', hp', kp⟩ := hm.fwd hp
    refine Or.inr ⟨p', hp', k.id ▸ kp.callbacks (fun h => h) _ hc, ?_⟩
    rw [kp.state, kp.id]; exact hps

theorem Fresh.maps {K} {now} {d d' : Doc} (h : Fresh now d) (hm : MapsBy K d d') : Fresh now d' := by
  intro o' ho' hp
  obtain ⟨o, ho, k⟩ := hm.back ho'
  rw [k.timeoutAt]; exact h o ho (k.state ▸ hp)

/-! ## The writes that keep -/

section Keeps

variable {K : String → Prop}

theorem not_susp_of_task {o : Obj} {g : Task → Task} (hg : ∀ t, (g t).state = .suspended → t.state = .suspended) :
    Suspended { o with task := o.task.map g } → Suspended o := by
  rintro ⟨t, e, hs⟩
  simp only [Option.map_eq_some_iff] at e
  obtain ⟨t0, h0, rfl⟩ := e
  exact ⟨t0, h0, hg t0 hs⟩

theorem keeps_taskMap {o : Obj} {g : Task → Task} (hg : ∀ t, (g t).state = .suspended → t.state = .suspended) :
    Keeps K o { o with task := o.task.map g } :=
  ⟨rfl, rfl, rfl, fun _ _ h => h, not_susp_of_task hg⟩

theorem keeps_fulfilTask (o : Obj) : Keeps K o (fulfilTask o) := by
  unfold fulfilTask
  split
  · rename_i t ht
    split
    · refine ⟨rfl, rfl, rfl, fun _ _ h => h, ?_⟩
      rintro ⟨t', e, hs⟩; cases e; cases hs
    · exact Keeps.refl K o
  · exact Keeps.refl K o

theorem keeps_wake (id now cfg) (o : Obj) : Keeps K o (wake id now cfg o) := by
  unfold wake
  split
  · rename_i t ht
    split
    · refine ⟨rfl, rfl, rfl, fun _ _ h => h, ?_⟩
      rintro ⟨t', e, hs⟩; cases e; cases hs
    · split
      · refine ⟨rfl, rfl, rfl, fun _ _ h => h, ?_⟩
        rintro ⟨t', e, hs⟩; cases e; exact ⟨t, ht, hs⟩
      · exact Keeps.refl K o
  · exact Keeps.refl K o

theorem keeps_clearCallbacks {o : Obj} (hk : K o.id) : Keeps K o (clearCallbacks o) :=
  ⟨rfl, rfl, rfl, fun h => absurd hk h, fun h => h⟩

theorem keeps_clearListeners (o : Obj) : Keeps K o (clearListeners o) := ⟨rfl, rfl, rfl, fun _ _ h => h, fun h => h⟩

theorem keeps_addCallback (a) (o : Obj) : Keeps K o (addCallback a o) := by
  unfold addCallback; split
  · exact Keeps.refl K o
  · exact ⟨rfl, rfl, rfl, fun _ x hx => List.mem_append_left _ hx, fun h => h⟩

theorem keeps_addListener (a) (o : Obj) : Keeps K o (addListener a o) := by
  unfold addListener; split
  · exact Keeps.refl K o
  · exact ⟨rfl, rfl, rfl, fun _ _ h => h, fun h => h⟩

theorem keeps_beat (pid v now) (o : Obj) : Keeps K o (beat pid v now o) := by
  unfold beat
  split
  · rename_i t ht
    split
    · split
      · refine ⟨rfl, rfl, rfl, fun _ _ h => h, ?_⟩
        rintro ⟨t', e, hs⟩; cases e; exact ⟨t, ht, hs⟩
      · exact Keeps.refl K o
    · exact Keeps.refl K o
  · exact Keeps.refl K o

end Keeps

/-! ## One settlement chain -/

theorem not_susp_fulfilTask (o : Obj) : ¬ Suspended (fulfilTask o) := by
  rintro ⟨t, e, hs⟩
  unfold fulfilTask at e
  split at e
  · rename_i t0 ht0
    split at e
    · cases e; cases hs
    · rename_i hne; rw [ht0] at e; cases e; exact hne (by rw [hs]; simp)
  · rename_i hn; rw [hn] at e; cases e

theorem mapsBy_wakeOne {K} {id now cfg a} (tx : Tx) : MapsBy K tx.doc (wakeOne id now cfg tx a).doc := by
  unfold wakeOne
  split
  · exact MapsBy.refl K _
  · split
    · exact MapsBy.refl K _
    · split
      · exact MapsBy.refl K _
      · split
        · simpa using mapsBy_modify_any (keeps_wake id now cfg)
        · simpa using mapsBy_modify_any (keeps_wake id now cfg)

theorem mapsBy_wakeAll {K} {id now cfg} :
    ∀ (l : List String) (tx : Tx), MapsBy K tx.doc (l.foldl (wakeOne id now cfg) tx).doc
  | [], _ => MapsBy.refl K _
  | a :: l, tx => (mapsBy_wakeOne tx).trans (mapsBy_wakeAll l _)

/-- The object named `a`, if any, is not suspended. -/
def Unparked (d : Doc) (a : String) : Prop := ∀ o ∈ d.objects, o.id = a → ¬ Suspended o

theorem Unparked.maps {K} {d d' : Doc} {a} (h : Unparked d a) (hm : MapsBy K d d') : Unparked d' a := by
  intro o' ho' hid hs
  obtain ⟨o, ho, k⟩ := hm.back ho'
  exact h o ho (k.id ▸ hid) (k.susp hs)

/-- Waking an awaiter whose promise is pending and not yet due leaves it unparked. -/
theorem unparked_wakeOne {id now cfg a} {tx : Tx} (hn : tx.doc.ids.Nodup)
    (hp : ∀ o ∈ tx.doc.objects, o.id = a → o.promise.state = .pending ∧ now < o.promise.timeoutAt) :
    Unparked (wakeOne id now cfg tx a).doc a := by
  unfold wakeOne
  split
  · rename_i hg
    intro o ho hid; exact absurd hid (get_none hg o ho)
  · rename_i ao hg
    obtain ⟨hao, haid⟩ := get_mem hg
    have ⟨hpa, hna⟩ := hp ao hao haid
    split
    · rename_i ht
      intro o ho hid hs
      rw [eq_of_get hn hg ho hid] at hs
      obtain ⟨t, e, _⟩ := hs; rw [ht] at e; cases e
    · rename_i t ht
      split
      · rename_i hc
        exfalso
        simp only [Bool.or_eq_true, decide_eq_true_eq] at hc
        rcases hc with h | h
        · exact h hpa
        · exact absurd h (Int.not_le.2 hna)
      · have hw : Unparked (tx.doc.modify a (wake id now cfg)) a := by
          intro o ho hid hs
          have hg' := get_modify_self hn (wake_id id now cfg) hg
          rw [eq_of_get (by rwa [ids_modify _ _ (wake_id id now cfg)]) hg' ho hid] at hs
          obtain ⟨t', e, hs'⟩ := hs
          unfold wake at e; rw [ht] at e
          simp only at e
          split at e
          · cases e; cases hs'
          · split at e
            · cases e; rename_i hs0 _; simp_all
            · rw [ht] at e; cases e; rename_i hs0 _; simp_all
        split
        · simpa using hw
        · simpa using hw

/-- A settlement's fan-out leaves every pending, not-yet-due awaiter unparked. -/
theorem unparked_wakeAll {id now cfg a} :
    ∀ (l : List String) (tx : Tx), a ∈ l → tx.doc.ids.Nodup →
      (∀ o ∈ tx.doc.objects, o.id = a → o.promise.state = .pending ∧ now < o.promise.timeoutAt) →
      Unparked (l.foldl (wakeOne id now cfg) tx).doc a
  | [], _, ha, _, _ => absurd ha List.not_mem_nil
  | b :: l, tx, ha, hn, hp => by
    simp only [List.foldl_cons]
    have hm : MapsBy (fun _ => False) tx.doc (wakeOne id now cfg tx b).doc := mapsBy_wakeOne tx
    by_cases hl : a ∈ l
    · refine unparked_wakeAll l _ hl (by rw [hm.ids]; exact hn) ?_
      intro o' ho' hid
      obtain ⟨o, ho, k⟩ := hm.back ho'
      rw [k.state, k.timeoutAt]; exact hp o ho (k.id ▸ hid)
    · have hb : a = b := by
        rcases List.mem_cons.1 ha with h | h
        · exact h
        · exact absurd h hl
      subst hb
      exact (unparked_wakeOne hn hp).maps (mapsBy_wakeAll (K := fun _ => False) l _)

theorem held_noObj {E : String → Prop} {d : Doc} {x} (h : Held E d) (hg : d.get x = none) :
    Held (fun y => E y ∧ y ≠ x) d :=
  ⟨h.nodup, h.sorted, h.weak, (fun o ho hn => h.agrees o ho fun he => hn ⟨he, get_none hg o ho⟩), h.refs⟩

/-- One settlement chain takes its promise out of `E` and strands no one. -/
theorem rungs_trigger {E : String → Prop} {tx : Tx} {x now cfg}
    (hh : Held E tx.doc) (hs : Settled tx.doc x) (hr : Rungs E tx.doc) (hf : Fresh now tx.doc) :
    Rungs (fun y => E y ∧ y ≠ x) (triggerSettlement tx x now cfg).doc ∧
      MapsBy (fun y => y = x) tx.doc (triggerSettlement tx x now cfg).doc := by
  unfold triggerSettlement
  split
  · rename_i hg
    refine ⟨?_, MapsBy.refl _ _⟩
    intro o ho hso
    rcases hr o ho hso with he | ⟨p, hp, hc, hps | he⟩
    · exact Or.inl ⟨he, get_none hg o ho⟩
    · exact Or.inr ⟨p, hp, hc, Or.inl hps⟩
    · exact Or.inr ⟨p, hp, hc, Or.inr ⟨he, get_none hg p hp⟩⟩
  · rename_i ox hg
    obtain ⟨hox, hoxid⟩ := get_mem hg
    dsimp only
    -- the document after the task is fulfilled and the callbacks taken
    have m1 : MapsBy (fun y => y = x) tx.doc ((tx.doc.modify x fulfilTask).modify x clearCallbacks) :=
      (mapsBy_modify_any keeps_fulfilTask).trans (mapsBy_modify fun o _ hid => keeps_clearCallbacks hid)
    have hn2 : ((tx.doc.modify x fulfilTask).modify x clearCallbacks).ids.Nodup := by
      rw [m1.ids]; exact hh.nodup
    -- the promise's own object is unparked from here on
    have hu1 : Unparked (tx.doc.modify x fulfilTask) x := by
      intro o' ho' hid hs'
      obtain ⟨o0, _, rfl⟩ := mem_modify ho'
      split at hs'
      · exact not_susp_fulfilTask o0 hs'
      · rename_i h0
        rw [if_neg h0] at hid
        exact h0 (by simp [hid])
    have hu2 : Unparked ((tx.doc.modify x fulfilTask).modify x clearCallbacks) x :=
      hu1.maps (mapsBy_modify (K := fun y => y = x) fun o _ hid => keeps_clearCallbacks hid)
    let tx2 := (tx.modify x fulfilTask).modify x clearCallbacks
    have m3 : MapsBy (fun y => y = x) tx.doc (ox.promise.callbacks.foldl (wakeOne x now cfg) tx2).doc :=
      m1.trans (mapsBy_wakeAll _ _)
    have hu3 : Unparked (ox.promise.callbacks.foldl (wakeOne x now cfg) tx2).doc x :=
      hu2.maps (mapsBy_wakeAll (K := fun _ => False) _ tx2)
    -- the rungs, for any document that `Keeps` everything from here and parks nothing new at x
    have key : ∀ d4 : Doc, MapsBy (fun y => y = x) tx.doc d4 → Unparked d4 x →
        (∀ a ∈ ox.promise.callbacks, (∀ o ∈ tx.doc.objects, o.id = a → o.promise.state = .pending) → Unparked d4 a) →
        Rungs (fun y => E y ∧ y ≠ x) d4 := by
      intro d4 m4 hu4 hw4 o' ho' hs'
      obtain ⟨o, ho, k⟩ := m4.back ho'
      have hxo : o.id ≠ x := fun e => hu4 o' ho' (k.id.trans e) hs'
      rcases hr o ho (k.susp hs') with he | ⟨p, hp, hc, hps⟩
      · exact Or.inl ⟨k.id ▸ he, k.id ▸ hxo⟩
      · by_cases hpx : p.id = x
        · -- registered on the promise that just settled: it was woken, unless it is expiring itself
          have hpo : p = ox := eq_of_get hh.nodup hg hp hpx
          subst hpo
          by_cases hpend : o.promise.state = .pending
          · exact absurd hs' (hw4 o.id hc (fun o2 ho2 hid2 => by
              rw [nodup_map_inj hh.nodup ho2 ho hid2]; exact hpend) o' ho' k.id)
          · have hE : E o.id := Classical.byContradiction fun hne => by
              obtain ⟨t, e, hst⟩ := k.susp hs'
              exact hpend ((hh.agrees o ho hne t e).2 (by rw [hst]; simp))
            exact Or.inl ⟨k.id ▸ hE, k.id ▸ hxo⟩
        · obtain ⟨p', hp', kp⟩ := m4.fwd hp
          refine Or.inr ⟨p', hp', k.id ▸ kp.callbacks hpx _ hc, ?_⟩
          rw [kp.state, kp.id]
          rcases hps with h | h
          · exact Or.inl h
          · exact Or.inr ⟨h, hpx⟩
    have hw3 : ∀ a ∈ ox.promise.callbacks, (∀ o ∈ tx.doc.objects, o.id = a → o.promise.state = .pending) →
        Unparked (ox.promise.callbacks.foldl (wakeOne x now cfg) tx2).doc a := by
      intro a ha hpa
      refine unparked_wakeAll _ tx2 ha hn2 fun o' ho' hid => ?_
      obtain ⟨o, ho, k⟩ := m1.back ho'
      rw [k.state, k.timeoutAt]
      have hpo := hpa o ho (k.id ▸ hid)
      exact ⟨hpo, hf o ho hpo⟩
    split
    · exact ⟨key _ m3 hu3 hw3, m3⟩
    · have m4 : MapsBy (fun y => y = x) (ox.promise.callbacks.foldl (wakeOne x now cfg) tx2).doc
          ((ox.promise.callbacks.foldl (wakeOne x now cfg) tx2).doc.modify x clearListeners) :=
        mapsBy_modify_any keeps_clearListeners
      have m4' : MapsBy (fun _ => False) (ox.promise.callbacks.foldl (wakeOne x now cfg) tx2).doc
          ((ox.promise.callbacks.foldl (wakeOne x now cfg) tx2).doc.modify x clearListeners) :=
        mapsBy_modify_any keeps_clearListeners
      simp only [Tx.modify_doc]
      exact ⟨key _ (m3.trans m4) (hu3.maps m4') (fun a ha hpa => (hw3 a ha hpa).maps m4'), m3.trans m4⟩

theorem Rungs.mono {E E' : String → Prop} {d : Doc} (h : Rungs E d) (hE : ∀ x, E x → E' x) : Rungs E' d := by
  intro o ho hs
  rcases h o ho hs with he | ⟨p, hp, hc, hps | he⟩
  · exact Or.inl (hE _ he)
  · exact Or.inr ⟨p, hp, hc, Or.inl hps⟩
  · exact Or.inr ⟨p, hp, hc, Or.inr (hE _ he)⟩

/-- Settling some promises in place, and touching nothing else they carry,
leaves every suspended task with a rung, pending or among the settled. -/
theorem rungs_settleMap {d d' : Doc} {g : Obj → Obj} {S : String → Prop} (h : NoLost d)
    (he : d'.objects = d.objects.map g) (hid : ∀ o, (g o).id = o.id)
    (hcb : ∀ o, (g o).promise.callbacks = o.promise.callbacks) (ht : ∀ o, (g o).task = o.task)
    (hst : ∀ o ∈ d.objects, (g o).promise.state = o.promise.state ∨ S o.id) : Rungs S d' := by
  intro o' ho' hs'
  rw [he] at ho'
  obtain ⟨o, ho, rfl⟩ := List.mem_map.1 ho'
  obtain ⟨t, e, hst'⟩ := hs'
  obtain ⟨p, hp, hc, hpp⟩ := h o ho ⟨t, (ht o) ▸ e, hst'⟩
  refine Or.inr ⟨g p, he ▸ List.mem_map_of_mem hp, by rw [hid, hcb]; exact hc, ?_⟩
  rcases hst p hp with e | e
  · exact Or.inl (e.trans hpp)
  · exact Or.inr (by rw [hid]; exact e)

/-! ## The sweep -/

theorem sweep_phase1 {d : Doc} {now} (h : NoLost d) :
    Rungs (fun x => x ∈ (d.objects.filter (expiring now)).map (·.id))
      { d with objects := d.objects.map fun o => if expiring now o then expire o else o } ∧
    Fresh now { d with objects := d.objects.map fun o => if expiring now o then expire o else o } := by
  refine ⟨rungs_settleMap h rfl (fun o => by split <;> rfl) (fun o => by split <;> rfl)
    (fun o => by split <;> rfl) (fun o ho => ?_), ?_⟩
  · split
    · rename_i he; exact Or.inr (List.mem_map.2 ⟨o, List.mem_filter.2 ⟨ho, he⟩, rfl⟩)
    · exact Or.inl rfl
  · intro o' ho' hp
    obtain ⟨o, _, rfl⟩ := List.mem_map.1 ho'
    by_cases he : expiring now o = true
    · rw [if_pos he] at hp; exact absurd hp (expire_settled o)
    · rw [if_neg he] at hp ⊢
      simp only [expiring, hp, beq_self_eq_true, Bool.true_and, decide_eq_true_eq] at he
      omega

theorem sweep_phase2 {now cfg} :
    ∀ (l : List String) (tx : Tx) (E : String → Prop), Held E tx.doc →
      (∀ x ∈ l, Settled tx.doc x) → (∀ x, E x → x ∈ l) → Rungs E tx.doc → Fresh now tx.doc →
      NoLost (l.foldl (fun tx id => triggerSettlement tx id now cfg) tx).doc ∧
        Fresh now (l.foldl (fun tx id => triggerSettlement tx id now cfg) tx).doc
  | [], _, _, _, _, hE, hr, hf => ⟨noLost_iff.2 (hr.mono fun x hx => by simpa using hE x hx), hf⟩
  | x :: l, tx, E, h, hs, hE, hr, hf => by
    obtain ⟨h1, _, h3⟩ := held_trigger (now := now) (cfg := cfg) h (hs x (by simp))
    obtain ⟨r1, m1⟩ := rungs_trigger (now := now) (cfg := cfg) h (hs x (by simp)) hr hf
    refine sweep_phase2 l _ _ h1 (fun y hy => h3 y (hs y (by simp [hy]))) ?_ r1 (hf.maps m1)
    intro y ⟨he, hne⟩
    rcases List.mem_cons.1 (hE y he) with rfl | hy
    · exact absurd rfl hne
    · exact hy

theorem mapsBy_sweepPhase {tx : Tx} {due : Obj → Bool} {f : Obj → Obj} (hk : ∀ o, due o → Keeps (fun _ => False) o (f o)) :
    MapsBy (fun _ => False) tx.doc (sweepPhase due f tx).doc := by
  refine ⟨fun o => if due o then f o else o, rfl, fun o _ => ?_⟩
  show Keeps _ o (if due o then f o else o)
  split
  · rename_i hd; exact hk o hd
  · exact Keeps.refl _ o

theorem phase34_noLost {tx : Tx} {now cfg} (h : NoLost tx.doc) (hf : Fresh now tx.doc) :
    NoLost (sweepPhase (leaseDue now) (reclaim now cfg) (sweepPhase (retryDue now) (rearm now cfg) tx)).doc ∧
      Fresh now (sweepPhase (leaseDue now) (reclaim now cfg) (sweepPhase (retryDue now) (rearm now cfg) tx)).doc := by
  have m3 := mapsBy_sweepPhase (tx := tx) (due := retryDue now) (f := rearm now cfg)
    (fun o _ => keeps_taskMap fun t hs => by simpa [Task.armRetry] using hs)
  have m4 := mapsBy_sweepPhase (due := leaseDue now) (f := reclaim now cfg)
    (tx := sweepPhase (retryDue now) (rearm now cfg) tx)
    (fun o _ => keeps_taskMap fun t hs => by simp [Task.armRetry] at hs)
  have m := m3.trans m4
  exact ⟨noLost_iff.2 ((noLost_iff.1 h).maps m), hf.maps m⟩

theorem sweep_noLost {d : Doc} {now cfg} (hc : Core d) (h : NoLost d) :
    NoLost (sweepTx d now cfg).doc ∧ Fresh now (sweepTx d now cfg).doc := by
  unfold sweepTx
  obtain ⟨h1, hs1⟩ := core_phase1 (now := now) hc
  obtain ⟨r1, f1⟩ := sweep_phase1 (now := now) h
  obtain ⟨n2, f2⟩ := sweep_phase2 (now := now) (cfg := cfg) _ { doc := _ } _ h1 hs1 (fun _ hx => hx) r1 f1
  exact phase34_noLost n2 f2

/-! ## The operations -/

theorem rungs_insert {E : String → Prop} {d : Doc} {o} (h : Rungs E d) (ho : ¬ Suspended o) : Rungs E (d.insert o) := by
  intro o' ho' hs'
  rcases mem_insert.1 ho' with rfl | ho'
  · exact absurd hs' ho
  · rcases h o' ho' hs' with he | ⟨p, hp, hc, hps⟩
    · exact Or.inl he
    · exact Or.inr ⟨p, mem_insert.2 (Or.inr hp), hc, hps⟩

theorem noLost_maps {d d' : Doc} (h : NoLost d) (m : MapsBy (fun _ => False) d d') : NoLost d' :=
  noLost_iff.2 ((noLost_iff.1 h).maps m)

theorem noLost_insert {d : Doc} {o} (h : NoLost d) (ho : ¬ Suspended o) : NoLost (d.insert o) :=
  noLost_iff.2 (rungs_insert (noLost_iff.1 h) ho)

theorem noLost_settle {tx : Tx} {id s v now cfg o} (hc : Core tx.doc) (h : NoLost tx.doc) (hf : Fresh now tx.doc)
    (hg : tx.doc.get id = some o) (hs : s ≠ .pending) : NoLost (settle tx id s v now cfg).1.doc := by
  obtain ⟨ho, _⟩ := get_mem hg
  have h1 : Held (fun x => x = id) (tx.doc.modify id (setSettled s v now)) :=
    held_modify hc (setSettled_id s v now) hg (weak_setSettled (hc.weak o ho) hs)
      (fun hn => absurd rfl hn) (refs_setSettled (hc.refs o ho)) (fun _ _ he => he.elim)
  have hs1 : Settled (tx.doc.modify id (setSettled s v now)) id :=
    settled_modify_self hc.nodup (setSettled_id s v now) hg (by simpa [setSettled] using hs)
  have r1 : Rungs (fun x => x = id) (tx.doc.modify id (setSettled s v now)) := by
    refine rungs_settleMap h (modify_objects _ _ _) (fun o => by split <;> rfl) (fun o => by split <;> rfl)
      (fun o => by split <;> rfl) (fun o _ => ?_)
    by_cases hx : o.id = id
    · exact Or.inr hx
    · exact Or.inl (by simp [hx])
  have f1 : Fresh now (tx.doc.modify id (setSettled s v now)) := by
    intro o' ho' hp
    obtain ⟨o2, ho2, rfl⟩ := mem_modify ho'
    by_cases hx : o2.id = id
    · simp [hx, setSettled] at hp; exact absurd hp hs
    · simp only [hx, beq_iff_eq, if_false] at hp ⊢; exact hf o2 ho2 hp
  unfold settle
  obtain ⟨r2, _⟩ := rungs_trigger (tx := tx.modify id (setSettled s v now)) (now := now) (cfg := cfg) h1 hs1 r1 f1
  exact noLost_iff.2 (r2.mono fun x ⟨e, ne⟩ => absurd e ne)

section Ops

variable {tx : Tx} {now : Int} {cfg : Cfg}

theorem keeps_claim (pid ttl now) (o : Obj) : Keeps (fun _ => False) o (claim pid ttl now o) := by
  unfold claim; exact keeps_taskMap fun t hs => by simp [Task.armLease] at hs

theorem keeps_unclaim (now cfg) (o : Obj) : Keeps (fun _ => False) o (unclaim now cfg o) := by
  unfold unclaim; exact keeps_taskMap fun t hs => by simp [Task.armRetry] at hs

theorem keeps_halt (o : Obj) : Keeps (fun _ => False) o (halt o) := by
  unfold halt; exact keeps_taskMap fun t hs => by simp [Task.disarm] at hs

theorem keeps_resume (now cfg) (o : Obj) : Keeps (fun _ => False) o (resume now cfg o) := by
  unfold resume; exact keeps_taskMap fun t hs => by simp [Task.armRetry] at hs

theorem keeps_clearResumes (o : Obj) : Keeps (fun _ => False) o (clearResumes o) := by
  unfold clearResumes; exact keeps_taskMap fun t hs => hs

theorem not_susp_new {p : Promise} {id} {t : Option Task} (ht : ∀ t', t = some t' → t'.state ≠ .suspended) :
    ¬ Suspended ⟨id, p, t⟩ := by
  rintro ⟨t', e, hs⟩; exact ht t' e hs

theorem noLost_promiseCreate (h : NoLost tx.doc) (r : PromiseCreate) : NoLost (promiseCreate tx r now cfg).1.doc := by
  unfold promiseCreate
  split
  · exact h
  · split
    · exact h
    · dsimp only
      split
      · exact noLost_insert h (not_susp_new (by simp))
      · split
        · exact noLost_insert h (not_susp_new (by simp))
        · split
          · split
            · exact noLost_insert h (not_susp_new (by simp [Task.armRetry]))
            · simpa using noLost_insert h (not_susp_new (by simp [Task.armRetry]))
          · simpa using noLost_insert h (not_susp_new (by simp [Task.armRetry]))

theorem noLost_promiseSettle (hc : Core tx.doc) (h : NoLost tx.doc) (hf : Fresh now tx.doc) (r : PromiseSettle) :
    NoLost (promiseSettle tx r now cfg).1.doc := by
  unfold promiseSettle
  split
  · exact h
  · rename_i hs
    split
    · exact h
    · rename_i o hg
      split
      · exact h
      · exact noLost_settle hc h hf hg (ne_pending_of_settleState (by simpa using hs))

theorem noLost_taskCreate (h : NoLost tx.doc) (pid ttl a) : NoLost (taskCreate tx pid ttl a now cfg).1.doc := by
  unfold taskCreate
  repeat' (first
    | exact h
    | exact noLost_maps h (mapsBy_modify_any (keeps_claim _ _ _))
    | exact noLost_insert h (not_susp_new (by intro t e; split at e <;> (cases e; simp [Task.armLease])))
    | split)

theorem noLost_taskFulfill (hc : Core tx.doc) (h : NoLost tx.doc) (hf : Fresh now tx.doc) (id v a) :
    NoLost (taskFulfill tx id v a now cfg).1.doc := by
  unfold taskFulfill
  split
  · exact h
  · split
    · exact h
    · rename_i hs
      split
      · exact h
      · split
        · exact h
        · split
          · exact h
          · rename_i po hg
            split
            · simpa using noLost_maps h (mapsBy_modify_any keeps_fulfilTask)
            · exact noLost_settle hc h hf hg (ne_pending_of_settleState (by simpa using hs))

theorem mapsBy_foldl_addCallback {id : String} :
    ∀ (l : List String) (tx : Tx), MapsBy (fun _ => False) tx.doc (l.foldl (fun tx a => tx.modify a (addCallback id)) tx).doc
  | [], _ => MapsBy.refl _ _
  | c :: l, tx => by
    simp only [List.foldl_cons]
    exact (mapsBy_modify_any (id := c) (keeps_addCallback id)).trans (mapsBy_foldl_addCallback l _)

/-- After registering `id` on every awaited promise, each of them carries it. -/
theorem registered_foldl {id : String} :
    ∀ (l : List String) (tx : Tx) (a : String), a ∈ l → ∀ p ∈ tx.doc.objects, p.id = a →
      ∃ p' ∈ (l.foldl (fun tx a => tx.modify a (addCallback id)) tx).doc.objects,
        p'.id = a ∧ id ∈ p'.promise.callbacks ∧ p'.promise.state = p.promise.state
  | [], _, _, ha, _, _, _ => absurd ha List.not_mem_nil
  | b :: l, tx, a, ha, p, hp, hpa => by
    simp only [List.foldl_cons]
    have m : MapsBy (fun _ => False) (tx.modify b (addCallback id)).doc
        (l.foldl (fun tx a => tx.modify a (addCallback id)) (tx.modify b (addCallback id))).doc :=
      mapsBy_foldl_addCallback l _
    have m0 : MapsBy (fun _ => False) tx.doc (tx.modify b (addCallback id)).doc := mapsBy_modify_any (keeps_addCallback id)
    by_cases hab : a = b
    · subst hab
      have hp1 : addCallback id p ∈ (tx.modify a (addCallback id)).doc.objects := by
        simp only [Tx.modify_doc, modify_objects]
        exact List.mem_map.2 ⟨p, hp, by simp [hpa]⟩
      have hin : id ∈ (addCallback id p).promise.callbacks := by
        unfold addCallback; split
        · rename_i hc; simpa using hc
        · simp
      obtain ⟨p', hp', k⟩ := m.fwd hp1
      refine ⟨p', hp', by rw [k.id]; exact (addCallback_id id p).trans hpa, k.callbacks (fun h => h) _ hin, ?_⟩
      rw [k.state]; unfold addCallback; split <;> rfl
    · have hl : a ∈ l := by
        rcases List.mem_cons.1 ha with h | h
        · exact absurd h hab
        · exact h
      obtain ⟨p1, hp1, k1⟩ := m0.fwd hp
      obtain ⟨p', hp', h1, h2, h3⟩ := registered_foldl l _ a hl p1 hp1 (k1.id.trans hpa)
      exact ⟨p', hp', h1, h2, h3.trans k1.state⟩

theorem noLost_taskSuspend (hc : Core tx.doc) (h : NoLost tx.doc) (id v awaited) :
    NoLost (taskSuspend tx id v awaited cfg).1.doc := by
  unfold taskSuspend
  split
  · exact h
  · rename_i hne0
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
            · rename_i t hat
              split
              · exact h
              · rename_i hmiss
                split
                · exact h
                · dsimp only
                  have m1 : MapsBy (fun _ => False) tx.doc (tx.modify id clearResumes).doc :=
                    mapsBy_modify_any keeps_clearResumes
                  split
                  · exact noLost_maps h m1
                  · rename_i hsettled
                    let tx2 := awaited.foldl (fun tx a => tx.modify a (addCallback id)) (tx.modify id clearResumes)
                    have m2 : MapsBy (fun _ => False) tx.doc tx2.doc :=
                      m1.trans (mapsBy_foldl_addCallback awaited _)
                    have n2 : NoLost tx2.doc := noLost_maps h m2
                    -- one awaited promise, pending, now carrying the registration
                    obtain ⟨a, ha⟩ : ∃ a, a ∈ awaited := by
                      cases awaited with
                      | nil => simp at hne0
                      | cons a _ => exact ⟨a, by simp⟩
                    have hida : a ≠ id := by intro e; subst e; simp_all
                    obtain ⟨pa, hga⟩ : ∃ pa, tx.doc.get a = some pa := by
                      cases hg : tx.doc.get a with
                      | none => simp only [List.any_eq_true, Option.isNone_iff_eq_none] at hmiss; exact absurd ⟨a, ha, hg⟩ hmiss
                      | some pa => exact ⟨pa, rfl⟩
                    obtain ⟨hpa, hpaid⟩ := get_mem hga
                    have hpend : pa.promise.state = .pending := by
                      simp only [List.any_eq_true, not_exists, not_and] at hsettled
                      have := hsettled a ha
                      rw [hga] at this
                      simpa using this
                    obtain ⟨pa1, hpa1, k1⟩ := m1.fwd hpa
                    obtain ⟨pa2, hpa2, hid2, hcb2, hst2⟩ :=
                      registered_foldl awaited (tx.modify id clearResumes) a ha pa1 hpa1 (k1.id.trans hpaid)
                    simp only [Tx.modify_doc]
                    intro o' ho' hs'
                    obtain ⟨o2, ho2, rfl⟩ := mem_modify ho'
                    have hpa2' : pa2 ∈ (tx2.doc.modify id park).objects := by
                      simp only [modify_objects]
                      exact List.mem_map.2 ⟨pa2, hpa2, by simp [hid2, hida]⟩
                    by_cases hx : o2.id = id
                    · refine ⟨pa2, hpa2', ?_, ?_⟩
                      · simp only [hx, beq_self_eq_true, if_true]
                        show o2.id ∈ pa2.promise.callbacks
                        rw [hx]; exact hcb2
                      · rw [hst2, k1.state]; exact hpend
                    · simp only [hx, beq_iff_eq, if_false] at hs' ⊢
                      obtain ⟨p, hp, hcp, hps⟩ := n2 o2 ho2 hs'
                      refine ⟨if p.id == id then park p else p, List.mem_map_of_mem hp, ?_, ?_⟩
                      · split <;> simpa [park] using hcp
                      · split <;> simpa [park] using hps

theorem noLost_taskFence (hc : Core tx.doc) (h : NoLost tx.doc) (hf : Fresh now tx.doc) (id v c a) :
    NoLost (taskFence tx id v c a now cfg).1.doc := by
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
        · have h' := noLost_promiseCreate (now := now) (cfg := cfg) h r
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
        · have h' := noLost_promiseSettle (now := now) (cfg := cfg) hc h hf r
          revert h'
          generalize promiseSettle tx r now cfg = p
          rcases p with ⟨tx', n⟩
          intro h'
          exact h'

theorem noLost_taskHeartbeat (h : NoLost tx.doc) (pid tasks) : NoLost (taskHeartbeat tx pid tasks now).1.doc := by
  unfold taskHeartbeat
  split
  · exact h
  · dsimp only
    suffices ∀ (l : List (String × Nat)) (tx : Tx),
        MapsBy (fun _ => False) tx.doc (l.foldl (fun tx (x : String × Nat) => tx.modify x.1 (beat pid x.2 now)) tx).doc from
      noLost_maps h (this _ _)
    intro l
    induction l with
    | nil => exact fun _ => MapsBy.refl _ _
    | cons x l ih =>
      intro tx
      simp only [List.foldl_cons]
      exact (mapsBy_modify_any (id := x.1) (keeps_beat pid x.2 now)).trans (ih (tx.modify x.1 (beat pid x.2 now)))

end Ops

theorem noLost_decide {tx : Tx} {now cfg} (hc : Core tx.doc) (h : NoLost tx.doc) (hf : Fresh now tx.doc) (req : Req) :
    NoLost (decide_ tx req now cfg).1.doc := by
  cases req with
  | promiseGet id => show NoLost (promiseGet tx id).1.doc; unfold promiseGet; split <;> exact h
  | promiseCreate r => exact noLost_promiseCreate h r
  | promiseSettle r => exact noLost_promiseSettle hc h hf r
  | promiseRegisterCallback a b =>
    show NoLost (promiseRegisterCallback tx a b).1.doc
    unfold promiseRegisterCallback
    repeat' (first | exact h | exact noLost_maps h (mapsBy_modify_any (keeps_addCallback _)) | split)
  | promiseRegisterListener a b =>
    show NoLost (promiseRegisterListener tx a b).1.doc
    unfold promiseRegisterListener
    repeat' (first | exact h | exact noLost_maps h (mapsBy_modify_any (keeps_addListener _)) | split)
  | taskGet id => show NoLost (taskGet tx id).1.doc; unfold taskGet; split <;> exact h
  | taskCreate pid ttl a => exact noLost_taskCreate h pid ttl a
  | taskAcquire id v pid ttl =>
    show NoLost (taskAcquire tx id v pid ttl now cfg).1.doc
    unfold taskAcquire
    repeat' (first | exact h | exact noLost_maps h (mapsBy_modify_any (keeps_claim _ _ _)) | split)
  | taskRelease id v =>
    show NoLost (taskRelease tx id v now cfg).1.doc
    unfold taskRelease
    repeat' (first | exact h | split)
    simpa using noLost_maps h (mapsBy_modify_any (keeps_unclaim _ _))
  | taskFulfill id v a => exact noLost_taskFulfill hc h hf id v a
  | taskSuspend id v aw => exact noLost_taskSuspend hc h id v aw
  | taskFence id v c a => exact noLost_taskFence hc h hf id v c a
  | taskHeartbeat pid ts => exact noLost_taskHeartbeat h pid ts
  | taskHalt id =>
    show NoLost (taskHalt tx id).1.doc
    unfold taskHalt
    repeat' (first | exact h | exact noLost_maps h (mapsBy_modify_any keeps_halt) | split)
  | taskContinue id =>
    show NoLost (taskContinue tx id now cfg).1.doc
    unfold taskContinue
    repeat' (first | exact h | split)
    simpa using noLost_maps h (mapsBy_modify_any (keeps_resume _ _))

/-! ## Over steps and runs -/

theorem handleInternal_noLost {d : Doc} {now cfg} (hc : Core d) (h : NoLost d) :
    ∃ d', committed (handleInternal d now cfg) = some d' ∧ NoLost d' :=
  ⟨_, committed_linearize _ _ _, (sweep_noLost (now := now) (cfg := cfg) hc h).1⟩

theorem handleExternal_noLost {d : Doc} {req now cfg} (hc : Core d) (h : NoLost d) :
    ∃ d', committed (handleExternal d req now cfg).1 = some d' ∧ NoLost d' := by
  unfold handleExternal
  dsimp only
  rw [List.append_assoc]
  obtain ⟨n1, f1⟩ := sweep_noLost (now := now) (cfg := cfg) hc h
  have c1 := core_timerAt (core_sweepTx (now := now) (cfg := cfg) hc) (minDeadline (sweepTx d now cfg).doc)
  exact ⟨_, committed_linearize _ _ _,
    noLost_decide (now := now) (cfg := cfg) (tx := { doc := _ }) c1 n1 f1 req⟩

theorem noLost_empty : NoLost {} := fun o ho => by simp at ho

/-- **In every document the server can commit, every suspended task is
registered on a pending promise: no wakeup is ever lost.** -/
theorem reachable_noLost {cfg} {d : Doc} (h : Reachable cfg d) : NoLost d := by
  induction h with
  | empty => exact noLost_empty
  | step hr s ih =>
    have hc := (reachable_inv hr).1
    cases s with
    | internal now e =>
      obtain ⟨d'', e', h''⟩ := handleInternal_noLost (now := now) (cfg := cfg) hc ih
      rw [e] at e'; cases e'; exact h''
    | external req now e =>
      obtain ⟨d'', e', h''⟩ := handleExternal_noLost (req := req) (now := now) (cfg := cfg) hc ih
      rw [e] at e'; cases e'; exact h''

end Kernel
