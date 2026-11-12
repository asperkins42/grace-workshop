/*
 * Copyright 2021 The CFU-Playground Authors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

// GRACE example: an NxN integer GEMM computed by a 16x16 tile engine in the CFU.
//
// The CFU (cfu.v) holds three 16x16 scratchpads -- A, B and an accumulator --
// plus an AXI master onto HBM. Firmware splits the matrices into 16x16 tiles
// and walks this schedule:
//
//   for each output tile C(i,j):
//     clear the accumulator
//     for k = 0 .. N/16-1:
//       load A(i,k), load B(k,j), MAC        (accumulator += A(i,k) * B(k,j))
//     store the accumulator to C(i,j)
//
// The CFU fetches each tile as one contiguous 1 KiB block, so pim_gemm_entry()
// first packs the row-major inputs into tile-major order, and afterwards
// unpacks the result back to row-major.

#include "proj_menu.h"

#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>

#include "base.h"
#include "cfu.h"
#include "menu.h"
#include "perf.h"

extern "C" {
#include "system.h"
}

// CFU function codes (funct7). These must be macros: cfu_op0() pastes them
// into the instruction encoding.
#define OP_LOAD_A 1    // HBM tile -> scratchpad A
#define OP_LOAD_B 2    // HBM tile -> scratchpad B
#define OP_MAC 3       // accumulator += A * B
#define OP_STORE_C 11  // accumulator -> HBM tile
#define OP_CLEAR_C 12  // accumulator = 0
#define CFU_OK 0xBEEF0000u

namespace {

constexpr unsigned TILE = 16;
constexpr unsigned TILE_WORDS = TILE * TILE;  // 1 KiB per tile
constexpr unsigned MAX_N = 256;

// Tile-major copies of A, B and C. These are the only buffers the CFU touches.
// Like all firmware data they live in main RAM (HBM0), which the CFU's AXI
// master reaches at the same addresses as the CPU.
alignas(32) int32_t g_a_tiles[MAX_N * MAX_N];
alignas(32) int32_t g_b_tiles[MAX_N * MAX_N];
alignas(32) int32_t g_c_tiles[MAX_N * MAX_N];

// When set, pim_gemm_entry() prints each step of the schedule as it runs it.
bool g_print_schedule = false;

// Cycle breakdown of the last pim_gemm_entry() call. It is printed by the
// caller, outside the timed region: one line over the UART costs ~80k cycles.
uint64_t g_pack_cycles, g_cfu_cycles, g_unpack_cycles;

inline void fence() { asm volatile("fence iorw, iorw" ::: "memory"); }

inline uint32_t addr_of(const int32_t* p) { return (uint32_t)(uintptr_t)p; }

// Index of element (r, c) of an n x n matrix stored tile-major: tiles are
// laid out row by row, and each tile is itself a row-major 16x16 block.
inline unsigned tiled_index(unsigned r, unsigned c, unsigned n) {
  const unsigned tile = (r / TILE) * (n / TILE) + (c / TILE);
  return tile * TILE_WORDS + (r % TILE) * TILE + (c % TILE);
}

void to_tiles(const int32_t* rm, int32_t* tm, unsigned n) {
  for (unsigned r = 0; r < n; r++)
    for (unsigned c = 0; c < n; c++) tm[tiled_index(r, c, n)] = rm[r * n + c];
}

void from_tiles(const int32_t* tm, int32_t* rm, unsigned n) {
  for (unsigned r = 0; r < n; r++)
    for (unsigned c = 0; c < n; c++) rm[r * n + c] = tm[tiled_index(r, c, n)];
}

// Address of tile (row, col) in a tile-major matrix with `tiles` tiles per row.
inline uint32_t tile_addr(const int32_t* m, unsigned row, unsigned col,
                          unsigned tiles) {
  return addr_of(m + (row * tiles + col) * TILE_WORDS);
}

bool cfu_ok(uint32_t status, const char* what) {
  if (status == CFU_OK) return true;
  printf("CFU %s failed: status 0x%08" PRIx32 "\n", what, status);
  return false;
}

// The partitioning schedule. With run=false it only prints the steps; with
// run=true it issues them to the CFU (and prints them first if print=true).
bool tiled_gemm(unsigned n, bool run, bool print) {
  const unsigned tiles = n / TILE;
  if (print) {
    printf("\nSchedule for N=%u: %ux%u output tiles, %u k-steps each\n", n,
           tiles, tiles, tiles);
  }
  for (unsigned i = 0; i < tiles; i++) {
    for (unsigned j = 0; j < tiles; j++) {
      if (print) printf("C(%u,%u): clear accumulator\n", i, j);
      if (run && !cfu_ok(cfu_op0(OP_CLEAR_C, 0, 0), "clear")) return false;

      for (unsigned k = 0; k < tiles; k++) {
        const uint32_t a = tile_addr(g_a_tiles, i, k, tiles);
        const uint32_t b = tile_addr(g_b_tiles, k, j, tiles);
        if (print) {
          printf("  k=%u: load A(%u,%u) @ 0x%08" PRIx32 ", load B(%u,%u) @ 0x%08" PRIx32
                 ", MAC\n",
                 k, i, k, a, k, j, b);
        }
        if (run && !(cfu_ok(cfu_op0(OP_LOAD_A, a, 0), "load A") &&
                     cfu_ok(cfu_op0(OP_LOAD_B, b, 0), "load B") &&
                     cfu_ok(cfu_op0(OP_MAC, 0, 0), "MAC"))) {
          return false;
        }
      }

      const uint32_t c = tile_addr(g_c_tiles, i, j, tiles);
      if (print) printf("  store accumulator -> C(%u,%u) @ 0x%08" PRIx32 "\n", i, j, c);
      if (run && !cfu_ok(cfu_op0(OP_STORE_C, c, 0), "store C")) return false;
    }
  }
  return true;
}

// Plain CPU GEMM: the reference result and the fallback for unsupported shapes.
void gemm_cpu(const int32_t* A, const int32_t* B, int32_t* C, int M, int N,
              int K) {
  for (int i = 0; i < M; i++) {
    for (int j = 0; j < N; j++) C[i * N + j] = 0;
    for (int k = 0; k < K; k++) {
      const int32_t a = A[i * K + k];
      for (int j = 0; j < N; j++) C[i * N + j] += a * B[k * N + j];
    }
  }
}

}  // anonymous namespace

// C = A * B with A MxK, B KxN and C MxN, all row-major. The CFU handles square
// matrices whose size is a multiple of 16 (up to MAX_N); anything else runs on
// the CPU.
extern "C" __attribute__((noinline)) void pim_gemm_entry(const int32_t* A,
                                                         const int32_t* B,
                                                         int32_t* C, int M,
                                                         int N, int K) {
  if (M != N || N != K || N <= 0 || N % TILE != 0 || N > (int)MAX_N) {
    puts("pim_gemm_entry: shape not supported by the CFU, using the CPU");
    gemm_cpu(A, B, C, M, N, K);
    return;
  }
  const unsigned n = N;

  const uint64_t t0 = perf_get_mcycle64();
  to_tiles(A, g_a_tiles, n);
  to_tiles(B, g_b_tiles, n);
  fence();  // the CFU must see the packed tiles

  const uint64_t t1 = perf_get_mcycle64();
  const bool ok = tiled_gemm(n, /*run=*/true, g_print_schedule);
  flush_cpu_dcache();  // the CFU wrote C behind the CPU's data cache
  fence();

  const uint64_t t2 = perf_get_mcycle64();
  if (!ok) {
    puts("pim_gemm_entry: CFU error, using the CPU");
    gemm_cpu(A, B, C, M, N, K);
    return;
  }
  from_tiles(g_c_tiles, C, n);

  const uint64_t t3 = perf_get_mcycle64();
  g_pack_cycles = t1 - t0;
  g_cfu_cycles = t2 - t1;
  g_unpack_cycles = t3 - t2;
}

