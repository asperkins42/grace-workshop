# example_grace_gemm

A minimal GRACE example: an NxN integer GEMM offloaded to a 16x16 tile engine
in the CFU, on the Alveo U280 with HBM. Derived from an earlier GRACE project.

| File | What it is |
|------|------------|
| `cfu.v` | CFU: three 16x16 scratchpads (A, B, accumulator), AXI master to HBM, command decoder |
| `gemm16x16.v` | 16x16 multiply-accumulate engine (accumulator += A * B) |
| `rams_tdp_rf_rf.v` | Dual-port block RAM used for the scratchpads |
| `unused_axi_ports.vh` | Declares the 31 AXI ports this CFU does not use (the SoC always connects 32) |
| `src/proj_menu.cc` | Tiling schedule, `pim_gemm_entry()`, and the menu |
| `src/software_cfu.cc` | C model of the CFU, for `CFU_SOFTWARE_DEFINED` builds |

CFU ops (funct7): 1 load A tile, 2 load B tile, 3 MAC, 11 store accumulator,
12 clear accumulator.

## Menu

- `s` prints the tiling schedule for N without running it.
- `g` runs the GEMM on the CFU, printing each step as it is issued, then
  checks the result against the CPU.
- `b` times the CPU against the CFU, including packing to tile-major and back.

## Build and run

```sh
make -C proj/example_grace_gemm software  # firmware only, no Vivado
make -C proj/example_grace_gemm prog      # builds the bitstream on first run (~12 min on milan3)
make -C proj/example_grace_gemm load TTY=/dev/ttyUSB2   # at litex>, press Enter or type reboot
```
