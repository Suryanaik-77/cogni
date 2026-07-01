# lowpower_demo — UPF checker ground truth

A 3-power-domain subsystem (`rtl/power_soc.sv`) with companion power intent
(`upf/power_soc.upf`). Built so a UPF-vs-RTL consistency checker has real
structure to verify and known bugs to catch.

## Power architecture

| Domain  | Rail     | Voltage | Gated? | RTL elements                     |
|---------|----------|---------|--------|----------------------------------|
| PD_AON  | VDD      | 1.0 V   | no     | power-control FSM, wake logic    |
| PD_CPU  | VDD_CPU  | 1.0 V   | yes    | `cpu_acc`, `cpu_result`, `cpu_done` |
| PD_MEM  | VDD_MEM  | 0.8 V   | yes    | `mem`, `mem_rdata`               |

AON FSM sequences: RUN → ISO → SAVE → OFF → RESTORE → DEISO → RUN, driving
`cpu_pwr_en / cpu_iso_en / cpu_ret_save / cpu_ret_restore / mem_pwr_en / mem_iso_en`.

## Seeded intent gaps (expected checker findings)

Checker result: **5 findings — all 4 seeds + 1 bonus, 0 false positives.**

| # | Signal / element | Bug | Rule | Status |
|---|------------------|-----|------|--------|
| 1 | `cpu_active` | PD_CPU output driven raw (`= cpu_done`), no isolation clamp, but `set_isolation cpu_iso -applies_to outputs` requires all PD_CPU outputs clamped | `UPF_missing_isolation` (error) | ✅ |
| 2 | `scratch_rdata` | PD_MEM(0.8V) → PD_AON(1.0V) crossing with no `set_level_shifter` declared | `UPF_missing_level_shifter` (error) | ✅ |
| 3 | `cpu_acc` | AON FSM save/restores CPU state, but `set_retention cpu_ret -elements {cpu_result}` omits the self-accumulating register | `UPF_retention_gap` (warning) | ✅ |
| 4 | `mem_iso_en` | Driven by FSM as PD_MEM isolation control, but no `set_isolation` strategy references it | `UPF_unused_iso_control` (warning) | ✅ |
| 5 (bonus) | `scratch_rdata` | PD_MEM is switchable but has **no isolation strategy at all** — output floats on power-down | `UPF_missing_isolation` (error) | ✅ real |

## Correct intent (checker should NOT flag)

- `result_data`, `result_valid` — clamped with `cpu_iso_en` → matches `cpu_iso`.
- `cpu_sw`, `mem_sw` — power switches controlled by `cpu_pwr_en` / `mem_pwr_en`.
- `cpu_ret_save` / `cpu_ret_restore` — wired to `set_retention` save/restore.

## Note

Plain RTL lint currently reports `cpu_ret_save`, `cpu_ret_restore`, `mem_iso_en`
as **W528 unused_signal** — they only have meaning to the power intent. A
UPF-aware pass should reclassify them as isolation/retention controls (and
still flag `mem_iso_en` under gap #4, since no strategy consumes it).
