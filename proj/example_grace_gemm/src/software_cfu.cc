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

#include <stdint.h>
#include "software_cfu.h"

// Scratchpads A, B and the accumulator C, one 16x16 tile each.
static int32_t sp_a[256], sp_b[256], sp_c[256];

//
// In this function, place C code to emulate your CFU. You can switch between
// hardware and emulated CFU by setting the CFU_SOFTWARE_DEFINED DEFINE in
// the Makefile.
uint32_t software_cfu(int funct3, int funct7, uint32_t rs1, uint32_t rs2)
{
  int32_t* mem = (int32_t*)(uintptr_t)rs1;

  switch (funct7)
  {
    case 1:  // load A tile from memory
      for (int i = 0; i < 256; ++i) sp_a[i] = mem[i];
      break;
    case 2:  // load B tile from memory
      for (int i = 0; i < 256; ++i) sp_b[i] = mem[i];
      break;
    case 3:  // C += A * B
      for (int i = 0; i < 16; ++i)
        for (int j = 0; j < 16; ++j)
          for (int k = 0; k < 16; ++k)
            sp_c[i * 16 + j] += sp_a[i * 16 + k] * sp_b[k * 16 + j];
      break;
    case 11:  // store C tile to memory
      for (int i = 0; i < 256; ++i) mem[i] = sp_c[i];
      break;
    case 12:  // clear C
      for (int i = 0; i < 256; ++i) sp_c[i] = 0;
      break;
    default:
      return 0xDEADFFFF;
  }
  return 0xBEEF0000;
}