namespace {

// Row-major test matrices and results.
int32_t g_a[MAX_N * MAX_N];
int32_t g_b[MAX_N * MAX_N];
int32_t g_c[MAX_N * MAX_N];
int32_t g_ref[MAX_N * MAX_N];

bool read_n(unsigned* n) {
  *n = read_val("Matrix size N (multiple of 16, up to 256)");
  // An Enter typed after the menu key arrives here as an empty line (0).
  if (*n == 0) *n = read_val("Matrix size N (multiple of 16, up to 256)");
  if (*n == 0 || *n % TILE != 0 || *n > MAX_N) {
    puts("N must be a multiple of 16 between 16 and 256");
    return false;
  }
  return true;
}

// Small signed values, so the result is easy to check and cannot overflow.
void seed_matrices(unsigned n) {
  for (unsigned r = 0; r < n; r++) {
    for (unsigned c = 0; c < n; c++) {
      g_a[r * n + c] = (int32_t)((r * 3 + c) % 17) - 8;
      g_b[r * n + c] = (int32_t)((r + c * 5) % 13) - 6;
      g_c[r * n + c] = 0x7EADBEEF;  // so a missing result cannot pass
    }
  }
}

void print_breakdown(void) {
  printf("pim_gemm_entry: pack %" PRIu64 " + CFU %" PRIu64 " + unpack %" PRIu64
         " cycles\n",
         g_pack_cycles, g_cfu_cycles, g_unpack_cycles);
}

bool check_result(unsigned n) {
  for (unsigned i = 0; i < n * n; i++) {
    if (g_c[i] != g_ref[i]) {
      printf("FAIL at C(%u,%u): CFU %ld, CPU %ld\n", i / n, i % n,
             (long)g_c[i], (long)g_ref[i]);
      return false;
    }
  }
  puts("PASS: CFU result matches the CPU reference");
  return true;
}

// Print the tiling schedule without touching the CFU.
void do_print_schedule(void) {
  unsigned n;
  if (!read_n(&n)) return;
  tiled_gemm(n, /*run=*/false, /*print=*/true);
}

// Run a GEMM on the CFU, printing each step as it is issued.
void do_run_gemm(void) {
  unsigned n;
  if (!read_n(&n)) return;
  seed_matrices(n);
  g_print_schedule = true;
  pim_gemm_entry(g_a, g_b, g_c, n, n, n);
  g_print_schedule = false;
  print_breakdown();  // CFU time here includes printing the steps
  gemm_cpu(g_a, g_b, g_ref, n, n, n);
  check_result(n);
}

// Time the CPU against the CFU (including pack/unpack) without printing steps.
void do_benchmark(void) {
  unsigned n;
  if (!read_n(&n)) return;
  seed_matrices(n);

  const uint64_t t0 = perf_get_mcycle64();
  gemm_cpu(g_a, g_b, g_ref, n, n, n);
  const uint64_t t1 = perf_get_mcycle64();
  pim_gemm_entry(g_a, g_b, g_c, n, n, n);
  const uint64_t t2 = perf_get_mcycle64();

  const uint64_t cpu = t1 - t0;
  const uint64_t cfu = t2 - t1;
  print_breakdown();
  printf("CPU: %" PRIu64 " cycles\nCFU: %" PRIu64 " cycles (end to end)\n", cpu,
         cfu);
  // printf here has no floating point, so print the ratio in fixed point.
  const uint64_t x100 = cpu * 100 / cfu;
  printf("Speedup: %" PRIu64 ".%02" PRIu64 "x\n", x100 / 100, x100 % 100);
  check_result(n);
}

struct Menu MENU = {
    "GRACE Tiled GEMM",
    "gemm",
    {
        MENU_ITEM('s', "print the tiling schedule", do_print_schedule),
        MENU_ITEM('g', "run GEMM on the CFU, printing each step", do_run_gemm),
        MENU_ITEM('b', "benchmark CPU vs CFU", do_benchmark),
        MENU_END,
    },
};

}  // anonymous namespace

extern "C" void do_proj_menu() { menu_run(&MENU); }
